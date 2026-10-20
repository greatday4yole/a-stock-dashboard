#!/usr/bin/env python3
"""Build a bounded daily price-P&L history for the configured portfolio.

The configured broker screenshots are a point-in-time position snapshot, not a
transaction ledger.  This module therefore performs an explicitly labelled
static replay: today's configured quantity is multiplied by each session's
change in the *unadjusted* Tencent close.  It must not be presented as the
broker's historical account P&L because trades, fees, financing, cash flows and
cash dividends are not available in the portfolio configuration.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as daytime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from scripts.position_advice_store import portfolio_fingerprint_sha256
except ModuleNotFoundError:  # Direct execution: python scripts/daily_pnl_history.py
    from position_advice_store import (  # type: ignore[no-redef]
        portfolio_fingerprint_sha256,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = ROOT / "data" / "daily-pnl-history.json"
DEFAULT_START_DATE = date(2026, 7, 1)
CN_TZ = timezone(timedelta(hours=8))

API_SCHEMA_VERSION = "daily-pnl-history.api.v1"
CACHE_SCHEMA_VERSION = "daily-pnl-history.cache.v1"
CALCULATION_VERSION = "static-quantity-raw-close.v2"
TENCENT_RAW_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
# 腾讯未复权日K主机轮换：web.ifzq 曾被 WAF 整主机拦截（501），ifzq 备机接管；
# 两台同属腾讯系，跨源兜底由新浪 getKLineData 承担（见 fetch_sina_raw_daily）。
TENCENT_RAW_KLINE_HOSTS = (
    TENCENT_RAW_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/kline/kline",
)
TENCENT_SOURCE_ID = "tencent-raw-unadjusted-daily"
SINA_RAW_KLINE = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
)
SINA_SOURCE_ID = "sina-raw-unadjusted-daily"
CALENDAR_SYMBOL = "sh000001"
MAX_CACHE_BYTES = 2 * 1024 * 1024
MAX_LOOKBACK_CALENDAR_DAYS = 900
MAX_HISTORY_BARS = 640
MIN_HISTORY_BARS = 100
CURRENT_DAY_CACHE_TTL_SECONDS = 55.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HTTP_LOCAL = threading.local()


class DailyPnlHistoryUnavailable(RuntimeError):
    """No safely bound daily P&L history can be published."""


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _http_session() -> requests.Session:
    session = getattr(_HTTP_LOCAL, "session", None)
    if isinstance(session, requests.Session):
        return session
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.35,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    _HTTP_LOCAL.session = session
    return session


def _coerce_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not _ISO_DATE_RE.fullmatch(raw):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return parsed


def validate_date_range(
    start: Any,
    end: Any,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    start_date = _coerce_date(start, "start")
    end_date = _coerce_date(end, "end")
    current = today or _now().date()
    if start_date > end_date:
        raise ValueError("start must not be later than end")
    if end_date > current:
        raise ValueError("end must not be in the future")
    if (end_date - start_date).days > MAX_LOOKBACK_CALENDAR_DAYS:
        raise ValueError("requested history exceeds the supported lookback")
    return start_date, end_date


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field} must be a finite {qualifier}number")
    return number


def _symbol_from_thscode(thscode: str) -> str:
    raw = str(thscode or "").strip().upper()
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", raw):
        raise ValueError(f"unsupported thscode: {raw!r}")
    code, exchange = raw.split(".", 1)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[exchange] + code


def aggregate_positions(portfolio: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Aggregate the same security across accounts using config quantities only."""

    if not isinstance(portfolio, Mapping):
        raise ValueError("portfolio must be an object")
    aggregated: dict[str, dict[str, Any]] = {}
    accounts = portfolio.get("accounts") or []
    if not isinstance(accounts, list):
        raise ValueError("portfolio accounts must be an array")
    for account in accounts:
        if not isinstance(account, Mapping):
            raise ValueError("portfolio account must be an object")
        positions = account.get("positions") or []
        if not isinstance(positions, list):
            raise ValueError("portfolio positions must be an array")
        for item in positions:
            if not isinstance(item, Mapping):
                raise ValueError("portfolio position must be an object")
            thscode = str(item.get("thscode") or "").strip().upper()
            symbol = _symbol_from_thscode(thscode)
            quantity = _finite_number(
                item.get("quantity"), f"{thscode}.quantity", positive=True
            )
            listing_date = str(item.get("listing_date") or "").strip()
            if listing_date:
                _coerce_date(listing_date, f"{thscode}.listing_date")
            existing = aggregated.get(thscode)
            if existing is None:
                aggregated[thscode] = {
                    "thscode": thscode,
                    "symbol": symbol,
                    "name": str(item.get("name") or thscode),
                    "instrument_type": str(item.get("instrument_type") or "stock"),
                    "quantity": quantity,
                    "listing_date": listing_date,
                }
            else:
                existing["quantity"] = float(existing["quantity"]) + quantity
                if listing_date and (
                    not existing.get("listing_date")
                    or listing_date < str(existing.get("listing_date"))
                ):
                    existing["listing_date"] = listing_date
    if not aggregated:
        raise ValueError("portfolio contains no positions")
    return [aggregated[key] for key in sorted(aggregated)]


def _history_items(value: Any) -> Sequence[Any]:
    if isinstance(value, Mapping):
        items = value.get("items")
    else:
        items = value
    if not isinstance(items, (list, tuple)):
        return []
    return items


def _parse_history(value: Any, thscode: str) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for raw in _history_items(value):
        if isinstance(raw, Mapping):
            raw_date = raw.get("date", raw.get("time"))
            raw_close = raw.get("close")
        elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
            raw_date, raw_close = raw[0], raw[2]
        else:
            continue
        try:
            session = _coerce_date(str(raw_date or "")[:10], f"{thscode}.date")
            close = _finite_number(raw_close, f"{thscode}.{session}.close", positive=True)
        except ValueError:
            continue
        parsed[session.isoformat()] = close
    return dict(sorted(parsed.items()))


def _parse_calendar(calendar_dates: Sequence[Any]) -> list[str]:
    parsed: set[str] = set()
    for value in calendar_dates:
        raw = value.get("date", value.get("time")) if isinstance(value, Mapping) else value
        try:
            parsed.add(_coerce_date(str(raw or "")[:10], "calendar date").isoformat())
        except ValueError:
            continue
    return sorted(parsed)


def _latest_close_on_or_before(
    sorted_dates: list[str], closes: Mapping[str, float], session: str
) -> tuple[str, float] | None:
    # The requested window is small; a reverse scan keeps this helper dependency-free.
    for candidate in reversed(sorted_dates):
        if candidate <= session:
            return candidate, float(closes[candidate])
    return None


def _is_provisional_session(session: str, generated_at: datetime) -> bool:
    return session == generated_at.date().isoformat() and generated_at.timetz().replace(
        tzinfo=None
    ) < daytime(15, 5)


def build_daily_pnl_history_payload(
    portfolio: Mapping[str, Any],
    histories: Mapping[str, Any],
    calendar_dates: Sequence[Any],
    *,
    start_date: Any = DEFAULT_START_DATE,
    end_date: Any | None = None,
    generated_at: datetime | None = None,
    source_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure aggregation layer used by the HTTP service and unit tests."""

    now = generated_at or _now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    else:
        now = now.astimezone(CN_TZ)
    start, end = validate_date_range(
        start_date,
        end_date or now.date(),
        today=now.date(),
    )
    positions = aggregate_positions(portfolio)
    configured_count = len(positions)
    all_calendar = _parse_calendar(calendar_dates)
    sessions = [value for value in all_calendar if start.isoformat() <= value <= end.isoformat()]
    if not sessions:
        raise DailyPnlHistoryUnavailable("no exchange sessions in the requested range")
    previous_sessions = [value for value in all_calendar if value < sessions[0]]
    if not previous_sessions:
        raise DailyPnlHistoryUnavailable("exchange calendar lacks the previous close session")

    parsed_histories: dict[str, dict[str, float]] = {}
    listing_dates: dict[str, str] = {}
    for position in positions:
        thscode = position["thscode"]
        closes = _parse_history(histories.get(thscode), thscode)
        parsed_histories[thscode] = closes
        explicit_listing = str(position.get("listing_date") or "")
        if explicit_listing:
            listing_dates[thscode] = explicit_listing
        elif closes:
            listing_dates[thscode] = next(iter(closes))

    records: list[dict[str, Any]] = []
    cumulative_price = 0.0
    covered_cumulative = 0.0
    strict_cumulative_complete = True
    previous_session = previous_sessions[-1]
    partial_dates: list[str] = []
    inferred_unlisted: set[str] = set()

    for session in sessions:
        covered_price = 0.0
        covered_count = 0
        eligible_count = 0
        current_value = 0.0
        current_value_covered = 0
        missing_symbols: list[str] = []
        carried_forward_symbols: list[str] = []
        unlisted_symbols: list[str] = []
        listing_session_symbols: list[str] = []

        for position in positions:
            thscode = position["thscode"]
            listing_date = listing_dates.get(thscode)
            # A non-empty Tencent series begins on the IPO session when the
            # instrument has fewer rows than the requested lookback.  Before
            # that first bar it is not part of the eligible static portfolio.
            if listing_date and session < listing_date:
                unlisted_symbols.append(thscode)
                inferred_unlisted.add(thscode)
                continue
            eligible_count += 1
            closes = parsed_histories[thscode]
            sorted_dates = list(closes)
            current = _latest_close_on_or_before(sorted_dates, closes, session)
            previous = _latest_close_on_or_before(sorted_dates, closes, previous_session)
            if current is not None:
                current_value += float(position["quantity"]) * current[1]
                current_value_covered += 1
            if current is None or previous is None:
                # On the instrument's very first bar there is no prior close to
                # compare against, so the session simply has no price move to
                # contribute — it is a zero-contribution session, not a data
                # gap.  Treating it as missing would null the cumulative P&L
                # for every later date (the series can never be "strict"
                # again), which is far worse than a single flat day.
                if (
                    current is not None
                    and previous is None
                    and listing_date
                    and current[0] == listing_date
                ):
                    covered_count += 1
                    listing_session_symbols.append(thscode)
                    if current[0] != session:
                        carried_forward_symbols.append(thscode)
                    continue
                missing_symbols.append(thscode)
                continue
            contribution = float(position["quantity"]) * (current[1] - previous[1])
            if not math.isfinite(contribution):
                missing_symbols.append(thscode)
                continue
            covered_price += contribution
            covered_count += 1
            if current[0] != session:
                carried_forward_symbols.append(thscode)

        complete = covered_count == eligible_count
        price_pnl = round(covered_price, 2) if complete else None
        # Cash distributions are deliberately not collected.  Unknown must
        # remain null: zero would incorrectly assert that none occurred.
        known_distribution = None
        daily_pnl = price_pnl
        covered_daily = round(covered_price, 2)
        covered_cumulative += covered_daily
        if daily_pnl is None:
            strict_cumulative_complete = False
            partial_dates.append(session)
        elif strict_cumulative_complete:
            cumulative_price += daily_pnl

        if not complete:
            status = "partial"
        elif _is_provisional_session(session, now):
            status = "provisional"
        else:
            status = "final"
        coverage_pct = 100.0 if eligible_count == 0 else covered_count / eligible_count * 100.0
        records.append(
            {
                "trade_date": session,
                "price_pnl_cny": price_pnl,
                "covered_price_pnl_cny": round(covered_price, 2),
                "known_distribution_cny": known_distribution,
                "cash_adjustment_status": "not_collected",
                "daily_pnl_cny": daily_pnl,
                "covered_daily_pnl_cny": covered_daily,
                "cumulative_pnl_cny": (
                    round(cumulative_price, 2) if strict_cumulative_complete else None
                ),
                "covered_cumulative_pnl_cny": round(covered_cumulative, 2),
                "market_value_cny": (
                    round(current_value, 2)
                    if current_value_covered == eligible_count
                    else None
                ),
                "covered_market_value_cny": round(current_value, 2),
                "coverage_pct": round(coverage_pct, 2),
                "status": status,
                "source": TENCENT_SOURCE_ID,
                "configured_count": configured_count,
                "eligible_count": eligible_count,
                "covered_count": covered_count,
                "eligible_positions": eligible_count,
                "covered_positions": covered_count,
                "unlisted_positions": len(unlisted_symbols),
                "missing_positions": len(missing_symbols),
                "unlisted_symbols": sorted(unlisted_symbols),
                "missing_symbols": sorted(missing_symbols),
                "listing_session_symbols": sorted(listing_session_symbols),
                "carried_forward_symbols": sorted(carried_forward_symbols),
            }
        )
        previous_session = session

    statuses = source_status if isinstance(source_status, Mapping) else {}
    source_errors = sorted(
        thscode
        for thscode, status in statuses.items()
        if isinstance(status, Mapping) and status.get("ok") is not True
    )
    warnings = [
        "这是按当前持仓数量回放的价格盈亏估算，不是券商账户历史盈亏。",
        "未计入历史买卖、税费、融资利息、出入金和现金分红。",
    ]
    if partial_dates:
        warnings.append(
            f"{len(partial_dates)} 个交易日覆盖不完整；这些日期的总盈亏和严格累计值留空。"
        )
    if inferred_unlisted:
        warnings.append("上市前交易日已从对应证券的可计算集合中排除。")
    if source_errors:
        warnings.append(f"{len(source_errors)} 个持仓行情源失败，相关日期按缺失处理。")

    final_count = sum(record["status"] == "final" for record in records)
    provisional_count = sum(record["status"] == "provisional" for record in records)
    strict_period_total = None if partial_dates else round(covered_cumulative, 2)
    return {
        "schema_version": API_SCHEMA_VERSION,
        "ok": True,
        "available": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "latest_trade_date": records[-1]["trade_date"],
        "portfolio_binding": {
            "portfolio_id": str(portfolio.get("portfolio_id") or ""),
            "portfolio_as_of": str(portfolio.get("as_of") or ""),
            "fingerprint_sha256": portfolio_fingerprint_sha256(portfolio),
            "configured_position_count": configured_count,
        },
        "period_total_pnl_cny": strict_period_total,
        "period_covered_pnl_cny": round(covered_cumulative, 2),
        "period_totals": {
            "price_pnl_cny": strict_period_total,
            "known_distribution_cny": None,
            "daily_pnl_cny": strict_period_total,
            "covered_daily_pnl_cny": round(covered_cumulative, 2),
            "complete_price_coverage": not partial_dates,
            "cash_adjustment_complete": False,
            "note": (
                "完整区间总额因存在缺口而留空；covered_daily_pnl_cny 仅汇总可覆盖的价格盈亏。"
                if partial_dates
                else "区间总额为原始收盘价差的静态持仓回放；未含现金分红。"
            ),
        },
        "basis": {
            "calculation_version": CALCULATION_VERSION,
            "method": "current configured quantity × (raw close - previous raw close)",
            "quantity_basis": "current-portfolio-static-replay",
            "price_basis": "tencent-unadjusted-daily-close",
            "price_adjustment": "none",
            "cash_distribution_adjustment": "none-not-collected",
            "currency": str(portfolio.get("currency") or "CNY"),
            "excludes": [
                "historical trades",
                "taxes and fees",
                "financing interest",
                "cash deposits and withdrawals",
                "cash dividends and other distributions",
                "realized P&L",
            ],
            "display_note": "当前持仓静态回放估算（原始收盘价口径），非券商真实历史盈亏",
        },
        "quality": {
            "status": "limited" if partial_dates or source_errors else "good",
            "record_count": len(records),
            "final_count": final_count,
            "provisional_count": provisional_count,
            "partial_count": len(partial_dates),
            "partial_dates": partial_dates,
            "strict_cumulative_complete": not partial_dates,
            "distribution_coverage": "not_collected",
            "source_errors": source_errors,
            "source_status": copy.deepcopy(dict(statuses)),
        },
        "records": records,
        "warnings": warnings,
    }


def fetch_tencent_raw_daily(symbol: str, count: int) -> dict[str, Any]:
    """Fetch only Tencent's raw ``day`` key; never accept qfq as raw.

    主机轮换：web.ifzq 失败后自动换 ifzq 备机，全部失败才抛错（不写负面
    结果，调用方收到异常后由上层降级到新浪备源）。
    """

    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", str(symbol or "")):
        raise ValueError("unsupported Tencent symbol")
    requested = max(2, min(int(count), MAX_HISTORY_BARS))
    last_error: Exception | None = None
    payload: Any = None
    for host in TENCENT_RAW_KLINE_HOSTS:
        try:
            response = _http_session().get(
                host,
                params={"param": f"{symbol},day,,,{requested}"},
                timeout=(3.5, 8.0),
            )
            response.raise_for_status()
            payload = response.json()
            break
        except Exception as exc:
            last_error = exc
            continue
    else:
        raise RuntimeError(
            f"Tencent raw daily hosts unavailable: {type(last_error).__name__}: {last_error}"
        )
    if not isinstance(payload, Mapping):
        raise RuntimeError("Tencent raw daily response is not an object")
    body = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = body.get("day") if isinstance(body, Mapping) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RuntimeError(f"Tencent returned no raw daily bars for {symbol}")
    items: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            continue
        try:
            session = _coerce_date(str(raw[0])[:10], "Tencent trade date")
            close = _finite_number(raw[2], "Tencent raw close", positive=True)
        except ValueError:
            continue
        items.append({"date": session.isoformat(), "close": close})
    deduplicated = {item["date"]: item for item in items}
    items = [deduplicated[key] for key in sorted(deduplicated)]
    if not items:
        raise RuntimeError(f"Tencent returned no valid raw daily bars for {symbol}")
    return {
        "symbol": symbol,
        "adjustment": "none",
        "source": TENCENT_SOURCE_ID,
        "requested_count": requested,
        "items": items,
    }


def fetch_sina_raw_daily(symbol: str, count: int) -> dict[str, Any]:
    """新浪 getKLineData 未复权日K跨源备援（腾讯系全部失败时启用）。

    scale=240 即日线、不复权，与腾讯 ``day`` 键语义一致（都是真实成交价），
    可安全接替静态回放；datalen 上限 1023 覆盖 MAX_HISTORY_BARS=640。
    新浪必须带 finance.sina.com.cn 的 Referer，否则 403。
    """

    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", str(symbol or "")):
        raise ValueError("unsupported Sina symbol")
    requested = max(2, min(int(count), MAX_HISTORY_BARS))
    response = _http_session().get(
        SINA_RAW_KLINE,
        params={"symbol": symbol, "scale": "240", "ma": "no",
                "datalen": str(requested)},
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=(3.5, 8.0),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Sina raw daily response is not a list")
    items: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, Mapping):
            continue
        try:
            session = _coerce_date(str(raw.get("day") or "")[:10], "Sina trade date")
            close = _finite_number(raw.get("close"), "Sina raw close", positive=True)
        except ValueError:
            continue
        items.append({"date": session.isoformat(), "close": close})
    deduplicated = {item["date"]: item for item in items}
    items = [deduplicated[key] for key in sorted(deduplicated)]
    if not items:
        raise RuntimeError(f"Sina returned no valid raw daily bars for {symbol}")
    return {
        "symbol": symbol,
        "adjustment": "none",
        "source": SINA_SOURCE_ID,
        "requested_count": requested,
        "items": items,
    }


def fetch_unadjusted_daily(symbol: str, count: int) -> dict[str, Any]:
    """未复权日K主备线：腾讯 kline/kline 双主机 → 新浪 getKLineData。

    两级同为不复权口径；结果 ``source`` 字段标明实际来源，上层状态区
    原样透传，不做静默混用。最近一次生效的层级会登记到
    ``last_fetch_chain_status()``，供 /api/health 展示。
    """

    try:
        result = fetch_tencent_raw_daily(symbol, count)
    except Exception as exc:
        result = fetch_sina_raw_daily(symbol, count)
        _record_fetch_chain_status("backup", SINA_SOURCE_ID, type(exc).__name__)
        return result
    _record_fetch_chain_status("primary", TENCENT_SOURCE_ID)
    return result


_LAST_FETCH_LOCK = threading.Lock()
_LAST_FETCH_STATUS: dict[str, Any] = {}


def _record_fetch_chain_status(tier: str, source: str, detail: str = "") -> None:
    with _LAST_FETCH_LOCK:
        _LAST_FETCH_STATUS.update({
            "chain": "daily_pnl_history",
            "tier": tier,  # primary=主源 backup=备源
            "source": source,
            "detail": detail,
            "updated_at": _now().isoformat(timespec="seconds"),
        })


def last_fetch_chain_status() -> dict[str, Any]:
    """最近一次未复权日K抓取实际生效的主备层级（只读快照）。"""

    with _LAST_FETCH_LOCK:
        return copy.deepcopy(_LAST_FETCH_STATUS)


def _history_count(start: date, end: date) -> int:
    """Number of daily bars to request so the window covers ``start``.

    The upstream endpoints return the **last N bars ending today**, not the N
    bars inside ``[start, end]``.  Sizing the request by the requested range
    alone therefore returns a window that starts far too late whenever
    ``end`` is in the past, and the exchange calendar lookback comes up
    short.  The count must cover every session from ``start`` up to today.
    """

    today = _now().date()
    window_end = max(end, today)
    approximate_sessions = int((window_end - start).days * 5 / 7) + 35
    return max(MIN_HISTORY_BARS, min(MAX_HISTORY_BARS, approximate_sessions))


HistoryFetcher = Callable[[str, int], Mapping[str, Any]]


def fetch_portfolio_histories(
    positions: Sequence[Mapping[str, Any]],
    *,
    start_date: date,
    end_date: date,
    fetcher: HistoryFetcher = fetch_unadjusted_daily,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    count = _history_count(start_date, end_date)
    targets = {CALENDAR_SYMBOL: "__calendar__"}
    for position in positions:
        targets[str(position["symbol"])] = str(position["thscode"])
    results: dict[str, Any] = {}
    status: dict[str, Any] = {}
    max_workers = max(1, min(5, len(targets)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="daily-pnl") as pool:
        futures = {
            pool.submit(fetcher, symbol, count): (symbol, key)
            for symbol, key in targets.items()
        }
        for future in as_completed(futures):
            symbol, key = futures[future]
            try:
                value = dict(future.result())
                items = list(_history_items(value))
                if not items:
                    raise RuntimeError("empty history")
                results[key] = value
                status[key] = {
                    "ok": True,
                    "source": str(value.get("source") or TENCENT_SOURCE_ID),
                    "adjustment": str(value.get("adjustment") or ""),
                    "count": len(items),
                }
            except Exception as exc:
                status[key] = {
                    "ok": False,
                    "source": TENCENT_SOURCE_ID,
                    "adjustment": "none",
                    "error_type": type(exc).__name__,
                }

    calendar_result = results.pop("__calendar__", None)
    calendar_status = status.pop("__calendar__", None)
    if calendar_result is None:
        raise DailyPnlHistoryUnavailable("exchange calendar source is unavailable")
    calendar_dates = [
        str(item.get("date") or item.get("time") or "")[:10]
        for item in _history_items(calendar_result)
        if isinstance(item, Mapping)
    ]
    parsed_calendar = _parse_calendar(calendar_dates)
    if not any(value < start_date.isoformat() for value in parsed_calendar):
        raise DailyPnlHistoryUnavailable("exchange calendar lookback is insufficient")
    status["calendar"] = calendar_status or {
        "ok": True,
        "source": TENCENT_SOURCE_ID,
        "adjustment": "none",
        "count": len(parsed_calendar),
    }
    return results, parsed_calendar, status


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_CACHE_BYTES:
        raise ValueError("daily P&L cache exceeds its size limit")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("daily P&L cache root must be an object")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    body = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(body) > MAX_CACHE_BYTES:
        raise ValueError("daily P&L cache exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _cache_payload_valid(
    envelope: Mapping[str, Any],
    *,
    fingerprint: str,
    start_date: date,
    end_date: date,
    allow_older_end: bool = False,
) -> dict[str, Any] | None:
    raw_cached_end = str(envelope.get("end_date") or "")
    try:
        cached_end = _coerce_date(raw_cached_end, "cache end")
    except ValueError:
        return None
    end_matches = cached_end == end_date or (
        allow_older_end and start_date <= cached_end < end_date
    )
    if (
        envelope.get("schema_version") != CACHE_SCHEMA_VERSION
        or envelope.get("calculation_version") != CALCULATION_VERSION
        or str(envelope.get("portfolio_fingerprint_sha256") or "") != fingerprint
        or str(envelope.get("start_date") or "") != start_date.isoformat()
        or not end_matches
    ):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != API_SCHEMA_VERSION or payload.get("ok") is not True:
        return None
    if str(payload.get("end_date") or "") != cached_end.isoformat():
        return None
    binding = payload.get("portfolio_binding")
    if not isinstance(binding, Mapping) or binding.get("fingerprint_sha256") != fingerprint:
        return None
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return None
    dates = [str(record.get("trade_date") or "") for record in records if isinstance(record, Mapping)]
    if len(dates) != len(records) or dates != sorted(set(dates)):
        return None
    if dates[0] < start_date.isoformat() or dates[-1] > end_date.isoformat():
        return None
    return copy.deepcopy(dict(payload))


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _payload_has_recoverable_gaps(payload: Mapping[str, Any]) -> bool:
    """Whether a payload carries source failures or unfinalized records.

    A completed, source-clean history is stable and may be reused long-term.
    A history that was built while a data source failed, or that still ends on
    an in-session provisional record, is only a degraded snapshot: it must be
    allowed to expire so a later request can rebuild it.

    A payload that contains *partial* sessions is degraded in the same way:
    those sessions were missing a symbol, and the next rebuild may well fetch
    the data that was missing.  Without this check a single partial session
    would freeze the cached payload forever.
    """

    quality = payload.get("quality")
    if isinstance(quality, Mapping):
        if quality.get("source_errors"):
            return True
        if quality.get("partial_count"):
            return True
        partial_dates = quality.get("partial_dates")
        if isinstance(partial_dates, list) and partial_dates:
            return True
    records = payload.get("records")
    if isinstance(records, list) and records:
        latest = records[-1]
        if isinstance(latest, Mapping) and str(latest.get("status") or "") == "provisional":
            return True
    return False


def _cache_fresh(payload: Mapping[str, Any], now: datetime, end_date: date) -> bool:
    generated = _parse_datetime(payload.get("generated_at"))
    if generated is None or generated > now + timedelta(minutes=5):
        return False
    if end_date < now.date():
        # Stable history may be reused long-term, but a snapshot that carries
        # source failures or a provisional tail must stay rebuildable (B06).
        return not _payload_has_recoverable_gaps(payload)
    age = max(0.0, (now - generated).total_seconds())
    if age <= CURRENT_DAY_CACHE_TTL_SECONDS:
        return True
    latest = (payload.get("records") or [{}])[-1]
    return bool(
        isinstance(latest, Mapping)
        and latest.get("trade_date") == now.date().isoformat()
        and latest.get("status") == "final"
        and generated.date() == now.date()
    )


def _with_cache_metadata(
    payload: Mapping[str, Any],
    *,
    source: str,
    fresh: bool,
    stale_reason: str = "",
    requested_end_date: date | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    result["stale"] = not fresh
    result["cache"] = {
        "source": source,
        "fresh": fresh,
        "stale": not fresh,
        "path_kind": "single-bounded-atomic-file",
        "max_bytes": MAX_CACHE_BYTES,
        "cached_end_date": str(payload.get("end_date") or ""),
    }
    if requested_end_date is not None:
        result["cache"]["requested_end_date"] = requested_end_date.isoformat()
    if stale_reason:
        result["cache"]["stale_reason"] = stale_reason
        result["warnings"] = list(
            dict.fromkeys(
                [
                    *(result.get("warnings") or []),
                    "最新行情刷新失败，当前显示与本组合绑定的最近一次缓存。",
                ]
            )
        )
    return result


class DailyPnlHistoryStore:
    """Single-flight builder with one bounded, portfolio-bound cache file."""

    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        *,
        fetcher: HistoryFetcher = fetch_unadjusted_daily,
        now_fn: Callable[[], datetime] = _now,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.fetcher = fetcher
        self.now_fn = now_fn
        self._lock = threading.Lock()
        self.last_error = ""

    def _load_cache(
        self,
        *,
        fingerprint: str,
        start_date: date,
        end_date: date,
        allow_older_end: bool = False,
    ) -> dict[str, Any] | None:
        if not self.cache_path.is_file():
            return None
        try:
            envelope = _read_json(self.cache_path)
            return _cache_payload_valid(
                envelope,
                fingerprint=fingerprint,
                start_date=start_date,
                end_date=end_date,
                allow_older_end=allow_older_end,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _build(
        self,
        portfolio: Mapping[str, Any],
        *,
        start_date: date,
        end_date: date,
        now: datetime,
    ) -> dict[str, Any]:
        positions = aggregate_positions(portfolio)
        histories, calendar_dates, source_status = fetch_portfolio_histories(
            positions,
            start_date=start_date,
            end_date=end_date,
            fetcher=self.fetcher,
        )
        return build_daily_pnl_history_payload(
            portfolio,
            histories,
            calendar_dates,
            start_date=start_date,
            end_date=end_date,
            generated_at=now,
            source_status=source_status,
        )

    def peek(
        self,
        portfolio: Mapping[str, Any],
        *,
        start: Any = DEFAULT_START_DATE,
        end: Any | None = None,
        allow_older_end: bool = False,
    ) -> dict[str, Any] | None:
        """Return the fresh cached payload for this portfolio, or ``None``.

        Unlike :meth:`get` this never triggers a network build; it exists so
        aggregation endpoints (for example cross-portfolio comparison) can read
        whatever history is already cached without blocking on remote fetches.
        With ``allow_older_end`` a cache whose end date trails the requested
        end (for example right after midnight, before the next refresh) still
        counts as a hit; all other integrity checks stay exact.
        """

        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=CN_TZ)
        else:
            now = now.astimezone(CN_TZ)
        try:
            start_date, end_date = validate_date_range(
                start,
                end or now.date(),
                today=now.date(),
            )
        except ValueError:
            return None
        fingerprint = portfolio_fingerprint_sha256(portfolio)
        cached = self._load_cache(
            fingerprint=fingerprint,
            start_date=start_date,
            end_date=end_date,
            allow_older_end=allow_older_end,
        )
        if cached is None:
            return None
        return _with_cache_metadata(cached, source="disk", fresh=_cache_fresh(cached, now, end_date))

    def get(
        self,
        portfolio: Mapping[str, Any],
        *,
        start: Any = DEFAULT_START_DATE,
        end: Any | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=CN_TZ)
        else:
            now = now.astimezone(CN_TZ)
        start_date, end_date = validate_date_range(
            start,
            end or now.date(),
            today=now.date(),
        )
        fingerprint = portfolio_fingerprint_sha256(portfolio)
        cached = self._load_cache(
            fingerprint=fingerprint,
            start_date=start_date,
            end_date=end_date,
        )
        if cached is not None and not force and _cache_fresh(cached, now, end_date):
            return _with_cache_metadata(cached, source="disk", fresh=True)

        with self._lock:
            now = self.now_fn()
            if now.tzinfo is None:
                now = now.replace(tzinfo=CN_TZ)
            else:
                now = now.astimezone(CN_TZ)
            exact_cached = self._load_cache(
                fingerprint=fingerprint,
                start_date=start_date,
                end_date=end_date,
            )
            if (
                exact_cached is not None
                and not force
                and _cache_fresh(exact_cached, now, end_date)
            ):
                return _with_cache_metadata(exact_cached, source="disk", fresh=True)
            cached = exact_cached or self._load_cache(
                fingerprint=fingerprint,
                start_date=start_date,
                end_date=end_date,
                allow_older_end=True,
            )
            try:
                payload = self._build(
                    portfolio,
                    start_date=start_date,
                    end_date=end_date,
                    now=now,
                )
                envelope = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "calculation_version": CALCULATION_VERSION,
                    "saved_at": now.isoformat(timespec="seconds"),
                    "portfolio_fingerprint_sha256": fingerprint,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "payload": payload,
                }
                _atomic_write_json(self.cache_path, envelope)
                self.last_error = ""
                return _with_cache_metadata(payload, source="computed", fresh=True)
            except Exception as exc:
                self.last_error = type(exc).__name__
                if cached is not None:
                    return _with_cache_metadata(
                        cached,
                        source="stale-disk",
                        fresh=False,
                        stale_reason=type(exc).__name__,
                        requested_end_date=end_date,
                    )
                raise DailyPnlHistoryUnavailable(
                    "daily P&L history refresh failed"
                ) from exc


__all__ = [
    "API_SCHEMA_VERSION",
    "CACHE_SCHEMA_VERSION",
    "CALCULATION_VERSION",
    "DEFAULT_CACHE_PATH",
    "DEFAULT_START_DATE",
    "DailyPnlHistoryStore",
    "DailyPnlHistoryUnavailable",
    "aggregate_positions",
    "build_daily_pnl_history_payload",
    "fetch_portfolio_histories",
    "fetch_sina_raw_daily",
    "fetch_tencent_raw_daily",
    "fetch_unadjusted_daily",
    "last_fetch_chain_status",
    "validate_date_range",
]
