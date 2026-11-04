"""持仓「最新财报」取数：东财业绩报表 + 机构汇总（累计口径，不拆单季）。

数据来源与口径
--------------
* ``RPT_LICO_FN_CPD``（业绩报表）：营业总收入 / 归母净利润及其同比、加权 ROE、
  销售毛利率、基本 EPS。**累计口径**——三季报即 1-9 月合计，不从中报做减法。
* ``RPT_MAIN_ORGHOLD``（机构汇总，``ORG_TYPE=00``）：机构家数、持股市值、
  占流通股比例。

为什么强调「未披露」而不是补零
------------------------------
三季报披露期到 10-31 才结束。截至 2026-10-28，14 只持仓里只有 7 只出了三季报。
未披露的股票必须显示「三季报未披露」，不能拿中报顶替，也不能填 0——这个项目
的中心约定是「缺证据就说缺证据」，错误数字比空值危险得多。

机构数的时效性
--------------
机构汇总在披露期内会随基金季报陆续更新，同一天不同时刻查询可能不同。所以每行
都带机构数自己的 ``report_period`` 与 ``as_of``，前端注明「截至披露」。
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "holding-fundamentals.json"
CACHE_SCHEMA = "holding-fundamentals.v1"

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CN_TZ = timezone(timedelta(hours=8))

# 东财 datacenter 限流较严：必须串行 + 间隔 ≥1.2s（与 market_snapshot 同口径）。
EM_MIN_INTERVAL_SECONDS = 1.2
EM_JITTER_SECONDS = 0.35
_EM_LOCK = threading.Lock()
_EM_LAST_CALL = 0.0

CACHE_TTL_SECONDS = 6 * 60 * 60.0
CACHE_MAX_BYTES = 512 * 1024
# 每轮最多两次东财调用（业绩报表一次、机构汇总一次），不做逐股循环。
MAX_DATACENTER_CALLS = 2

Q3_PERIOD = "2026-09-30"
Q3_LABEL = "2026-09-30"


class FundamentalsError(RuntimeError):
    """上游取数失败（网络/结构异常），调用方应回退缓存。"""


def _throttle() -> None:
    global _EM_LAST_CALL
    with _EM_LOCK:
        wait = EM_MIN_INTERVAL_SECONDS + random.uniform(0.0, EM_JITTER_SECONDS)
        elapsed = time.monotonic() - _EM_LAST_CALL
        if elapsed < wait:
            time.sleep(wait - elapsed)
        _EM_LAST_CALL = time.monotonic()


def _number(value: Any) -> Optional[float]:
    """东财字段转 float；空串、"-"、None 一律视为未知，不当作 0。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _period_label(period: str) -> str:
    """2026-09-30 → 2026三季报；2026-06-30 → 2026中报；其余原样返回。"""
    text = str(period or "")[:10]
    if len(text) < 10:
        return text
    year = text[:4]
    suffix = {"09-30": "三季报", "06-30": "中报", "03-31": "一季报", "12-31": "年报"}.get(text[5:])
    return f"{year}{suffix}" if suffix else text


def _code_list(codes: Iterable[str]) -> str:
    cleaned = [str(code).strip() for code in codes if str(code).strip()]
    return ",".join(f'"{code}"' for code in cleaned)


def _request(params: dict[str, Any]) -> dict[str, Any]:
    _throttle()
    response = requests.get(
        DATACENTER,
        params=params,
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 12.0),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise FundamentalsError("datacenter 返回的不是 JSON 对象")
    if payload.get("success") is False:
        # 接口明确失败（如「报表配置不存在」）≠ 没有数据。若不区分，错误会被
        # 下游渲染成「未披露」，把接口故障伪装成业务事实。
        raise FundamentalsError(str(payload.get("message") or "datacenter 返回失败"))
    return payload


def fetch_earnings(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    """批量取最新一期业绩报表（累计口径）。返回 {code: row}。

    为什么按「报告期集合」筛选而不是「按 SECURITY_CODE,REPORTDATE 排序 + pageSize」：
    后者会按代码排序后只返回第一只股票的全部历史（实测 3 只股票、pageSize=64
    时 64 行全是 000977），其余代码永远取不到——表现为大量股票被误判为「未披露」。
    这里显式取出最近几个报告期，再在本地按报告期倒序取每只股票最新一行。
    """
    if not codes:
        return {}
    periods = _recent_periods()
    payload = _request({
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": (
            f"(SECURITY_CODE in ({_code_list(codes)}))"
            f"({_period_filter(periods)})"
        ),
        "pageNumber": "1",
        # 代码数 × 报告期数，留足余量避免分页截断。
        "pageSize": str(max(50, len(codes) * len(periods) + 20)),
        "source": "WEB",
        "client": "WEB",
    })
    rows = ((payload.get("result") or {}).get("data")) or []
    latest: dict[str, dict[str, Any]] = {}
    best_period: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("SECURITY_CODE") or "").strip()
        period = str(row.get("REPORTDATE") or "")[:10]
        if not code or not period:
            continue
        if code not in best_period or period > best_period[code]:
            best_period[code] = period
            latest[code] = row
    return latest


def _recent_periods(today: Optional[str] = None) -> list[str]:
    """最近 4 个报告期末（含当期），用于一次请求覆盖最新与上一期。"""
    today_date = today or datetime.now(CN_TZ).date().isoformat()
    year = int(today_date[:4])
    candidates = [
        f"{y}-{mmdd}"
        for y in (year, year - 1)
        for mmdd in ("12-31", "09-30", "06-30", "03-31")
        if f"{y}-{mmdd}" <= today_date
    ]
    return sorted(candidates, reverse=True)[:4]


def _period_filter(periods: Sequence[str]) -> str:
    """东财 datacenter 的 OR 语法必须是 in ('a','b')；`||` 会触发解析异常。"""
    quoted = ",".join(f"'{period}'" for period in periods)
    return f"(REPORTDATE in ({quoted}))"


def fetch_org_holdings(codes: Sequence[str], period: str = Q3_PERIOD) -> dict[str, dict[str, Any]]:
    """批量取指定报告期的机构汇总（ORG_TYPE=00，即机构合计而非单类）。"""
    if not codes:
        return {}
    payload = _request({
        "reportName": "RPT_MAIN_ORGHOLD",
        "columns": "ALL",
        "filter": (
            f"(SECURITY_CODE in ({_code_list(codes)}))"
            f'(ORG_TYPE="00")(REPORT_DATE=\'{period}\')'
        ),
        "pageNumber": "1",
        "pageSize": str(max(50, len(codes) * 2)),
        "source": "WEB",
        "client": "WEB",
    })
    rows = ((payload.get("result") or {}).get("data")) or []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("SECURITY_CODE") or "").strip()
        if not code or code in result:
            continue
        result[code] = {
            "report_period": str(row.get("REPORT_DATE") or "")[:10],
            "org_count": int(_number(row.get("HOULD_NUM")) or 0) or None,
            "hold_value": _number(row.get("HOLD_VALUE")),
            "free_share_ratio_pct": _number(row.get("FREESHARES_RATIO")),
        }
    return result


def _earnings_row(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return {"available": False, "reason": "not_disclosed"}
    period = str(row.get("REPORTDATE") or "")[:10]
    return {
        "available": True,
        "period": period,
        "period_label": _period_label(period),
        "notice_date": str(row.get("NOTICE_DATE") or "")[:10],
        "revenue": _number(row.get("TOTAL_OPERATE_INCOME")),
        "revenue_yoy_pct": _number(row.get("YSTZ")),
        "net_profit": _number(row.get("PARENT_NETPROFIT")),
        "net_profit_yoy_pct": _number(row.get("SJLTZ")),
        "roe_pct": _number(row.get("WEIGHTAVG_ROE")),
        "gross_margin_pct": _number(row.get("XSMLL")),
        "eps": _number(row.get("BASIC_EPS")),
    }


def build_fundamentals(positions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把持仓列表转成 {symbol6: entry}。ETF 返回占位，不做财报查询。"""
    stock_codes: list[str] = []
    etf_symbols: set[str] = set()
    for position in positions:
        code = str(position.get("code") or "").strip()
        if not code:
            continue
        instrument = str(position.get("instrument_type") or "stock").lower()
        if instrument == "etf":
            etf_symbols.add(code)
        else:
            stock_codes.append(code)

    earnings = fetch_earnings(stock_codes) if stock_codes else {}
    holdings: dict[str, dict[str, Any]] = {}
    holdings_error = ""
    if stock_codes:
        try:
            holdings = fetch_org_holdings(stock_codes)
        except Exception as exc:  # 机构数是附加信息，失败不应拖垮整块
            holdings_error = f"{type(exc).__name__}: {exc}"

    entries: dict[str, dict[str, Any]] = {}
    for code in etf_symbols:
        entries[code] = {"instrument_type": "etf", "available": False, "reason": "etf"}
    for code in stock_codes:
        entry = {"instrument_type": "stock", **_earnings_row(earnings.get(code))}
        entry["org_holding"] = holdings.get(code)
        entries[code] = entry
    return {
        "entries": entries,
        "holdings_error": holdings_error,
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ 缓存


def _cache_key(codes: Iterable[str]) -> str:
    return ",".join(sorted(str(code) for code in codes))


def load_cache(codes: Sequence[str]) -> Optional[dict[str, Any]]:
    """读取未过期的磁盘缓存；任何异常都当作无缓存。"""
    try:
        if not CACHE_PATH.is_file():
            return None
        if CACHE_PATH.stat().st_size > CACHE_MAX_BYTES:
            return None
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA:
            return None
        if payload.get("key") != _cache_key(codes):
            return None
        fetched_at = float(payload.get("fetched_at_monotonic") or 0)
        # 磁盘缓存的 monotonic 在跨进程后无意义，改用墙钟时间判断。
        written = str(payload.get("written_at") or "")
        if not written:
            return None
        age = (datetime.now(CN_TZ) - datetime.fromisoformat(written)).total_seconds()
        if age < 0 or age > CACHE_TTL_SECONDS:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_cache(codes: Sequence[str], data: dict[str, Any]) -> None:
    """原子写入；失败不影响本次返回。"""
    try:
        payload = {
            "schema_version": CACHE_SCHEMA,
            "key": _cache_key(codes),
            "written_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "data": data,
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > CACHE_MAX_BYTES:
            return
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_PATH.with_suffix(f".tmp{os.getpid()}")
        temp.write_bytes(encoded)
        os.replace(temp, CACHE_PATH)
    except Exception:
        return


def get_holding_fundamentals(
    positions: Sequence[dict[str, Any]], force: bool = False
) -> dict[str, Any]:
    """对外入口：缓存优先，失败时回退旧缓存并标 stale。"""
    codes = [str(p.get("code") or "").strip() for p in positions if str(p.get("code") or "").strip()]
    if not force:
        cached = load_cache(codes)
        if cached is not None:
            return {**cached, "stale": False, "from_cache": True}
    try:
        data = build_fundamentals(positions)
    except Exception as exc:
        cached = load_cache(codes)
        if cached is not None:
            return {
                **cached,
                "stale": True,
                "from_cache": True,
                "refresh_error": f"{type(exc).__name__}: {exc}",
            }
        raise
    save_cache(codes, data)
    return {**data, "stale": False, "from_cache": False}
