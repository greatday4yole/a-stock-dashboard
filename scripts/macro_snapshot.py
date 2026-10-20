#!/usr/bin/env python3
"""Fetch a fail-fast official China macro snapshot for the local dashboard.

The two data routes intentionally follow a-stock-data v3.7.1 section 11:

* National Bureau of Statistics latest-release index -> PMI article.
* People's Bank of China statistics index -> year page -> social-financing
  topic page -> official Excel workbook.

No unpublished month is imputed.  A changed page or workbook structure raises
an exception before a cache file can be replaced.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

NBS_INDEX = "https://www.stats.gov.cn/sj/zxfb/"
PBC_BASE = "https://www.pbc.gov.cn"
PBC_INDEX = f"{PBC_BASE}/diaochatongjisi/116219/116319/index.html"

SOCIAL_COLUMNS = [
    "month",
    "afre_total",
    "rmb_loans",
    "fx_loans",
    "entrusted_loans",
    "trust_loans",
    "undiscounted_bankers_acceptance",
    "corporate_bonds",
    "government_bonds",
    "equity_financing",
    "abs_by_depository",
    "loans_written_off",
]

SOCIAL_HEADER_FRAGMENTS = [
    "月份",
    "社会融资规模增量",
    "人民币贷款",
    "外币贷款（折合人民币）",
    "委托贷款",
    "信托贷款",
    "未贴现银行承兑汇票",
    "企业债券",
    "政府债券",
    "非金融企业境内股票融资",
    "存款类金融机构资产支持证券",
    "贷款核销",
]

_PMI_TITLE_RE = re.compile(r"(\d{4})年(\d{1,2})月中国采购经理指数运行情况")
_YEAR_TITLE_RE = re.compile(r"^(\d{4})年统计数据$")
_MONTH_CELL_RE = re.compile(r"^(\d{4})\.(\d{1,2})$")


def _build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


SESSION = _build_session()


class _DocumentParser(HTMLParser):
    """Collect links and meta tags without depending on BeautifulSoup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self.meta: Dict[str, str] = {}
        self._href: Optional[str] = None
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        values = {str(key).lower(): value or "" for key, value in attrs}
        lower = tag.lower()
        if lower == "a":
            self._href = values.get("href")
            self._parts = []
        elif lower == "meta":
            name = values.get("name", "").strip().lower()
            if name:
                self.meta[name] = values.get("content", "").strip()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._parts)))
            self._href = None
            self._parts = []


def _parse_document(value: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(value)
    parser.close()
    return parser


def _normal_text(value: Any) -> str:
    return re.sub(r"[\s\u3000\xa0]+", "", str(value or ""))


def _official_url(base: str, href: str, authority: str) -> str:
    url = urljoin(base, href)
    host = (urlparse(url).hostname or "").lower()
    if host != authority and not host.endswith("." + authority):
        raise RuntimeError(f"官方页面链接跳转到非官方域名: {url}")
    if urlparse(url).scheme != "https":
        raise RuntimeError(f"官方页面链接不是 HTTPS: {url}")
    return url


def _response(session: Any, url: str, timeout: int) -> Any:
    response = session.get(url, headers={"User-Agent": UA}, timeout=timeout)
    response.raise_for_status()
    final_url = str(getattr(response, "url", url) or url)
    requested_host = (urlparse(url).hostname or "").lower()
    final_host = (urlparse(final_url).hostname or "").lower()
    if final_host != requested_host:
        raise RuntimeError(f"官方请求发生跨域重定向: {url} -> {final_url}")
    return response


def _get_text(session: Any, url: str, timeout: int = 30) -> str:
    response = _response(session, url, timeout)
    encoding = getattr(response, "encoding", None)
    if not encoding or str(encoding).lower() in ("iso-8859-1", "latin-1"):
        apparent = getattr(response, "apparent_encoding", None)
        if apparent:
            response.encoding = apparent
    text_value = response.text
    if not isinstance(text_value, str) or not text_value.strip():
        raise RuntimeError(f"官方页面为空: {url}")
    return text_value


def _get_bytes(session: Any, url: str, timeout: int = 60) -> bytes:
    content = bytes(_response(session, url, timeout).content)
    if len(content) < 100:
        raise RuntimeError(f"官方附件过短，可能不是工作簿: {url}")
    if not (content.startswith(b"PK") or content.startswith(b"\xd0\xcf\x11\xe0")):
        raise RuntimeError(f"官方附件不是 xls/xlsx 工作簿: {url}")
    return content


def _coerce_now(value: Optional[Any] = None) -> datetime:
    if value is None:
        return datetime.now(CN_TZ)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max)
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"无法解析 --as-of 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _iso_cn_timestamp(value: str, label: str) -> str:
    raw = str(value or "").strip().replace("/", "-")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(f"{label} 时间戳格式已变化: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ).isoformat(timespec="seconds")


def _strip_article_html(value: str) -> str:
    text_value = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", value, flags=re.I | re.S
    )
    text_value = unescape(re.sub(r"<[^>]+>", "", text_value))
    # Section 11.2 requires deleting all whitespace, including full-width and
    # non-breaking spaces inside the full-width PMI parentheses.
    return re.sub(r"[\s\u3000\xa0]+", "", text_value)


def _parse_pmi_article(title: str, url: str, html: str) -> Dict[str, Any]:
    title_match = _PMI_TITLE_RE.search(_normal_text(title))
    if not title_match:
        raise RuntimeError(f"PMI 标题格式已变化: {title!r}")
    period = f"{int(title_match.group(1)):04d}-{int(title_match.group(2)):02d}"
    document = _parse_document(html)
    article_title = document.meta.get("articletitle", "")
    if article_title:
        article_match = _PMI_TITLE_RE.search(_normal_text(article_title))
        if not article_match:
            raise RuntimeError(f"PMI 页面 ArticleTitle 格式已变化: {article_title!r}")
        article_period = f"{int(article_match.group(1)):04d}-{int(article_match.group(2)):02d}"
        if article_period != period:
            raise RuntimeError(
                f"PMI 索引标题与正文元数据期间不一致: {period} != {article_period}"
            )
    pub_date = document.meta.get("pubdate")
    if not pub_date:
        raise RuntimeError(f"PMI 页面缺少 PubDate 官方发布时间: {url}")
    published_at = _iso_cn_timestamp(pub_date, "PMI PubDate")
    if published_at[:7] != period:
        raise RuntimeError(
            f"PMI 发布月份与数据期间不一致: period={period}, published_at={published_at}"
        )

    text_value = _strip_article_html(html)

    def grab(pattern: str) -> Optional[float]:
        match = re.search(pattern, text_value)
        return float(match.group(1)) if match else None

    large = medium = small = None
    combined = re.search(
        r"大、中、小型企业PMI分别为([\d.]+)%、([\d.]+)%和([\d.]+)%", text_value
    )
    if combined:
        large, medium, small = (float(item) for item in combined.groups())
    else:
        medium_small = re.search(
            r"中、小型企业PMI分别为([\d.]+)%和([\d.]+)%", text_value
        )
        if medium_small:
            medium, small = (float(item) for item in medium_small.groups())
        separate = (
            ("large", r"大型企业PMI为([\d.]+)%"),
            ("medium", r"中型企业PMI为([\d.]+)%"),
            ("small", r"小型企业PMI为([\d.]+)%"),
        )
        for name, pattern in separate:
            match = re.search(pattern, text_value)
            if not match:
                continue
            parsed = float(match.group(1))
            if name == "large":
                large = parsed
            elif name == "medium" and medium is None:
                medium = parsed
            elif name == "small" and small is None:
                small = parsed

    result: Dict[str, Any] = {
        "title": re.sub(r"\s+", " ", title).strip(),
        "period": period,
        "manufacturing_pmi": grab(
            r"(?<!非)制造业采购经理指数[（(]PMI[）)]为([\d.]+)%"
        ),
        "non_manufacturing_pmi": grab(r"非制造业商务活动指数为([\d.]+)%"),
        "composite_pmi": grab(r"综合PMI产出指数为([\d.]+)%"),
        "pmi_large": large,
        "pmi_medium": medium,
        "pmi_small": small,
        "published_at": published_at,
        "source_url": url,
    }
    core = ("manufacturing_pmi", "non_manufacturing_pmi", "composite_pmi")
    missing = [name for name in core if result[name] is None]
    if missing:
        raise RuntimeError(
            f"PMI 正文措辞可能已变更，无法解析 {missing}；请核对页面：{url}"
        )
    invalid = [
        name
        for name in core + ("pmi_large", "pmi_medium", "pmi_small")
        if result[name] is not None and not 0 <= float(result[name]) <= 100
    ]
    if invalid:
        raise RuntimeError(f"PMI 指标超出 0~100 合法范围: {invalid}")
    return result


def fetch_pmi(
    session: Optional[Any] = None,
    as_of: Optional[Any] = None,
    history_limit: int = 2,
    fetched_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the latest official NBS PMI release and, when available, its predecessor."""
    client = session or SESSION
    effective_now = _coerce_now(as_of)
    if history_limit < 1:
        raise ValueError("history_limit 必须至少为 1")
    index_html = _get_text(client, NBS_INDEX)
    candidates: Dict[str, Tuple[str, str]] = {}
    current_period = effective_now.strftime("%Y-%m")
    index_urls = [NBS_INDEX]

    def collect(page_html: str) -> None:
        for href, raw_title in _parse_document(page_html).links:
            title = re.sub(r"\s+", " ", raw_title).strip()
            match = _PMI_TITLE_RE.search(_normal_text(title))
            if not match:
                continue
            period = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
            url = _official_url(NBS_INDEX, href, "stats.gov.cn")
            previous = candidates.get(url)
            if previous and previous[1] != title:
                raise RuntimeError(f"国家统计局同一 PMI 链接出现冲突标题: {url}")
            candidates[url] = (period, title)

    collect(index_html)
    available_periods = lambda: {
        period for period, _ in candidates.values() if period <= current_period
    }
    # NBS currently lists roughly one calendar month's releases per page.  The
    # prior PMI is therefore normally on index_1.html.  Read pagination count
    # from the official createPageHTML call and stop as soon as enough periods
    # are found; cap the historical scan so a malformed page cannot trigger an
    # unbounded crawl.
    count_match = re.search(r"createPageHTML\(\s*(\d+)\s*,", index_html)
    page_count = int(count_match.group(1)) if count_match else 1
    for page_number in range(1, min(page_count, 6)):
        if len(available_periods()) >= history_limit:
            break
        page_url = _official_url(NBS_INDEX, f"index_{page_number}.html", "stats.gov.cn")
        collect(_get_text(client, page_url))
        index_urls.append(page_url)
    if not candidates:
        raise RuntimeError("国家统计局最新发布页未找到「中国采购经理指数运行情况」条目")

    releases: List[Dict[str, Any]] = []
    for url, (period, title) in sorted(
        candidates.items(), key=lambda item: (item[1][0], item[0]), reverse=True
    ):
        if period > current_period:
            continue
        release = _parse_pmi_article(title, url, _get_text(client, url))
        if datetime.fromisoformat(release["published_at"]) > effective_now:
            continue
        if any(item["period"] == release["period"] for item in releases):
            continue
        releases.append(release)
        if len(releases) >= history_limit:
            break
    if not releases:
        raise RuntimeError(f"国家统计局没有截至 {effective_now.isoformat()} 可用的 PMI 发布")

    stamp = fetched_at or effective_now.isoformat(timespec="seconds")
    return {
        "latest": releases[0],
        "previous": releases[1] if len(releases) > 1 else None,
        "releases": releases,
        "fetched_at": stamp,
        "index_url": NBS_INDEX,
        "index_urls": index_urls,
    }


def _discover_pboc_years(index_html: str) -> Dict[int, str]:
    document = _parse_document(index_html)
    result: Dict[int, str] = {}
    for href, raw_title in document.links:
        title = _normal_text(raw_title)
        match = _YEAR_TITLE_RE.match(title)
        if not match:
            continue
        year = int(match.group(1))
        url = _official_url(PBC_INDEX, href, "pbc.gov.cn")
        if year in result and result[year] != url:
            raise RuntimeError(f"人民银行 {year} 年统计数据出现多个不同链接")
        result[year] = url
    if not result:
        raise RuntimeError("人民银行索引页未找到「XXXX年统计数据」链接，页面结构可能已变更")
    return result


def _single_pboc_topic(year: int, year_url: str, year_html: str) -> str:
    matches: List[str] = []
    for href, raw_title in _parse_document(year_html).links:
        if _normal_text(raw_title).startswith("社会融资规模"):
            matches.append(_official_url(year_url, href, "pbc.gov.cn"))
    distinct = list(dict.fromkeys(matches))
    if not distinct:
        raise RuntimeError(f"{year} 年页未找到「社会融资规模」专题链接")
    if len(distinct) != 1:
        raise RuntimeError(f"{year} 年页出现多个不同的「社会融资规模」专题链接: {distinct}")
    return distinct[0]


def _first_social_workbook(year: int, topic_url: str, topic_html: str) -> str:
    books: List[str] = []
    for href, _ in _parse_document(topic_html).links:
        if re.search(r"\.xlsx?(?:$|[?#])", href, flags=re.I):
            books.append(_official_url(topic_url, href, "pbc.gov.cn"))
    if not books:
        raise RuntimeError(f"{year} 年社融专题页未找到 xls/xlsx 附件")
    # The first workbook is the national social-financing flow table.  Its
    # in-workbook title is validated below so a reordered page fails closed.
    return books[0]


def _social_month_label(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    match = _MONTH_CELL_RE.match(str(value).strip())
    if not match:
        return None
    year_value, month_text = match.groups()
    # Excel reads 2026.10 as float 2026.1 and drops the trailing zero.  January
    # remains 2026.01.  Therefore a single decimal digit denotes an x0 month.
    if len(month_text) == 1:
        month_text += "0"
    month_value = int(month_text)
    if not 1 <= month_value <= 12:
        return None
    return f"{int(year_value):04d}-{month_value:02d}"


def _json_number(value: Any) -> Optional[Any]:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _parse_social_workbook(content: bytes, year: int, source_url: str) -> List[Dict[str, Any]]:
    if year < 2021:
        raise ValueError("人民银行社融工作簿本解析器仅支持 2021 年起的新版式")
    try:
        raw = pd.read_excel(io.BytesIO(content), header=None)
    except Exception as exc:
        raise RuntimeError(f"无法读取人民银行社融工作簿: {source_url}: {exc}") from exc
    if raw.shape[1] < len(SOCIAL_COLUMNS):
        raise RuntimeError(
            f"{year} 年社融表列数不足: 期望至少 {len(SOCIAL_COLUMNS)}，实际 {raw.shape[1]}"
        )
    title = _normal_text(raw.iloc[0, 0]) if not raw.empty else ""
    if "社会融资规模增量统计表" not in title:
        raise RuntimeError(
            f"{year} 年专题页首个工作簿不是「社会融资规模增量统计表」: {title!r}"
        )

    header_row: Optional[int] = None
    for row_index in range(len(raw)):
        if _normal_text(raw.iloc[row_index, 0]) == "月份":
            header_row = row_index
            break
    if header_row is None:
        raise RuntimeError(
            f"{year} 年社融表没有独立的「月份」表头单元格；"
            "2020 及更早为不支持的旧版式，或当前工作簿结构已变化"
        )
    actual_headers = [
        _normal_text(raw.iloc[header_row, index]) for index in range(len(SOCIAL_COLUMNS))
    ]
    bad_headers = [
        (index, expected, actual_headers[index])
        for index, expected in enumerate(SOCIAL_HEADER_FRAGMENTS)
        if expected not in actual_headers[index]
    ]
    if bad_headers:
        raise RuntimeError(f"{year} 年社融表中文表头结构已变化: {bad_headers}")

    data_start = header_row + 3
    if data_start >= len(raw) or _social_month_label(raw.iloc[data_start, 0]) is None:
        raise RuntimeError(
            f"{year} 年社融表数据区不再位于「月份」表头后三行，工作簿结构已变化"
        )

    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for row_index in range(data_start, len(raw)):
        month = _social_month_label(raw.iloc[row_index, 0])
        if month is None or not month.startswith(f"{year:04d}-"):
            continue
        values = [_json_number(raw.iloc[row_index, index]) for index in range(1, len(SOCIAL_COLUMNS))]
        # An unpublished month is represented by a month label and an empty
        # AFRE total.  Drop the whole row; never turn it into a zero/NaN record.
        if values[0] is None:
            continue
        if month in seen:
            raise RuntimeError(f"{year} 年社融表出现重复月份: {month}")
        seen.add(month)
        row: Dict[str, Any] = {"month": month}
        row.update(dict(zip(SOCIAL_COLUMNS[1:], values)))
        rows.append(row)

    rows.sort(key=lambda item: item["month"])
    if not rows:
        raise RuntimeError(f"社融表解析后无有效月份（{year} 年），格式可能已变更")
    month_numbers = [int(item["month"][-2:]) for item in rows]
    expected = list(range(1, max(month_numbers) + 1))
    if month_numbers != expected:
        raise RuntimeError(
            f"{year} 年已发布社融月份不连续: actual={month_numbers}, expected={expected}"
        )
    return rows


def fetch_social_financing(
    year: Optional[int] = None,
    session: Optional[Any] = None,
    as_of: Optional[Any] = None,
    fetched_at: Optional[str] = None,
    _index_html: Optional[str] = None,
    _year_links: Optional[Mapping[int, str]] = None,
) -> Dict[str, Any]:
    """Fetch one official PBOC social-financing flow workbook.

    Only published rows are returned.  ``year=None`` selects the newest year
    available on the official index.
    """
    client = session or SESSION
    effective_now = _coerce_now(as_of)
    index_html = _index_html if _index_html is not None else _get_text(client, PBC_INDEX)
    year_links = dict(_year_links or _discover_pboc_years(index_html))
    target = max(year_links) if year is None else int(year)
    if target not in year_links:
        raise ValueError(
            f"人民银行无 {target} 年数据，可选年份: {sorted(year_links, reverse=True)[:8]}"
        )
    if target < 2021:
        raise ValueError("人民银行社融工作簿本解析器仅支持 2021 年起的新版式")

    year_url = year_links[target]
    topic_url = _single_pboc_topic(target, year_url, _get_text(client, year_url))
    topic_html = _get_text(client, topic_url)
    topic_document = _parse_document(topic_html)
    create_date = topic_document.meta.get("createdate")
    if not create_date:
        raise RuntimeError(f"{target} 年社融专题页缺少 createDate 官方更新时间: {topic_url}")
    published_at = _iso_cn_timestamp(create_date, "人民银行 createDate")
    if datetime.fromisoformat(published_at) > effective_now:
        raise RuntimeError(
            f"{target} 年社融专题更新时间晚于 as_of: {published_at} > {effective_now.isoformat()}"
        )
    workbook_url = _first_social_workbook(target, topic_url, topic_html)
    rows = _parse_social_workbook(_get_bytes(client, workbook_url), target, workbook_url)

    if target == effective_now.year and rows[-1]["month"] > effective_now.strftime("%Y-%m"):
        raise RuntimeError(
            f"社融工作簿含 as_of 之后的月份: {rows[-1]['month']} > {effective_now:%Y-%m}"
        )
    stamp = fetched_at or effective_now.isoformat(timespec="seconds")
    return {
        "year": target,
        "rows": rows,
        "latest_month": rows[-1]["month"],
        "published_at": published_at,
        "fetched_at": stamp,
        "source_url": workbook_url,
        "provenance": {
            "index_url": PBC_INDEX,
            "year_page_url": year_url,
            "topic_page_url": topic_url,
            "workbook_url": workbook_url,
        },
    }


def fetch(session: Optional[Any] = None, as_of: Optional[Any] = None) -> Dict[str, Any]:
    """Fetch both official sources once, without deriving investment signals."""
    client = session or SESSION
    effective_now = _coerce_now(as_of)
    fetched_at = effective_now.isoformat(timespec="seconds")
    pmi = fetch_pmi(
        session=client,
        as_of=effective_now,
        history_limit=2,
        fetched_at=fetched_at,
    )
    pbc_index_html = _get_text(client, PBC_INDEX)
    year_links = _discover_pboc_years(pbc_index_html)
    current_year = effective_now.year
    required = (current_year, current_year - 1)
    missing = [year for year in required if year not in year_links]
    if missing:
        raise RuntimeError(f"人民银行索引缺少当年/上年社融入口: {missing}")
    current = fetch_social_financing(
        current_year,
        session=client,
        as_of=effective_now,
        fetched_at=fetched_at,
        _index_html=pbc_index_html,
        _year_links=year_links,
    )
    previous = fetch_social_financing(
        current_year - 1,
        session=client,
        as_of=effective_now,
        fetched_at=fetched_at,
        _index_html=pbc_index_html,
        _year_links=year_links,
    )
    return {
        "fetched_at": fetched_at,
        "as_of": effective_now.isoformat(timespec="seconds"),
        "pmi": pmi,
        "social_financing_current": current,
        "social_financing_previous": previous,
    }


def _sum_social(rows: Iterable[Mapping[str, Any]], through_month: str) -> Dict[str, Any]:
    chosen = [row for row in rows if str(row.get("month", "")) <= through_month]
    result: Dict[str, Any] = {"through_month": through_month, "month_count": len(chosen)}
    for column in SOCIAL_COLUMNS[1:]:
        values = [row.get(column) for row in chosen]
        if not values or any(value is None for value in values):
            result[column] = None
            continue
        total = sum(float(value) for value in values)
        result[column] = int(total) if total.is_integer() else round(total, 4)
    return result


def _percent_change(current: Any, previous: Any, label: str) -> float:
    if current is None or previous in (None, 0):
        raise RuntimeError(f"无法计算 {label}：当期或上年同期社融值缺失/为零")
    return round((float(current) / float(previous) - 1.0) * 100.0, 4)


def _month_lag(latest_period: str, as_of: datetime) -> int:
    match = re.match(r"^(\d{4})-(\d{2})$", latest_period)
    if not match:
        raise RuntimeError(f"非法月度期间: {latest_period!r}")
    latest_index = int(match.group(1)) * 12 + int(match.group(2))
    as_of_index = as_of.year * 12 + as_of.month
    return as_of_index - latest_index


def build_snapshot(
    as_of: Optional[Any] = None,
    session: Optional[Any] = None,
    fetched: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the dashboard-compatible, JSON-safe macro snapshot."""
    effective_now = _coerce_now(as_of)
    raw = dict(fetched or fetch(session=session, as_of=effective_now))
    pmi_raw = dict(raw["pmi"])
    pmi_latest = dict(pmi_raw["latest"])
    pmi_previous = dict(pmi_raw["previous"]) if pmi_raw.get("previous") else None
    current = dict(raw["social_financing_current"])
    previous = dict(raw["social_financing_previous"])
    current_rows = [dict(row) for row in current["rows"]]
    previous_rows = [dict(row) for row in previous["rows"]]

    latest_month = str(current["latest_month"])
    month_number = int(latest_month[-2:])
    if len(current_rows) != month_number:
        raise RuntimeError(
            f"当年社融有效行数与最新月份不一致: rows={len(current_rows)}, latest={latest_month}"
        )
    if len(previous_rows) != 12:
        raise RuntimeError(f"上年社融应有 12 个已发布月份，实际 {len(previous_rows)}")
    comparison_month = f"{effective_now.year - 1:04d}-{month_number:02d}"
    previous_same = next(
        (row for row in previous_rows if row["month"] == comparison_month), None
    )
    if previous_same is None:
        raise RuntimeError(f"上年社融缺少同期月份: {comparison_month}")

    current_ytd = _sum_social(current_rows, latest_month)
    previous_ytd = _sum_social(previous_rows, comparison_month)
    pmi_lag = _month_lag(str(pmi_latest["period"]), effective_now)
    if pmi_lag < 0 or pmi_lag > 1:
        raise RuntimeError(
            f"国家统计局最新 PMI 期间异常或过旧: {pmi_latest['period']} (lag={pmi_lag})"
        )
    social_lag = _month_lag(latest_month, effective_now)
    if social_lag < 0 or social_lag > 2:
        raise RuntimeError(f"人民银行最新社融期间异常或过旧: {latest_month} (lag={social_lag})")

    pmi = dict(pmi_latest)
    pmi["previous"] = pmi_previous
    pmi["change_from_previous"] = (
        {
            name: round(float(pmi_latest[name]) - float(pmi_previous[name]), 4)
            for name in (
                "manufacturing_pmi",
                "non_manufacturing_pmi",
                "composite_pmi",
                "pmi_large",
                "pmi_medium",
                "pmi_small",
            )
            if pmi_latest.get(name) is not None and pmi_previous.get(name) is not None
        }
        if pmi_previous
        else None
    )

    social = {
        "unit": "亿元人民币",
        "latest_month": latest_month,
        "latest": current_rows[-1],
        "latest_previous_year": previous_same,
        "year_to_date": current_ytd,
        "previous_year_to_date": previous_ytd,
        "year_over_year_pct": _percent_change(
            current_rows[-1]["afre_total"], previous_same["afre_total"], "社融单月同比"
        ),
        "ytd_year_over_year_pct": _percent_change(
            current_ytd["afre_total"], previous_ytd["afre_total"], "社融累计同比"
        ),
        "current_year_rows": current_rows,
        "previous_year_rows": previous_rows,
        "published_at": current["published_at"],
        "source_url": current["source_url"],
        "previous_year_source_url": previous["source_url"],
    }

    generated_at = str(raw.get("fetched_at") or effective_now.isoformat(timespec="seconds"))
    sources = {
        "recipe": "a-stock-data v3.7.1 §11",
        "national_bureau_of_statistics": {
            "name": "国家统计局",
            "index_url": pmi_raw["index_url"],
            "latest_release_url": pmi_latest["source_url"],
            "previous_release_url": pmi_previous.get("source_url") if pmi_previous else None,
            "fetched_at": pmi_raw["fetched_at"],
        },
        "peoples_bank_of_china": {
            "name": "中国人民银行",
            "index_url": current["provenance"]["index_url"],
            "current_year_workbook_url": current["source_url"],
            "previous_year_workbook_url": previous["source_url"],
            "fetched_at": current["fetched_at"],
        },
    }
    provenance = {
        "pmi": {
            "index_url": pmi_raw["index_url"],
            "latest": {
                "source_url": pmi_latest["source_url"],
                "published_at": pmi_latest["published_at"],
            },
            "previous": (
                {
                    "source_url": pmi_previous["source_url"],
                    "published_at": pmi_previous["published_at"],
                }
                if pmi_previous
                else None
            ),
        },
        "social_financing": {
            "current_year": current["provenance"],
            "previous_year": previous["provenance"],
            "current_published_at": current["published_at"],
            "previous_published_at": previous["published_at"],
        },
    }
    validation = {
        "passed": True,
        "official_sources_only": True,
        "pmi_period": pmi_latest["period"],
        "pmi_month_lag": pmi_lag,
        "social_financing_latest_month": latest_month,
        "social_financing_month_lag": social_lag,
        "social_financing_unpublished_months_omitted": True,
        "current_year_published_month_count": len(current_rows),
        "previous_year_published_month_count": len(previous_rows),
    }
    snapshot = {
        "ok": True,
        "schema_version": "macro-snapshot.v1",
        "generated_at": generated_at,
        "as_of": effective_now.isoformat(timespec="seconds"),
        "pmi": pmi,
        "social_financing": social,
        "sources": sources,
        "provenance": provenance,
        "validation": validation,
    }
    # Refuse NaN/Infinity before callers atomically replace a known-good file.
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    return snapshot


def atomic_write_json(path: Path, payload: Mapping[str, Any], compact: bool = False) -> None:
    """Write JSON through fsync + os.replace so readers never see a partial file."""
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":") if compact else None,
                indent=None if compact else 2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取国家统计局 PMI 与人民银行社融官方快照")
    parser.add_argument("--output", type=Path, default=Path("data/macro-snapshot.json"))
    parser.add_argument("--as-of", help="北京时间 ISO 时间；默认当前时间")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None, session: Optional[Any] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        effective_now = _coerce_now(args.as_of)
        snapshot = build_snapshot(as_of=effective_now, session=session)
        atomic_write_json(args.output, snapshot, compact=args.compact)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "generated_at": snapshot["generated_at"],
                "pmi_period": snapshot["pmi"]["period"],
                "social_financing_latest_month": snapshot["social_financing"]["latest_month"],
                "output": str(Path(args.output).expanduser().resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
