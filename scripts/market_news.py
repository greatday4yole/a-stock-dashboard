#!/usr/bin/env python3
"""Resilient, key-free, whole-market financial news feed.

The module combines seven independent public sources that act as primary/backup
lines for each other::

* CLS telegraph (A-share focused, locally signed, no credential)
* Eastmoney 7x24 global financial news
* Jin10 flash (金十数据, 2026-10-20 实测可达的国产快讯备援源)
* THS push news (同花顺快讯, 2026-10-20 实测可达的国产快讯备援源)
* Google News RSS search (global equities and macro, explicit three-day query)
* Wall Street CN 7x24 lives
* MarketWatch top-stories RSS

Network access is deliberately injectable.  Passing ``fetch=...`` lets callers and
tests return either a JSON mapping or a ``requests.Response``-like object without
touching the network.  Production Eastmoney requests use a process-wide serial rate
limiter and a reused HTTP session; injected fetchers are not delayed unless a limiter
is explicitly supplied.

This module does not read environment variables, API keys, portfolio files, or place
orders.  Its aggregate result always states whether the feed is healthy, degraded, or
unavailable so callers cannot mistake a source failure for an empty news day.  This is
a bounded discovery feed, not a completeness guarantee: ranked sources can omit or
delay stories.
"""

from __future__ import annotations

import ast
import hashlib
import html
import json
import random
import re
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Protocol, Sequence, Union
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests


CN_TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = "market-news.v1"
CLS_SOURCE_ID = "cls"
EASTMONEY_SOURCE_ID = "eastmoney"
JIN10_SOURCE_ID = "jin10"
THS_NEWS_SOURCE_ID = "ths_news"
GOOGLE_NEWS_SOURCE_ID = "google_news"
WSCN_SOURCE_ID = "wallstreetcn"
MARKETWATCH_SOURCE_ID = "marketwatch"
SOURCE_NAMES = {
    CLS_SOURCE_ID: "财联社",
    EASTMONEY_SOURCE_ID: "东方财富",
    JIN10_SOURCE_ID: "金十数据",
    THS_NEWS_SOURCE_ID: "同花顺快讯",
    GOOGLE_NEWS_SOURCE_ID: "Google 新闻",
    WSCN_SOURCE_ID: "华尔街见闻",
    MARKETWATCH_SOURCE_ID: "MarketWatch",
}
# 优先级数值越小越优先；cls 始终保持最高优先级，其余为互为备份的独立源。
SOURCE_PRIORITY = {
    CLS_SOURCE_ID: 0,
    EASTMONEY_SOURCE_ID: 1,
    JIN10_SOURCE_ID: 2,
    THS_NEWS_SOURCE_ID: 3,
    GOOGLE_NEWS_SOURCE_ID: 4,
    WSCN_SOURCE_ID: 5,
    MARKETWATCH_SOURCE_ID: 6,
}

CLS_URL = "https://www.cls.cn/v1/roll/get_roll_list"
EASTMONEY_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
WSCN_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
# 金十数据闪讯（2026-10-20 实测可达）：channel=-8200 为全频道快讯，vip=1 才带
# VIP/PLUS 锁定标记。需要 x-app-id / x-version / Referer 三个附加请求头，缺失会被拒。
JIN10_FLASH_URL = "https://flash-api.jin10.com/get_flash_list"
JIN10_APP_ID = "bVBF4FyRTn5NJF5n"
JIN10_APP_VERSION = "1.0.0"
JIN10_CHANNEL = "-8200"
# 同花顺快讯（2026-10-20 实测可达）：分页 push 接口，需要 Referer 反爬头。
THS_NEWS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
# MarketWatch 顶条 RSS：news.google.com 在部分网络不可达（连接超时）时，
# 全球市场新闻仍可由该源兜底（2026-10-11 实测可达，Dow Jones 托管）。
MARKETWATCH_RSS_URL = "https://feeds.content.dowjones.io/public/rss/mw_topstories"
GOOGLE_NEWS_QUERY = (
    '("stock market" OR "equity markets" OR "Federal Reserve" OR FOMC '
    'OR Iran OR "Treasury yield" OR "SK Hynix" OR "FTSE China A50" '
    'OR "index futures expiry" OR "bond market" OR "oil prices" '
    'OR "foreign exchange") when:3d'
)
TREASURY_YIELD_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
NAVER_HYNIX_URL = "https://api.finance.naver.com/siseJson.naver"
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
CFFEX_RULE_URL = "http://www.cffex.com.cn/hs300/"
INTELLIGENCE_SCHEMA_VERSION = "market-intelligence.v1"
INTELLIGENCE_TOPICS = (
    "federal_reserve",
    "us_iran_conflict",
    "us_treasury_10y",
    "sk_hynix",
    "a50",
    "index_futures_expiry",
)
NEWS_WINDOW = timedelta(days=3)
MAX_XML_BYTES = 2_000_000
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# The skill's Eastmoney rule is >=1 second plus jitter, serial, with session reuse.
# A dashboard should additionally cache the aggregate feed.
EASTMONEY_MIN_INTERVAL_SECONDS = 1.0
EASTMONEY_JITTER_SECONDS = (0.1, 0.5)
NEAR_DUPLICATE_SIMILARITY = 0.90
NEAR_DUPLICATE_MAX_SECONDS = 10 * 60
TREASURY_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
HYNIX_MAX_AGE_SECONDS = 5 * 24 * 60 * 60
A50_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


class ResponseLike(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...

    @property
    def content(self) -> bytes: ...

    @property
    def text(self) -> str: ...


Fetch = Callable[..., Union[Mapping[str, Any], ResponseLike, str, bytes]]


class NewsSourceError(RuntimeError):
    """A source responded, but its status or payload contract was invalid."""


class SerialRateLimiter:
    """Thread-safe minimum-interval gate using a monotonic clock."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        if min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        self.min_interval = float(min_interval)
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter or (lambda: 0.0)
        self._last_call: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Wait until one request slot is available, then reserve it."""

        with self._lock:
            now = self._clock()
            if self._last_call is not None:
                delay = self.min_interval - (now - self._last_call)
                if delay > 0:
                    self._sleep(delay + max(0.0, float(self._jitter())))
                    now = self._clock()
            self._last_call = now


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retries = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            # 403/429 are source-side throttling signals.  Do not amplify them with an
            # immediate HTTP retry; return control to the server circuit breaker.
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(("GET",)),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    except (ImportError, TypeError):
        # Compatibility fallback for old urllib3 versions.  Session reuse and the
        # explicit Eastmoney rate limiter remain active.
        pass
    return session


_SESSION = _build_session()
_EASTMONEY_LIMITER = SerialRateLimiter(
    EASTMONEY_MIN_INTERVAL_SECONDS,
    jitter=lambda: random.uniform(*EASTMONEY_JITTER_SECONDS),
)
# 金十 / 同花顺两个新增快讯源不做严格串行，但各自保持最小请求间隔，
# 避免聚合层高频刷新时对同一上游造成突发流量（2026-10-20 实测可达）。
FLASH_MIN_INTERVAL_SECONDS = 0.5
_JIN10_LIMITER = SerialRateLimiter(FLASH_MIN_INTERVAL_SECONDS)
_THS_NEWS_LIMITER = SerialRateLimiter(FLASH_MIN_INTERVAL_SECONDS)


def _default_fetch(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
) -> ResponseLike:
    return _SESSION.get(url, params=params, headers=headers, timeout=timeout)


def _json_request(
    fetch: Fetch,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> Mapping[str, Any]:
    response = fetch(url, params=params, headers=headers, timeout=timeout)
    if isinstance(response, Mapping):
        payload = response
    else:
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, Mapping):
        raise NewsSourceError(f"JSON root must be an object, got {type(payload).__name__}")
    return payload


def _xml_request(
    fetch: Fetch,
    url: str,
    *,
    params: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> ElementTree.Element:
    """Fetch a bounded XML document and fail closed on transport/parse errors."""

    response = fetch(url, params=params, headers=headers, timeout=timeout)
    if isinstance(response, Mapping):
        raise NewsSourceError("XML response must not be a JSON object")
    if isinstance(response, bytes):
        payload = response
    elif isinstance(response, str):
        payload = response.encode("utf-8")
    else:
        response.raise_for_status()
        content = getattr(response, "content", b"")
        if isinstance(content, bytes) and content:
            payload = content
        else:
            payload = str(getattr(response, "text", "") or "").encode("utf-8")
    if not payload:
        raise NewsSourceError("XML response is empty")
    if len(payload) > MAX_XML_BYTES:
        raise NewsSourceError(f"XML response exceeds {MAX_XML_BYTES} bytes")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise NewsSourceError(f"XML parse failed: {error}") from error


def _text_request(
    fetch: Fetch,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
    max_bytes: int = MAX_XML_BYTES,
) -> str:
    response = fetch(url, params=params or {}, headers=headers or {}, timeout=timeout)
    if isinstance(response, Mapping):
        return json.dumps(response, ensure_ascii=False)
    if isinstance(response, bytes):
        payload = response
    elif isinstance(response, str):
        payload = response.encode("utf-8")
    else:
        response.raise_for_status()
        content = getattr(response, "content", b"")
        payload = content if isinstance(content, bytes) and content else str(
            getattr(response, "text", "") or ""
        ).encode("utf-8")
    if not payload:
        raise NewsSourceError("text response is empty")
    if len(payload) > max_bytes:
        raise NewsSourceError(f"text response exceeds {max_bytes} bytes")
    return payload.decode("utf-8-sig", errors="replace")


def _clean_text(value: Any, *, limit: int = 2_000) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", " ", text)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\s\u3000\xa0]+", " ", text).strip()
    return text[:limit]


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime_time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1_000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        candidate = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            parsed = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y%m%d%H%M%S",
                "%Y-%m-%d",
            ):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                try:
                    parsed = parsedate_to_datetime(raw)
                except (TypeError, ValueError, OverflowError):
                    parsed = None
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _iso_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def _unix_seconds_to_local_string(value: Any) -> str:
    """把 unix 秒级时间戳转成项目统一的 ``%Y-%m-%d %H:%M:%S``（东八区）字符串。

    同花顺快讯的 ``ctime`` / ``rtime`` 是字符串形式的 unix 秒（例如 ``"1792475149"``），
    ``_parse_datetime`` 只接受数值型的秒级时间戳，因此这里先做一次显式转换，
    转换失败返回空串，交由上层按"时间缺失"处理（不猜测、不臆造时间）。
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        try:
            seconds = int(float(raw))
        except (TypeError, ValueError):
            return ""
    try:
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(CN_TZ)
    except (OSError, OverflowError, ValueError):
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _observed_at(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(CN_TZ)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=CN_TZ)
    return observed.astimezone(CN_TZ)


def _filter_time_window(
    items: Sequence[Mapping[str, Any]],
    *,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], int, int]:
    """Keep only timestamped records in the inclusive trailing three-day window."""

    window_start = window_end - NEWS_WINDOW
    kept: list[dict[str, Any]] = []
    invalid_time = 0
    outside_window = 0
    for item in items:
        published = _parse_datetime(item.get("published_at"))
        if published is None:
            invalid_time += 1
            continue
        if not window_start <= published <= window_end:
            outside_window += 1
            continue
        normalized = dict(item)
        normalized["published_at"] = published.isoformat(timespec="seconds")
        kept.append(normalized)
    return kept, invalid_time, outside_window


def _first_text(item: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        text = _clean_text(item.get(key))
        if text:
            return text
    return ""


def _first_url(item: Mapping[str, Any]) -> str:
    value = _first_text(
        item,
        ("url", "uri", "shareurl", "share_url", "shareUrl", "newsUrl", "wap_url", "web_url"),
    )
    return value if value.startswith(("https://", "http://")) else ""


def _market_suffix(code: str, market_hint: Any = "") -> str:
    hint = str(market_hint or "").strip().upper()
    aliases = {
        "1": "SH",
        "SH": "SH",
        "SSE": "SH",
        "SHANGHAI": "SH",
        "0": "SZ",
        "SZ": "SZ",
        "SZSE": "SZ",
        "SHENZHEN": "SZ",
        "B": "BJ",
        "BJ": "BJ",
        "BSE": "BJ",
        "BEIJING": "BJ",
    }
    if hint in aliases:
        hinted = aliases[hint]
        # Eastmoney uses market 0 for both Shenzhen and Beijing.  The unambiguous
        # Beijing number ranges must win over that shared transport-level code.
        if hinted == "SZ" and (code.startswith("92") or code[:2] in ("43", "83", "87")):
            return "BJ"
        return hinted
    if code.startswith("92") or code[:2] in ("43", "83", "87"):
        return "BJ"
    return "SH" if code.startswith(("5", "6", "9")) else "SZ"


def _normalize_symbol(value: Any, market_hint: Any = "") -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    match = re.fullmatch(r"(SH|SZ|BJ)[.\-:]?(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = re.fullmatch(r"(\d{6})[.\-:]?(SH|SZ|BJ)", raw)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.fullmatch(r"([01])\.(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{_market_suffix(match.group(2), match.group(1))}"
    if re.fullmatch(r"\d{6}", raw):
        return f"{raw}.{_market_suffix(raw, market_hint)}"
    return ""


def _mapping_value(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value not in (None, ""):
            return value
    return ""


def _symbols_from_value(value: Any, market_hint: Any = "") -> set[str]:
    symbols: set[str] = set()
    if value in (None, ""):
        return symbols
    if isinstance(value, Mapping):
        hint = _mapping_value(
            value,
            ("market", "exchange", "market_code", "marketCode", "stock_type", "stockType"),
        ) or market_hint
        candidate = _mapping_value(
            value,
            (
                "thscode",
                "symbol",
                "stock_code",
                "stockCode",
                "security_code",
                "securityCode",
                "sec_code",
                "secCode",
                "stock_id",
                "stockId",
                "code",
                "f12",
            ),
        )
        normalized = _normalize_symbol(candidate, hint)
        if normalized:
            symbols.add(normalized)
            return symbols
        # Some payload variants use the security code as a mapping key.
        for key in value:
            normalized = _normalize_symbol(key, hint)
            if normalized:
                symbols.add(normalized)
        return symbols
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for entry in value:
            symbols.update(_symbols_from_value(entry, market_hint))
        return symbols

    raw = str(value).strip()
    if raw.startswith(("[", "{")):
        try:
            return _symbols_from_value(json.loads(raw), market_hint)
        except (json.JSONDecodeError, TypeError):
            pass
    normalized = _normalize_symbol(raw, market_hint)
    if normalized:
        symbols.add(normalized)
        return symbols
    for token in re.split(r"[,;|\s]+", raw):
        normalized = _normalize_symbol(token, market_hint)
        if normalized:
            symbols.add(normalized)
    return symbols


def _extract_symbols(item: Mapping[str, Any]) -> list[str]:
    values = []
    for key in ("stock_list", "stockList", "symbols"):
        if key in item:
            values.append(item.get(key))
    symbols: set[str] = set()
    for value in values:
        symbols.update(_symbols_from_value(value))
    return sorted(symbols)


def _normalize_item(
    item: Mapping[str, Any],
    *,
    source_id: str,
    title_keys: Sequence[str],
    summary_keys: Sequence[str],
    time_keys: Sequence[str],
    source_item_id_keys: Sequence[str],
) -> dict[str, Any] | None:
    title = _first_text(item, title_keys)
    summary = _first_text(item, summary_keys)
    if not title:
        title = summary[:160]
    if not title:
        return None
    published_at = ""
    for key in time_keys:
        published_at = _iso_datetime(item.get(key))
        if published_at:
            break
    raw_id = _first_text(item, source_item_id_keys)
    return {
        "scope": "market",
        "title": title,
        "summary": summary,
        "published_at": published_at,
        "source_id": source_id,
        "source": SOURCE_NAMES[source_id],
        "source_kind": "wire",
        "source_item_id": raw_id,
        "symbols": _extract_symbols(item),
        "url": _first_url(item),
    }


RISK_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "regulatory",
        ("立案", "被调查", "证监会调查", "处罚", "监管", "问询函", "警示函", "退市", "停牌核查"),
    ),
    (
        "earnings_downside",
        ("预亏", "亏损", "下修", "不及预期", "业绩下降", "计提减值", "爆雷", "暴雷"),
    ),
    (
        "shareholder_pressure",
        ("减持", "解禁", "质押", "平仓", "清仓"),
    ),
    (
        "credit_stress",
        ("违约", "破产", "债务逾期", "流动性危机", "无法偿付"),
    ),
    (
        "geopolitical",
        ("制裁", "关税", "冲突", "战争", "袭击", "地缘风险"),
    ),
    (
        "market_stress",
        ("暴跌", "大跌", "跌停", "闪崩", "熔断", "抛售"),
    ),
    (
        "tightening",
        ("加息", "提高利率", "收紧流动性", "缩表"),
    ),
)

OPPORTUNITY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "policy_support",
        ("降准", "降息", "政策支持", "财政刺激", "补贴", "专项债", "稳增长"),
    ),
    (
        "earnings_upside",
        ("预增", "扭亏", "超预期", "业绩增长", "利润增长", "创历史新高"),
    ),
    (
        "orders_and_contracts",
        ("中标", "订单", "签约", "重大合同", "战略合作"),
    ),
    (
        "shareholder_support",
        ("回购", "增持", "提高持股"),
    ),
    (
        "innovation",
        ("技术突破", "获批", "量产", "商业化", "首发", "国产替代"),
    ),
    (
        "capital_inflow",
        ("净流入", "净买入", "资金流入", "外资加仓"),
    ),
)


def classify_keywords(title: str, summary: str = "") -> tuple[list[str], list[str]]:
    """Return deterministic risk and opportunity category tags.

    Tags denote keyword evidence, not a trading conclusion.  A story can validly carry
    both kinds (for example, a policy response to a market sell-off).
    """

    text = f"{_clean_text(title)} {_clean_text(summary)}".casefold()
    risk = [tag for tag, words in RISK_KEYWORDS if any(word.casefold() in text for word in words)]
    opportunity = [
        tag for tag, words in OPPORTUNITY_KEYWORDS if any(word.casefold() in text for word in words)
    ]
    return risk, opportunity


def _canonical_text(value: str) -> str:
    text = _clean_text(value).casefold()
    text = re.sub(r"^【?(?:快讯|突发|更新)】?", "", text)
    text = re.sub(r"^财联社\d{1,2}月\d{1,2}日电", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _dedupe_key(item: Mapping[str, Any]) -> str:
    title = _canonical_text(str(item.get("title") or ""))
    if not title:
        title = _canonical_text(str(item.get("summary") or ""))[:120]
    # The same generic headline on different days is a new event.  Independent sources
    # reporting one event within a day still collapse to one record.
    day = str(item.get("published_at") or "")[:10]
    return f"{day}|{title}"


def _item_symbols(item: Mapping[str, Any]) -> set[str]:
    values = item.get("symbols") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        return set()
    return {str(value) for value in values if str(value or "").strip()}


def _are_exact_duplicates(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if _dedupe_key(first) != _dedupe_key(second) or _dedupe_key(first) == "|":
        return False
    first_symbols, second_symbols = _item_symbols(first), _item_symbols(second)
    # Identical generic headlines tied to two explicitly different companies are not
    # the same event.  Missing association data does not block the legacy exact match.
    return not (first_symbols and second_symbols and first_symbols.isdisjoint(second_symbols))


def _are_near_duplicates(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if str(first.get("source_id") or "") == str(second.get("source_id") or ""):
        return False
    first_symbols, second_symbols = _item_symbols(first), _item_symbols(second)
    if not first_symbols or not second_symbols or first_symbols.isdisjoint(second_symbols):
        return False
    first_time = _parse_datetime(first.get("published_at"))
    second_time = _parse_datetime(second.get("published_at"))
    if first_time is None or second_time is None:
        return False
    if abs((first_time - second_time).total_seconds()) > NEAR_DUPLICATE_MAX_SECONDS:
        return False
    first_title = _canonical_text(str(first.get("title") or ""))
    second_title = _canonical_text(str(second.get("title") or ""))
    if not first_title or not second_title or first_title == second_title:
        return False
    similarity = SequenceMatcher(None, first_title, second_title, autojunk=False).ratio()
    return similarity >= NEAR_DUPLICATE_SIMILARITY


def _item_order_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _dedupe_key(item),
        SOURCE_PRIORITY.get(str(item.get("source_id") or ""), 99),
        str(item.get("source_item_id") or ""),
        str(item.get("published_at") or ""),
        str(item.get("title") or ""),
        str(item.get("summary") or ""),
        tuple(sorted(_item_symbols(item))),
        str(item.get("url") or ""),
    )


def _duplicate_components(items: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted((dict(item) for item in items), key=_item_order_key)
    parent = list(range(len(ordered)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    for first_index, first in enumerate(ordered):
        for second_index in range(first_index + 1, len(ordered)):
            second = ordered[second_index]
            if _are_exact_duplicates(first, second) or _are_near_duplicates(first, second):
                union(first_index, second_index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(ordered):
        groups.setdefault(find(index), []).append(item)
    return [groups[root] for root in sorted(groups)]


def _choice_key(item: Mapping[str, Any], field: str) -> tuple[Any, ...]:
    value = str(item.get(field) or "")
    source_id = str(item.get("source_id") or "")
    return (-len(value), SOURCE_PRIORITY.get(source_id, 99), value, source_id)


def deduplicate_news(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically merge exact and tightly constrained cross-source duplicates."""

    merged: list[dict[str, Any]] = []
    for group in _duplicate_components(items):
        identity_candidates = sorted({_dedupe_key(item) for item in group if _dedupe_key(item) != "|"})
        if not identity_candidates:
            continue
        identity = identity_candidates[0]
        title_item = min(group, key=lambda item: _choice_key(item, "title"))
        summary_item = min(group, key=lambda item: _choice_key(item, "summary"))
        url_item = min(group, key=lambda item: _choice_key(item, "url"))
        published_values = sorted(
            (str(item.get("published_at") or "") for item in group if item.get("published_at")),
            reverse=True,
        )
        source_ids = sorted(
            {str(item.get("source_id") or "") for item in group if item.get("source_id")},
            key=lambda value: (SOURCE_PRIORITY.get(value, 99), value),
        )
        source_names = sorted(
            {
                (
                    SOURCE_PRIORITY.get(str(item.get("source_id") or ""), 99),
                    str(item.get("source_id") or ""),
                    str(item.get("source") or "").strip(),
                )
                for item in group
                if str(item.get("source") or "").strip()
            }
        )
        sources = list(dict.fromkeys(name for _, _, name in source_names))
        if not sources:
            sources = [SOURCE_NAMES.get(value, value) for value in source_ids]
        source_kinds = sorted(
            {str(item.get("source_kind") or "wire") for item in group if item.get("source_kind")}
        )
        source_item_ids = sorted(
            {str(item.get("source_item_id") or "") for item in group if item.get("source_item_id")}
        )
        publisher_urls = sorted(
            {
                str(item.get("publisher_url") or "").strip()
                for item in group
                if str(item.get("publisher_url") or "").startswith(("https://", "http://"))
            }
        )
        publishers = sorted(
            {
                str(item.get("source") or "").strip()
                for item in group
                if str(item.get("publisher_url") or "").startswith(("https://", "http://"))
                and str(item.get("source") or "").strip()
            }
        )
        symbols = sorted(set().union(*(_item_symbols(item) for item in group)))
        nonempty_symbol_sets = [_item_symbols(item) for item in group if _item_symbols(item)]
        identity_symbols = (
            sorted(set.intersection(*nonempty_symbol_sets)) if nonempty_symbol_sets else []
        )
        combined_text = " ".join(
            f"{item.get('title', '')} {item.get('summary', '')}" for item in group
        )
        risk_tags, opportunity_tags = classify_keywords(combined_text)
        id_seed = f"market-news|{identity}|{','.join(identity_symbols)}"
        digest = hashlib.sha256(id_seed.encode("utf-8")).hexdigest()[:20]
        merged_item = {
            "id": digest,
            "scope": "market",
            "title": str(title_item.get("title") or ""),
            "summary": str(summary_item.get("summary") or ""),
            "published_at": published_values[0] if published_values else "",
            "source_id": source_ids[0] if len(source_ids) == 1 else "multi",
            "source": " / ".join(sources),
            "source_ids": source_ids,
            "sources": sources,
            "source_kind": source_kinds[0] if len(source_kinds) == 1 else "mixed",
            "source_item_ids": source_item_ids,
            "publisher_urls": publisher_urls,
            "publishers": publishers,
            "symbols": symbols,
            "url": str(url_item.get("url") or ""),
            "risk_tags": risk_tags,
            "opportunity_tags": opportunity_tags,
        }
        # 付费墙标记：金十等源的锁定条目正文不完整，合并后仍需保留该标记，
        # 让下游能识别"不完整正文"，不要把它当成完整新闻消费。
        if any(bool(item.get("locked")) for item in group):
            merged_item["locked"] = True
        merged.append(merged_item)
    return sorted(merged, key=lambda item: (str(item["published_at"]), item["id"]), reverse=True)


def _page_size(value: int) -> int:
    if isinstance(value, bool):
        raise ValueError("page_size must be an integer between 1 and 100")
    size = int(value)
    if not 1 <= size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    return size


def _cls_params(page_size: int) -> dict[str, str]:
    base = {
        "appName": "CailianpressWeb",
        "last_time": "",
        "os": "web",
        "refresh_type": "1",
        "rn": str(_page_size(page_size)),
        "sv": "7.7.5",
    }
    query = "&".join(f"{key}={base[key]}" for key in sorted(base))
    sha1_hex = hashlib.sha1(query.encode("utf-8")).hexdigest()
    base["sign"] = hashlib.md5(sha1_hex.encode("utf-8")).hexdigest()
    return base


def fetch_cls_telegraph(page_size: int = 50, *, fetch: Fetch | None = None) -> list[dict[str, Any]]:
    """Fetch and normalize CLS whole-market telegraph records."""

    payload = _json_request(
        fetch or _default_fetch,
        CLS_URL,
        params=_cls_params(page_size),
        headers={"User-Agent": USER_AGENT, "Referer": "https://www.cls.cn/"},
        timeout=10,
    )
    errno = payload.get("errno")
    if isinstance(errno, bool) or errno != 0:
        raise NewsSourceError(f"CLS rejected request: errno={errno!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("roll_data"), list):
        raise NewsSourceError("CLS payload missing data.roll_data list")
    rows: list[dict[str, Any]] = []
    for raw in data["roll_data"]:
        if not isinstance(raw, Mapping):
            continue
        normalized = _normalize_item(
            raw,
            source_id=CLS_SOURCE_ID,
            title_keys=("title", "brief", "content"),
            summary_keys=("content", "brief", "title"),
            time_keys=("ctime", "showTime", "time"),
            source_item_id_keys=("id", "telegraph_id", "news_id", "art_code", "infoCode"),
        )
        if normalized:
            rows.append(normalized)
    return rows


def fetch_eastmoney_global_news(
    page_size: int = 50,
    *,
    fetch: Fetch | None = None,
    limiter: SerialRateLimiter | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize Eastmoney 7x24 records.

    Real HTTP calls always use the process-wide limiter.  An injected fetcher is
    presumed offline and is not delayed; pass ``limiter=...`` to test or enforce a
    custom gate explicitly.
    """

    request_fetch = fetch or _default_fetch
    request_limiter = limiter if limiter is not None else (_EASTMONEY_LIMITER if fetch is None else None)
    if request_limiter is not None:
        request_limiter.wait()
    payload = _json_request(
        request_fetch,
        EASTMONEY_URL,
        params={
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(_page_size(page_size)),
            "req_trace": str(uuid.uuid4()),
        },
        headers={"User-Agent": USER_AGENT, "Referer": "https://kuaixun.eastmoney.com/"},
        timeout=10,
    )
    code = payload.get("code")
    if code is not None and (isinstance(code, bool) or code not in (1, "1")):
        raise NewsSourceError(f"Eastmoney rejected request: code={code!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("fastNewsList"), list):
        raise NewsSourceError("Eastmoney payload missing data.fastNewsList list")
    rows: list[dict[str, Any]] = []
    for raw in data["fastNewsList"]:
        if not isinstance(raw, Mapping):
            continue
        normalized = _normalize_item(
            raw,
            source_id=EASTMONEY_SOURCE_ID,
            title_keys=("title", "summary", "content"),
            summary_keys=("summary", "content", "title"),
            time_keys=("showTime", "time", "publishTime"),
            source_item_id_keys=("code", "news_id", "id", "art_code", "infoCode"),
        )
        if normalized:
            rows.append(normalized)
    return rows


def fetch_jin10_flash(
    page_size: int = 50,
    *,
    fetch: Fetch | None = None,
    limiter: SerialRateLimiter | None = None,
) -> list[dict[str, Any]]:
    """抓取并归一化金十数据（金十快讯）闪讯记录。

    数据来源：``https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1``
    （2026-10-20 实测可达，作为财联社/东财之外的国产快讯备援源）。

    特殊请求头：该接口校验 ``x-app-id`` / ``x-version`` / ``Referer``，缺一会直接被拒。

    返回结构：``{"status": 200, "message": "OK", "data": [ {...}, ... ]}``，
    ``data`` 为数组，单条记录的正文在嵌套的 ``data.content``，标题为 ``data.title``，
    时间为本地时间字符串 ``time``（``YYYY-MM-DD HH:MM:SS``），``id`` 可作
    ``source_item_id``。

    已知限制：VIP/PLUS 专享快讯带 ``data.lock = true``，此时 ``data.content`` 为空，
    正文只保留在 ``data.vip_title``（甚至仅有 ``data.vip_desc`` 提示语），属于付费墙内容。
    这类条目按 ``data.title`` → ``data.vip_title`` → ``data.content`` 的顺序回退取标题，
    并在归一化结果上打 ``locked`` 标记，供下游识别为"不完整正文"，不要当成完整新闻。
    ``remark`` 中的锁只锁住"相关链接"，只有在正文也为空时才判定为锁定条目。
    """

    request_fetch = fetch or _default_fetch
    request_limiter = limiter if limiter is not None else (_JIN10_LIMITER if fetch is None else None)
    if request_limiter is not None:
        request_limiter.wait()
    payload = _json_request(
        request_fetch,
        JIN10_FLASH_URL,
        params={"channel": JIN10_CHANNEL, "vip": "1"},
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.jin10.com/",
            "x-app-id": JIN10_APP_ID,
            "x-version": JIN10_APP_VERSION,
        },
        timeout=10,
    )
    status = payload.get("status")
    if status is not None and (isinstance(status, bool) or str(status) != "200"):
        raise NewsSourceError(f"Jin10 rejected request: status={status!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise NewsSourceError("Jin10 payload missing data list")
    rows: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, Mapping):
            continue
        inner = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
        remark = raw.get("remark")
        remark_rows = remark if isinstance(remark, list) else []
        # 付费墙判定：正文被锁（data.lock）即为锁定条目；
        # remark 里的锁只锁住"相关链接"，只有在正文本身也为空时才说明本条内容不完整。
        content = inner.get("content")
        locked = bool(inner.get("lock")) or (
            not content
            and any(
                isinstance(entry, Mapping) and bool(entry.get("lock"))
                for entry in remark_rows
            )
        )
        flat = {
            "id": raw.get("id"),
            # 标题回退链：正文标题 → VIP 标题 → 正文内容。
            "title": inner.get("title"),
            "vip_title": inner.get("vip_title"),
            "content": content,
            "vip_desc": inner.get("vip_desc"),
            "time": raw.get("time"),
            "url": inner.get("link") or inner.get("source_link"),
        }
        normalized = _normalize_item(
            flat,
            source_id=JIN10_SOURCE_ID,
            title_keys=("title", "vip_title", "content"),
            summary_keys=("content", "vip_title", "vip_desc"),
            time_keys=("time",),
            source_item_id_keys=("id",),
        )
        if normalized:
            # 付费墙标记：下游据此识别"正文不完整"，避免当成完整新闻消费。
            normalized["locked"] = locked
            rows.append(normalized)
    return rows


def fetch_ths_news(
    page_size: int = 50,
    *,
    fetch: Fetch | None = None,
    limiter: SerialRateLimiter | None = None,
) -> list[dict[str, Any]]:
    """抓取并归一化同花顺快讯（THS push news）记录。

    数据来源：``https://news.10jqka.com.cn/tapp/news/push/stock/``
    （2026-10-20 实测可达，作为国产快讯备援源，与金十互为备份）。

    特殊请求头：需要 ``Referer: https://news.10jqka.com.cn/`` 反爬头，缺失会被拒。

    返回结构：``{"code": "200", "msg": "成功", "data": {"list": [ {...}, ... ]}}``，
    ``data.list`` 为数组，单条记录的 ``title`` 为标题、``digest`` 为摘要全文、
    ``url`` 为详情页地址。

    已知限制：``ctime`` / ``rtime`` 是字符串形式的 unix 秒（如 ``"1792475149"``），
    这里统一转成 ``%Y-%m-%d %H:%M:%S``（东八区）后再交给归一化流程；
    ``stock`` 字段多数条目为空，因此个股关联可能缺失。
    """

    request_fetch = fetch or _default_fetch
    request_limiter = limiter if limiter is not None else (_THS_NEWS_LIMITER if fetch is None else None)
    if request_limiter is not None:
        request_limiter.wait()
    payload = _json_request(
        request_fetch,
        THS_NEWS_URL,
        params={
            "page": "1",
            "tag": "",
            "track": "website",
            "pagesize": str(_page_size(page_size)),
        },
        headers={"User-Agent": USER_AGENT, "Referer": "https://news.10jqka.com.cn/"},
        timeout=10,
    )
    code = payload.get("code")
    if code is not None and (isinstance(code, bool) or str(code) != "200"):
        raise NewsSourceError(f"THS news rejected request: code={code!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("list"), list):
        raise NewsSourceError("THS news payload missing data.list list")
    rows: list[dict[str, Any]] = []
    for raw in data["list"]:
        if not isinstance(raw, Mapping):
            continue
        flat = dict(raw)
        # unix 秒字符串 → 项目统一的本地时间字符串。
        flat["ctime"] = _unix_seconds_to_local_string(raw.get("ctime"))
        flat["rtime"] = _unix_seconds_to_local_string(raw.get("rtime"))
        # stock 列表复用现有的个股代码归一化逻辑（多数条目为空）。
        flat["stock_list"] = raw.get("stock") if isinstance(raw.get("stock"), list) else []
        normalized = _normalize_item(
            flat,
            source_id=THS_NEWS_SOURCE_ID,
            title_keys=("title", "digest", "short"),
            summary_keys=("digest", "short", "title"),
            time_keys=("ctime", "rtime"),
            source_item_id_keys=("id", "seq"),
        )
        if normalized:
            rows.append(normalized)
    return rows


def fetch_wallstreetcn_lives(page_size: int = 50, *, fetch: Fetch | None = None) -> list[dict[str, Any]]:
    """Fetch and normalize Wall Street CN (华尔街见闻) 7x24 live records."""

    payload = _json_request(
        fetch or _default_fetch,
        WSCN_URL,
        params={"channel": "global-channel", "limit": str(_page_size(page_size))},
        headers={"User-Agent": USER_AGENT, "Referer": "https://wallstreetcn.com/"},
        timeout=10,
    )
    code = payload.get("code")
    if isinstance(code, bool) or code not in (20000, "20000"):
        raise NewsSourceError(f"Wall Street CN rejected request: code={code!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
        raise NewsSourceError("Wall Street CN payload missing data.items list")
    rows: list[dict[str, Any]] = []
    for raw in data["items"]:
        if not isinstance(raw, Mapping):
            continue
        normalized = _normalize_item(
            raw,
            source_id=WSCN_SOURCE_ID,
            title_keys=("title", "content_text", "content"),
            summary_keys=("content_text", "content", "title"),
            time_keys=("display_time", "updated_at"),
            source_item_id_keys=("id", "uri"),
        )
        if normalized:
            rows.append(normalized)
    return rows


def _xml_child(parent: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in parent:
        if child.tag.rsplit("}", 1)[-1] == name:
            return child
    return None


def _xml_text(parent: ElementTree.Element, name: str) -> str:
    child = _xml_child(parent, name)
    return _clean_text(child.text if child is not None else "")


def fetch_google_global_market_news(
    *,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch Google News RSS results for global markets in the trailing 72 hours.

    The upstream ``when:3d`` clause narrows discovery, while the local inclusive
    ``[now-72h, now]`` filter is authoritative.  Google ranks and caps its RSS search,
    so an empty or short result is not evidence that no relevant news exists.
    """

    observed = _observed_at(now)
    root = _xml_request(
        fetch or _default_fetch,
        GOOGLE_NEWS_URL,
        params={
            "q": GOOGLE_NEWS_QUERY,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
        timeout=10,
    )
    if root.tag.rsplit("}", 1)[-1] != "rss":
        raise NewsSourceError(f"Google News XML root must be rss, got {root.tag!r}")
    channel = _xml_child(root, "channel")
    if channel is None:
        raise NewsSourceError("Google News RSS missing channel")
    if not _xml_text(channel, "title") or not _xml_text(channel, "link"):
        raise NewsSourceError("Google News RSS channel metadata is incomplete")

    raw_items = [child for child in channel if child.tag.rsplit("}", 1)[-1] == "item"]
    normalized_items: list[dict[str, Any]] = []
    structurally_valid = 0
    for raw in raw_items:
        title = _xml_text(raw, "title")
        summary = _xml_text(raw, "description")
        url = _xml_text(raw, "link")
        published_at = _iso_datetime(_xml_text(raw, "pubDate"))
        source_node = _xml_child(raw, "source")
        publisher = _clean_text(source_node.text if source_node is not None else "")
        if not title or not published_at or not publisher or not url.startswith(("https://", "http://")):
            continue
        structurally_valid += 1
        publisher_url = _clean_text(
            source_node.attrib.get("url", "") if source_node is not None else ""
        )
        if not publisher_url.startswith(("https://", "http://")):
            publisher_url = ""
        normalized_items.append(
            {
                "scope": "market",
                "title": title,
                "summary": summary,
                "published_at": published_at,
                "source_id": GOOGLE_NEWS_SOURCE_ID,
                # Preserve the original publisher; source_id records the aggregator.
                "source": publisher,
                "source_kind": "aggregator",
                "source_item_id": _xml_text(raw, "guid") or url,
                "symbols": [],
                "url": url,
                "publisher_url": publisher_url,
            }
        )
    if raw_items and structurally_valid == 0:
        raise NewsSourceError("Google News RSS items do not satisfy the required field contract")
    filtered, _, _ = _filter_time_window(normalized_items, window_end=observed)
    return filtered


def fetch_marketwatch_top_stories(
    *,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch MarketWatch top-stories RSS as the reachable global-market source.

    news.google.com 在部分网络环境连接超时（2026-10-11 实测），MarketWatch
    （Dow Jones 托管 RSS）可达且同为英文全球市场头条，作为第 5 个独立源并行
    采集；Google 恢复可达后两源自然并存去重。
    """

    observed = _observed_at(now)
    root = _xml_request(
        fetch or _default_fetch,
        MARKETWATCH_RSS_URL,
        params={},
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
        timeout=10,
    )
    if root.tag.rsplit("}", 1)[-1] != "rss":
        raise NewsSourceError(f"MarketWatch XML root must be rss, got {root.tag!r}")
    channel = _xml_child(root, "channel")
    if channel is None:
        raise NewsSourceError("MarketWatch RSS missing channel")
    raw_items = [child for child in channel if child.tag.rsplit("}", 1)[-1] == "item"]
    normalized_items: list[dict[str, Any]] = []
    structurally_valid = 0
    for raw in raw_items:
        title = _xml_text(raw, "title")
        summary = re.sub(r"<[^>]+>", "", _xml_text(raw, "description")).strip()
        url = _xml_text(raw, "link")
        published_at = _iso_datetime(_xml_text(raw, "pubDate"))
        guid = _xml_text(raw, "guid")
        if not title or not published_at or not url.startswith(("https://", "http://")):
            continue
        structurally_valid += 1
        normalized_items.append(
            {
                "scope": "market",
                "title": title,
                "summary": summary,
                "published_at": published_at,
                "source_id": MARKETWATCH_SOURCE_ID,
                "source": SOURCE_NAMES[MARKETWATCH_SOURCE_ID],
                "source_kind": "publisher",
                "source_item_id": guid or url,
                "symbols": [],
                "url": url,
                "publisher_url": "https://www.marketwatch.com/",
            }
        )
    if raw_items and structurally_valid == 0:
        raise NewsSourceError("MarketWatch RSS items do not satisfy the required field contract")
    filtered, _, _ = _filter_time_window(normalized_items, window_end=observed)
    return filtered


def _safe_error(error: Exception) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(error)).strip()
    return f"{type(error).__name__}: {message}"[:300]


def _finite_number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _signal_strength(change: float | None, *, medium: float, high: float) -> str:
    absolute = abs(change) if change is not None else 0.0
    return "high" if absolute >= high else "medium" if absolute >= medium else "low"


def _intelligence_freshness(
    evidence_time: Any,
    *,
    now: datetime,
    max_age_seconds: float,
    future_tolerance_seconds: float = 24 * 60 * 60,
) -> dict[str, Any]:
    """Classify evidence recency without treating a successful request as fresh data."""

    parsed = _parse_datetime(evidence_time)
    if parsed is None:
        return {
            "status": "missing",
            "stale": True,
            "age_seconds": None,
            "max_age_seconds": int(max_age_seconds),
        }
    age = (now - parsed).total_seconds()
    if age < -future_tolerance_seconds:
        status = "future"
    elif age > max_age_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "stale": status != "fresh",
        "age_seconds": round(age, 1),
        "max_age_seconds": int(max_age_seconds),
    }


def _intelligence_signal(
    *,
    topic: str,
    label: str,
    observed_at: datetime,
    status: str,
    fact_kind: str,
    direction: str,
    confidence: float,
    relation_score: int,
    signal_strength: str,
    metric: Mapping[str, Any] | None,
    evidence_time: Any,
    source: str,
    source_kind: str,
    evidence: Sequence[Mapping[str, Any]],
    related_themes: Sequence[str],
    threshold: str,
    forecast: str,
    event_time: Any = None,
    limitations: Sequence[str] = (),
    stale: bool = False,
    freshness: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_rows = [dict(row) for row in evidence[:3] if isinstance(row, Mapping)]
    stable_seed = {
        "topic": topic,
        "status": status,
        "direction": direction,
        "metric": dict(metric or {}),
        "evidence_time": _iso_datetime(evidence_time) or str(evidence_time or ""),
        "event_time": _iso_datetime(event_time if event_time is not None else evidence_time)
        or str(event_time if event_time is not None else evidence_time or ""),
        "evidence": [
            (str(row.get("source") or ""), str(row.get("url") or ""), str(row.get("evidence_time") or ""))
            for row in evidence_rows
        ],
    }
    signal_id = hashlib.sha256(
        json.dumps(stable_seed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "id": signal_id,
        "topic": topic,
        "label": label,
        "status": status if status in {"available", "degraded", "unavailable"} else "degraded",
        "fact_kind": fact_kind,
        "direction": direction if direction in {"positive", "negative", "neutral", "mixed"} else "neutral",
        "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
        "relation_score": max(0, min(100, int(relation_score))),
        "relation_method": "rule_based_prior_v1",
        "relation_basis": "规则化A股风险暴露评分，仅表示关联程度，不代表确定因果或收益预测。",
        "signal_strength": signal_strength,
        "stale": bool(stale),
        "freshness": dict(freshness or {"status": "not_applicable", "stale": bool(stale)}),
        "metric": dict(metric or {}),
        "evidence_time": _iso_datetime(evidence_time) or str(evidence_time or ""),
        "event_time": _iso_datetime(event_time if event_time is not None else evidence_time)
        or str(event_time if event_time is not None else evidence_time or ""),
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "source": source,
        "source_kind": source_kind,
        "evidence": evidence_rows,
        "related_themes": list(dict.fromkeys(str(value) for value in related_themes if value)),
        "threshold": threshold,
        "forecast": forecast,
        "limitations": [str(value) for value in limitations if str(value).strip()][:4],
    }


def _unavailable_intelligence_signal(
    topic: str,
    label: str,
    observed_at: datetime,
    error: str,
    *,
    relation_score: int,
    related_themes: Sequence[str],
) -> dict[str, Any]:
    return _intelligence_signal(
        topic=topic,
        label=label,
        observed_at=observed_at,
        status="unavailable",
        fact_kind="data_quality",
        direction="neutral",
        confidence=0.0,
        relation_score=relation_score,
        signal_strength="unavailable",
        metric={},
        evidence_time="",
        source="",
        source_kind="unknown",
        evidence=[],
        related_themes=related_themes,
        threshold="数据源恢复且时点通过校验后才允许预判",
        forecast="数据不可用，不生成方向性预判。",
        limitations=[error or "source unavailable"],
        stale=True,
        freshness={"status": "unavailable", "stale": True},
    )


def fetch_us_treasury_10y(*, fetch: Fetch, now: datetime) -> dict[str, Any]:
    """Return the latest two official US Treasury 10-year par-yield observations."""

    root = _xml_request(
        fetch,
        TREASURY_YIELD_URL,
        params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(now.year)},
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml"},
        timeout=12,
    )
    rows: list[tuple[date, float]] = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] != "properties":
            continue
        values = {child.tag.rsplit("}", 1)[-1]: _clean_text(child.text) for child in entry}
        parsed_date = _parse_datetime(values.get("NEW_DATE"))
        yield_value = _finite_number(values.get("BC_10YEAR"))
        if parsed_date is not None and yield_value is not None:
            rows.append((parsed_date.date(), yield_value))
    rows = sorted(set(rows))
    if not rows:
        raise NewsSourceError("Treasury XML has no valid BC_10YEAR observations")
    latest_date, latest = rows[-1]
    previous = rows[-2][1] if len(rows) > 1 else None
    return {
        "date": latest_date.isoformat(),
        "value": latest,
        "unit": "%",
        "change": round((latest - previous) * 100, 2) if previous is not None else None,
        "change_unit": "bp",
    }


def fetch_sk_hynix_daily(*, fetch: Fetch, now: datetime) -> dict[str, Any]:
    start = (now.date() - timedelta(days=12)).strftime("%Y%m%d")
    end = now.date().strftime("%Y%m%d")
    text = _text_request(
        fetch,
        NAVER_HYNIX_URL,
        params={
            "symbol": "000660",
            "requestType": "1",
            "startTime": start,
            "endTime": end,
            "timeframe": "day",
        },
        headers={"User-Agent": USER_AGENT, "Referer": "https://finance.naver.com/"},
        timeout=10,
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # NAVER's live endpoint is named ``siseJson`` but its header row uses
        # single-quoted strings, so the body is a Python/JS-style literal rather
        # than strict JSON.  ``literal_eval`` accepts only Python literals (no
        # calls or names); the root/row contract is still validated below.
        try:
            payload = ast.literal_eval(text.strip())
        except (SyntaxError, ValueError) as error:
            raise NewsSourceError(f"NAVER array parse failed: {error}") from error
    if not isinstance(payload, list):
        raise NewsSourceError("NAVER root must be an array")
    rows: list[tuple[str, float]] = []
    for raw in payload:
        if not isinstance(raw, list) or len(raw) < 5 or not re.fullmatch(r"\d{8}", str(raw[0])):
            continue
        close = _finite_number(raw[4])
        if close is not None and close > 0:
            rows.append((str(raw[0]), close))
    rows.sort()
    if not rows:
        raise NewsSourceError("NAVER returned no valid SK Hynix daily bars")
    latest_date, latest = rows[-1]
    previous = rows[-2][1] if len(rows) > 1 else None
    return {
        "date": f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}",
        "value": latest,
        "unit": "KRW",
        "change_pct": round((latest / previous - 1) * 100, 2) if previous else None,
    }


def fetch_eastmoney_a50(instrument_id: str, *, fetch: Fetch) -> dict[str, Any]:
    expected = {"100.XIN9": "XIN9", "104.CN00Y": "CN00Y"}
    if instrument_id not in expected:
        raise ValueError("unsupported A50 instrument")
    payload = _json_request(
        fetch,
        EASTMONEY_QUOTE_URL,
        params={
            "secid": instrument_id,
            "fields": "f43,f44,f45,f46,f57,f58,f59,f60,f86,f169,f170",
        },
        headers={"User-Agent": USER_AGENT, "Referer": "https://quote.eastmoney.com/"},
        timeout=10,
    )
    data = payload.get("data")
    if payload.get("rc") != 0 or not isinstance(data, Mapping):
        raise NewsSourceError(f"Eastmoney A50 rejected request: rc={payload.get('rc')!r}")
    if str(data.get("f57") or "") != expected[instrument_id]:
        raise NewsSourceError("Eastmoney A50 instrument identity mismatch")
    precision = int(_finite_number(data.get("f59")) or 0)
    if not 0 <= precision <= 6:
        raise NewsSourceError("Eastmoney A50 precision is invalid")
    scale = 10 ** precision
    value = _finite_number(data.get("f43"))
    change_pct = _finite_number(data.get("f170"))
    if value is None or value <= 0 or change_pct is None:
        raise NewsSourceError("Eastmoney A50 quote fields are incomplete")
    return {
        "instrument_id": instrument_id,
        "code": expected[instrument_id],
        "name": _clean_text(data.get("f58"), limit=80),
        "value": round(value / scale, precision),
        "unit": "index_point",
        "change_pct": round(change_pct / 100, 2),
        "quote_time": _iso_datetime(data.get("f86")),
    }


def fetch_next_fomc_meeting(*, fetch: Fetch, now: datetime) -> dict[str, Any]:
    text = _text_request(
        fetch,
        FOMC_CALENDAR_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=10,
    )
    meetings: list[date] = []
    month_numbers = {
        name: index
        for index, name in enumerate(
            ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
        )
        if name
    }
    for year in (now.year, now.year + 1):
        year_match = re.search(
            rf">\s*{year}\s+FOMC Meetings\s*</a>.*?</div>(.*?)(?=<div class=\"panel panel-default\">|\Z)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not year_match:
            continue
        for month_name, raw_days in re.findall(
            r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)</div>",
            year_match.group(1),
            flags=re.IGNORECASE | re.DOTALL,
        ):
            month = month_numbers.get(_clean_text(month_name).title())
            days = [int(value) for value in re.findall(r"\d{1,2}", raw_days)]
            if month and days:
                try:
                    meetings.append(date(year, month, days[-1]))
                except ValueError:
                    continue
    future = sorted(value for value in meetings if value >= now.date())
    if not future:
        raise NewsSourceError("FOMC calendar has no future meeting")
    next_meeting = future[0]
    return {
        "date": next_meeting.isoformat(),
        "days_until": (next_meeting - now.date()).days,
        "unit": "calendar_day",
    }


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = 1 + (4 - first.weekday()) % 7
    return date(year, month, first_friday + 14)


def _next_index_futures_expiry(now: datetime) -> dict[str, Any]:
    candidate = _third_friday(now.year, now.month)
    if candidate < now.date():
        next_month = now.month % 12 + 1
        next_year = now.year + (1 if next_month == 1 else 0)
        candidate = _third_friday(next_year, next_month)
    return {
        "date": candidate.isoformat(),
        "days_until": (candidate - now.date()).days,
        "unit": "calendar_day",
        "confirmation": "projected",
    }


def _topic_news(
    items: Sequence[Mapping[str, Any]], *, required: Sequence[str], actions: Sequence[str]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in items:
        text = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
        if any(word.casefold() in text for word in required) and any(
            word.casefold() in text for word in actions
        ):
            matches.append(dict(item))
    return sorted(matches, key=lambda row: str(row.get("published_at") or ""), reverse=True)


def _news_channel_count(item: Mapping[str, Any]) -> int:
    source_ids = item.get("source_ids")
    return len(set(map(str, source_ids))) if isinstance(source_ids, list) else 1


def _news_publisher_count(item: Mapping[str, Any]) -> int:
    """Count explicit original-publisher domains, never aggregator channels."""

    values = item.get("publisher_urls")
    if not isinstance(values, list):
        one = str(item.get("publisher_url") or "").strip()
        values = [one] if one else []
    domains = set()
    for value in values:
        parsed = urlparse(str(value or "").strip())
        domain = parsed.hostname.casefold() if parsed.hostname else ""
        if domain:
            domains.add(domain.removeprefix("www."))
    return len(domains)


def build_global_intelligence(
    news_items: Sequence[Mapping[str, Any]],
    news_source_status: Mapping[str, Any],
    *,
    fetch: Fetch | None,
    now: datetime,
) -> dict[str, Any]:
    """Build six bounded, independently degradable global-risk signals."""

    observed = _observed_at(now)
    signals: dict[str, dict[str, Any]] = {}
    healthy_news_sources = sum(
        1
        for raw in news_source_status.values()
        if raw is True or (isinstance(raw, Mapping) and raw.get("ok") is True)
    )

    fed_news = _topic_news(
        news_items,
        required=("美联储", "联邦储备", "fed", "fomc", "powell", "鲍威尔"),
        actions=("降息", "加息", "rate cut", "rate hike", "目标利率", "利率决定"),
    )
    fomc: dict[str, Any] | None = None
    fomc_error = "external fetch disabled"
    if fetch is not None:
        try:
            fomc = fetch_next_fomc_meeting(fetch=fetch, now=observed)
        except Exception as error:
            fomc_error = _safe_error(error)
    fed_event = fed_news[0] if fed_news else None
    fed_text = f"{(fed_event or {}).get('title', '')} {(fed_event or {}).get('summary', '')}".casefold()
    fed_direction = (
        "positive"
        if any(value in fed_text for value in ("降息", "rate cut"))
        else "negative"
        if any(value in fed_text for value in ("加息", "rate hike"))
        else "neutral"
    )
    if fomc or fed_event:
        evidence: list[dict[str, Any]] = []
        if fomc:
            evidence.append({
                "source": "美联储",
                "url": FOMC_CALENDAR_URL,
                "event_time": fomc["date"],
                "observed_at": observed.isoformat(timespec="seconds"),
            })
        if fed_event:
            evidence.append({
                "source": str(fed_event.get("source") or "市场快讯"),
                "url": str(fed_event.get("url") or ""),
                "evidence_time": str(fed_event.get("published_at") or ""),
            })
        meeting_days = _finite_number((fomc or {}).get("days_until"))
        signals["federal_reserve"] = _intelligence_signal(
            topic="federal_reserve",
            label="美联储利率与会议窗口",
            observed_at=observed,
            status="available" if fomc else "degraded",
            fact_kind="official_calendar" if fomc and not fed_event else "calendar_and_news_lead",
            direction=fed_direction,
            confidence=0.93 if fomc and not fed_event else min(0.9, 0.58 + 0.12 * _news_channel_count(fed_event or {})),
            relation_score=76,
            signal_strength="high" if meeting_days is not None and meeting_days <= 2 else "medium" if fed_event else "low",
            metric={"next_meeting": (fomc or {}).get("date"), "days_until": meeting_days, "news_evidence_count": len(fed_news)},
            evidence_time=(fed_event or {}).get("published_at") or "",
            event_time=(fomc or {}).get("date") or (fed_event or {}).get("published_at"),
            source="美联储 / 市场快讯" if fed_event else "美联储",
            source_kind="official_and_wire" if fed_event else "official",
            evidence=evidence,
            related_themes=("high_beta_tech", "property_transport", "broad_market"),
            threshold="会议前2日或出现明确加/减息决定线索",
            forecast=(
                "降息线索通常改善成长估值环境，仍需A50与人民币确认。"
                if fed_direction == "positive"
                else "加息线索通常压制高估值成长风险偏好。"
                if fed_direction == "negative"
                else "临近会议只提示波动窗口，不直接预测涨跌。"
            ),
            limitations=([f"日历源降级：{fomc_error}"] if not fomc else []) + ["新闻标题仅作线索，需核验美联储原文。"],
        )
    else:
        signals["federal_reserve"] = _unavailable_intelligence_signal(
            "federal_reserve", "美联储利率与会议窗口", observed, fomc_error,
            relation_score=76, related_themes=("high_beta_tech", "broad_market"),
        )

    conflict_news = _topic_news(
        news_items,
        required=("伊朗", "iran"),
        actions=("美国", "美军", "袭击", "战争", "冲突", "导弹", "停火", "us "),
    )
    if conflict_news:
        event = conflict_news[0]
        channel_count = _news_channel_count(event)
        publisher_count = _news_publisher_count(event)
        conflict_text = f"{event.get('title', '')} {event.get('summary', '')}".casefold()
        escalation = any(
            value in conflict_text
            for value in ("袭击", "战争", "冲突升级", "导弹", "空袭", "strike", "attack", "escalat")
        )
        easing = any(
            value in conflict_text
            for value in ("停火", "和谈", "缓和", "ceasefire", "truce", "de-escalat")
        )
        conflict_direction = (
            "mixed" if escalation and easing else "negative" if escalation else "positive" if easing else "neutral"
        )
        signals["us_iran_conflict"] = _intelligence_signal(
            topic="us_iran_conflict",
            label="美伊冲突线索",
            observed_at=observed,
            status="available" if publisher_count >= 2 else "degraded",
            fact_kind=(
                "cross_publisher_news"
                if publisher_count >= 2
                else "multi_channel_news_lead"
                if channel_count >= 2
                else "single_channel_news_lead"
            ),
            direction=conflict_direction,
            confidence=0.8 if publisher_count >= 2 else 0.62 if channel_count >= 2 else 0.55,
            relation_score=70,
            signal_strength="high" if publisher_count >= 2 else "medium",
            metric={"channel_count": channel_count, "independent_publisher_count": publisher_count},
            evidence_time=event.get("published_at"),
            source=str(event.get("source") or "市场快讯"),
            source_kind="wire",
            evidence=[{"source": event.get("source"), "url": event.get("url"), "evidence_time": event.get("published_at")}],
            related_themes=("utilities", "property_transport", "industrial_environmental", "broad_market"),
            threshold="至少两个明确原始发布方域名才标为交叉确认；多采集渠道但原始发布方不明只作线索",
            forecast=(
                "停火或缓和线索可能降低油价与风险溢价，仍需官方确认。"
                if conflict_direction == "positive"
                else "升级线索可能推升油价与风险厌恶，运输和高贝塔方向承压。"
                if conflict_direction == "negative"
                else "升级与缓和线索并存或方向不明，仅提示事件波动风险。"
            ),
            limitations=("采集渠道数不等于独立发布方数，同一通讯社转载不会升级为交叉确认。", "新闻线索不等同于军事事件已获官方确认。"),
        )
    elif healthy_news_sources:
        scan_evidence_time = max(
            (str(item.get("published_at") or "") for item in news_items if item.get("published_at")),
            default="",
        )
        signals["us_iran_conflict"] = _intelligence_signal(
            topic="us_iran_conflict", label="美伊冲突线索", observed_at=observed,
            status="available" if healthy_news_sources >= 2 else "degraded",
            fact_kind="bounded_news_scan", direction="neutral", confidence=0.4,
            relation_score=70, signal_strength="low",
            metric={"evidence_count": 0}, evidence_time=scan_evidence_time,
            source="财联社 / 东方财富 / Google 新闻", source_kind="wire_scan", evidence=[],
            related_themes=("utilities", "property_transport", "broad_market"),
            threshold="出现伊朗与美国/袭击/停火等共同关键词",
            forecast="当前有限快讯窗口未匹配到新线索；这不代表冲突风险为零。",
            limitations=("有界新闻流并非全网完整检索。",),
        )
    else:
        signals["us_iran_conflict"] = _unavailable_intelligence_signal(
            "us_iran_conflict", "美伊冲突线索", observed, "all bounded news sources unavailable",
            relation_score=70, related_themes=("utilities", "property_transport", "broad_market"),
        )

    treasury_error = "external fetch disabled"
    if fetch is not None:
        try:
            treasury = fetch_us_treasury_10y(fetch=fetch, now=observed)
            bp = _finite_number(treasury.get("change"))
            direction = "negative" if bp is not None and bp >= 5 else "positive" if bp is not None and bp <= -5 else "neutral"
            freshness = _intelligence_freshness(
                treasury.get("date"),
                now=observed,
                max_age_seconds=TREASURY_MAX_AGE_SECONDS,
            )
            if freshness["stale"]:
                direction = "neutral"
            signals["us_treasury_10y"] = _intelligence_signal(
                topic="us_treasury_10y", label="美国10年期国债收益率", observed_at=observed,
                status="degraded" if freshness["stale"] else "available",
                fact_kind="official_end_of_day", direction=direction,
                confidence=0.35 if freshness["stale"] else 0.95, relation_score=82,
                signal_strength="stale" if freshness["stale"] else _signal_strength(bp, medium=5, high=10), metric=treasury,
                evidence_time=treasury["date"], source="美国财政部", source_kind="official",
                evidence=[{"source": "美国财政部", "url": TREASURY_YIELD_URL, "evidence_time": treasury["date"]}],
                related_themes=("high_beta_tech", "property_transport", "broad_market"),
                threshold="单日变动达到5bp关注、10bp高警戒",
                forecast=("数据时点已过期，不生成方向性预判。" if freshness["stale"] else "收益率上行通常压制高估值成长板块。" if direction == "negative" else "收益率回落通常缓解成长估值压力。" if direction == "positive" else "日变动未越过方向阈值。"),
                limitations=("财政部数据是官方日终收益率，不是盘中实时行情。",),
                stale=freshness["stale"],
                freshness=freshness,
            )
        except Exception as error:
            treasury_error = _safe_error(error)
    if "us_treasury_10y" not in signals:
        signals["us_treasury_10y"] = _unavailable_intelligence_signal(
            "us_treasury_10y", "美国10年期国债收益率", observed, treasury_error,
            relation_score=82, related_themes=("high_beta_tech", "broad_market"),
        )

    hynix_error = "external fetch disabled"
    if fetch is not None:
        try:
            hynix = fetch_sk_hynix_daily(fetch=fetch, now=observed)
            change = _finite_number(hynix.get("change_pct"))
            direction = "positive" if change is not None and change >= 2 else "negative" if change is not None and change <= -2 else "neutral"
            freshness = _intelligence_freshness(
                hynix.get("date"),
                now=observed,
                max_age_seconds=HYNIX_MAX_AGE_SECONDS,
            )
            if freshness["stale"]:
                direction = "neutral"
            signals["sk_hynix"] = _intelligence_signal(
                topic="sk_hynix", label="SK海力士股价", observed_at=observed,
                status="degraded" if freshness["stale"] else "available",
                fact_kind="vendor_daily_quote", direction=direction,
                confidence=0.3 if freshness["stale"] else 0.72, relation_score=78,
                signal_strength="stale" if freshness["stale"] else _signal_strength(change, medium=2, high=4), metric=hynix,
                evidence_time=hynix["date"], source="NAVER Finance", source_kind="market_feed",
                evidence=[{"source": "NAVER Finance", "url": NAVER_HYNIX_URL, "evidence_time": hynix["date"]}],
                related_themes=("high_beta_tech", "semiconductor", "memory"),
                threshold="日涨跌达到2%关注、4%高警戒",
                forecast=("数据时点已过期，不生成方向性预判。" if freshness["stale"] else "海力士走强可作为存储产业链情绪的正向旁证。" if direction == "positive" else "海力士走弱提示A股存储与半导体情绪传导风险。" if direction == "negative" else "变化未越过方向阈值。"),
                limitations=("NAVER是未文档化厂商行情，不是KRX官方接口。", "跨市场假日与交易时段不同，不能直接推断A股收益。"),
                stale=freshness["stale"],
                freshness=freshness,
            )
        except Exception as error:
            hynix_error = _safe_error(error)
    if "sk_hynix" not in signals:
        signals["sk_hynix"] = _unavailable_intelligence_signal(
            "sk_hynix", "SK海力士股价", observed, hynix_error,
            relation_score=78, related_themes=("high_beta_tech", "semiconductor", "memory"),
        )

    a50_errors: list[str] = []
    spot: dict[str, Any] | None = None
    future: dict[str, Any] | None = None
    if fetch is not None:
        try:
            spot = fetch_eastmoney_a50("100.XIN9", fetch=fetch)
        except Exception as error:
            a50_errors.append(f"现货：{_safe_error(error)}")
        try:
            future = fetch_eastmoney_a50("104.CN00Y", fetch=fetch)
        except Exception as error:
            a50_errors.append(f"连续期指：{_safe_error(error)}")
    else:
        a50_errors.append("external fetch disabled")
    if spot or future:
        instruments = [("spot", spot, "100.XIN9"), ("continuous_future", future, "104.CN00Y")]
        freshness_by_instrument = {
            key: _intelligence_freshness(
                row.get("quote_time") if row else None,
                now=observed,
                max_age_seconds=A50_MAX_AGE_SECONDS,
                future_tolerance_seconds=5 * 60,
            )
            for key, row, _ in instruments
            if row is not None
        }
        fresh_rows = [
            (key, row, instrument_id)
            for key, row, instrument_id in instruments
            if row is not None and not freshness_by_instrument[key]["stale"]
        ]
        changes = [_finite_number(row.get("change_pct")) for _, row, _ in fresh_rows]
        usable_changes = [value for value in changes if value is not None]
        if len(usable_changes) >= 2 and min(usable_changes) < 0 < max(usable_changes):
            direction = "mixed"
        else:
            reference = sum(usable_changes) / len(usable_changes) if usable_changes else None
            direction = "positive" if reference is not None and reference >= 0.5 else "negative" if reference is not None and reference <= -0.5 else "neutral"
        strongest_change = max(usable_changes, key=abs) if usable_changes else None
        all_fresh = len(fresh_rows) == 2
        no_fresh = not fresh_rows
        evidence_time = max(
            (str(row.get("quote_time") or "") for _, row, _ in instruments if row),
            default="",
        )
        signals["a50"] = _intelligence_signal(
            topic="a50", label="富时中国A50现货与期指", observed_at=observed,
            status="available" if all_fresh else "degraded",
            fact_kind="vendor_spot_and_continuous_future" if spot and future else "vendor_partial_quote",
            direction=direction,
            confidence=0.82 if all_fresh else 0.55 if fresh_rows else 0.25,
            relation_score=92,
            signal_strength=_signal_strength(strongest_change, medium=0.5, high=1.0),
            metric={key: row for key, row, _ in instruments if row is not None},
            evidence_time=evidence_time,
            source="东方财富", source_kind="market_feed",
            evidence=[
                {"source": "东方财富", "url": EASTMONEY_QUOTE_URL,
                 "evidence_time": row.get("quote_time"), "instrument_id": instrument_id}
                for _, row, instrument_id in instruments if row is not None
            ],
            related_themes=("broad_market", "high_beta_tech", "financials"),
            threshold="现货或连续期指涨跌达到0.5%关注、1%高警戒；现货与期指双标的齐全时交叉检查",
            forecast=("A50走强可作为A股大盘风险偏好的正向旁证。" if direction == "positive" else "A50走弱提示大盘与高贝塔持仓的短线压力。" if direction == "negative" else "现货与连续期指信号不一致、过期或未越阈值，方向置信度降低。"),
            limitations=("100.XIN9为现货指数，104.CN00Y为期指月连续，二者不会混作同一标的。", "连续期指换月跳空不应进入历史相关模型。", *a50_errors),
            stale=no_fresh,
            freshness={"status": "fresh" if all_fresh else "partial" if fresh_rows else "stale", "stale": no_fresh, "instruments": freshness_by_instrument},
        )
    if "a50" not in signals:
        signals["a50"] = _unavailable_intelligence_signal(
            "a50", "富时中国A50现货与期指", observed, "; ".join(a50_errors),
            relation_score=92, related_themes=("broad_market", "high_beta_tech"),
        )

    expiry = _next_index_futures_expiry(observed)
    days_until = int(expiry["days_until"])
    signals["index_futures_expiry"] = _intelligence_signal(
        topic="index_futures_expiry", label="股指期货交割窗口", observed_at=observed,
        status="degraded", fact_kind="official_rule_projection", direction="neutral",
        confidence=0.6, relation_score=55,
        signal_strength="high" if days_until <= 2 else "medium" if days_until <= 5 else "low",
        metric=expiry, evidence_time="", event_time=expiry["date"],
        source="中金所交割规则", source_kind="exchange_rule",
        evidence=[{
            "source": "中金所", "url": CFFEX_RULE_URL,
            "event_time": expiry["date"],
            "observed_at": observed.isoformat(timespec="seconds"),
        }],
        related_themes=("broad_market", "high_beta_tech"),
        threshold="规则推算交割日前5日关注、前2日高关注",
        forecast="交割临近只提示波动率、成交量与基差可能放大，不直接预测涨跌。",
        limitations=("日期按每月第三个周五规则推算；遇法定假日顺延，需用中金所日历确认。",),
    )

    ordered = [signals[topic] for topic in INTELLIGENCE_TOPICS]
    available = sum(signal["status"] != "unavailable" for signal in ordered)
    degraded = any(signal["status"] != "available" for signal in ordered)
    canonical = [
        {key: value for key, value in signal.items() if key != "observed_at"}
        for signal in ordered
    ]
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "status": "unavailable" if available == 0 else "degraded" if degraded else "healthy",
        "generated_at": observed.isoformat(timespec="seconds"),
        "signal_count": len(ordered),
        "available_count": available,
        "fingerprint": fingerprint,
        "signals": ordered,
        "notes": [
            "关联度是规则化风险暴露评分，不代表确定因果或收益预测。",
            "只有数据新鲜且越过阈值的信号才应进入预判提醒。",
        ],
    }


def fetch_market_news(
    page_size: int = 50,
    *,
    fetch: Fetch | None = None,
    intelligence_fetch: Fetch | None = None,
    eastmoney_limiter: SerialRateLimiter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch all sources and return one explicit health/data envelope.

    ``ok`` means at least one source completed with a structurally valid payload.
    ``degraded`` means fewer than all independent sources completed.  Therefore
    a total failure is represented as ``status='unavailable', ok=False,
    degraded=True`` rather than a misleading successful empty list.
    """

    _page_size(page_size)
    observed = _observed_at(now)
    source_status: dict[str, dict[str, Any]] = {}
    all_items: list[dict[str, Any]] = []

    collectors = (
        (
            CLS_SOURCE_ID,
            lambda: fetch_cls_telegraph(page_size, fetch=fetch),
        ),
        (
            EASTMONEY_SOURCE_ID,
            lambda: fetch_eastmoney_global_news(
                page_size,
                fetch=fetch,
                limiter=eastmoney_limiter,
            ),
        ),
        (
            JIN10_SOURCE_ID,
            lambda: fetch_jin10_flash(page_size, fetch=fetch),
        ),
        (
            THS_NEWS_SOURCE_ID,
            lambda: fetch_ths_news(page_size, fetch=fetch),
        ),
        (
            GOOGLE_NEWS_SOURCE_ID,
            lambda: fetch_google_global_market_news(fetch=fetch, now=observed),
        ),
        (
            WSCN_SOURCE_ID,
            lambda: fetch_wallstreetcn_lives(page_size, fetch=fetch),
        ),
        (
            MARKETWATCH_SOURCE_ID,
            lambda: fetch_marketwatch_top_stories(fetch=fetch, now=observed),
        ),
    )
    for source_id, collect in collectors:
        try:
            received = collect()
        except Exception as error:
            source_status[source_id] = {
                "ok": False,
                "count": 0,
                "received_count": 0,
                "invalid_time_count": 0,
                "outside_window_count": 0,
                "error": _safe_error(error),
            }
            continue
        items, invalid_time_count, outside_window_count = _filter_time_window(
            received,
            window_end=observed,
        )
        source_status[source_id] = {
            "ok": True,
            "count": len(items),
            "received_count": len(received),
            "invalid_time_count": invalid_time_count,
            "outside_window_count": outside_window_count,
            "error": "",
        }
        all_items.extend(items)

    succeeded = sum(1 for value in source_status.values() if value["ok"])
    expected_sources = len(collectors)
    status = (
        "healthy"
        if succeeded == expected_sources
        else "degraded"
        if succeeded > 0
        else "unavailable"
    )
    items = deduplicate_news(all_items)
    latest_event_at = max(
        (str(item.get("published_at") or "") for item in items if item.get("published_at")),
        default="",
    )
    canonical_items = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_fingerprint = hashlib.sha256(canonical_items.encode("utf-8")).hexdigest()
    # Preserve the original three-source transport contract for injected tests and
    # callers.  Production can explicitly pass its shared/throttled transport;
    # direct production calls (no injected fetcher) use this module's session.
    external_fetch = intelligence_fetch if intelligence_fetch is not None else (
        _default_fetch if fetch is None else None
    )
    intelligence = build_global_intelligence(
        items,
        source_status,
        fetch=external_fetch,
        now=observed,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": succeeded > 0,
        "degraded": succeeded < expected_sources,
        "status": status,
        "generated_at": observed.isoformat(timespec="seconds"),
        "window_start_at": (observed - NEWS_WINDOW).isoformat(timespec="seconds"),
        "window_end_at": observed.isoformat(timespec="seconds"),
        "window_seconds": int(NEWS_WINDOW.total_seconds()),
        "source_status": source_status,
        "source_count": succeeded,
        "expected_source_count": expected_sources,
        "item_count": len(items),
        "content_fingerprint": content_fingerprint,
        "latest_event_at": latest_event_at,
        "items": items,
        "intelligence": intelligence,
        "intelligence_fingerprint": intelligence["fingerprint"],
    }


__all__ = [
    "CN_TZ",
    "EASTMONEY_JITTER_SECONDS",
    "EASTMONEY_MIN_INTERVAL_SECONDS",
    "FLASH_MIN_INTERVAL_SECONDS",
    "GOOGLE_NEWS_QUERY",
    "GOOGLE_NEWS_URL",
    "JIN10_APP_ID",
    "JIN10_FLASH_URL",
    "MARKETWATCH_RSS_URL",
    "THS_NEWS_URL",
    "INTELLIGENCE_SCHEMA_VERSION",
    "INTELLIGENCE_TOPICS",
    "NEAR_DUPLICATE_MAX_SECONDS",
    "NEAR_DUPLICATE_SIMILARITY",
    "NEWS_WINDOW",
    "NewsSourceError",
    "SCHEMA_VERSION",
    "SerialRateLimiter",
    "classify_keywords",
    "build_global_intelligence",
    "deduplicate_news",
    "fetch_cls_telegraph",
    "fetch_eastmoney_global_news",
    "fetch_google_global_market_news",
    "fetch_jin10_flash",
    "fetch_marketwatch_top_stories",
    "fetch_ths_news",
    "fetch_eastmoney_a50",
    "fetch_market_news",
    "fetch_next_fomc_meeting",
    "fetch_sk_hynix_daily",
    "fetch_us_treasury_10y",
]
