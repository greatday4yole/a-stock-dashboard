#!/usr/bin/env python3
"""Reconstruct historical intraday portfolio P&L from per-stock minute bars.

The intraday P&L ledger (``intraday_pnl_timeseries``) only contains points that
were recorded live while the dashboard was polling quotes.  Days (or parts of
days) missed because the server was down are permanently absent from the
ledger.  This module fills those holes from an external source: Tencent's
five-minute kline (``kline/mkline``, unadjusted) plus Tencent's forward
adjusted daily kline (``newfqkline/get`` qfq).

Design constraints:

* The live ledger is immutable and integrity-chained; backfill never writes
  into it.  Reconstructed data is served from derived caches only and merged
  at read time, with per-point provenance (``source``).
* One fetch window per symbol covers every requested date: two HTTP requests
  per holding regardless of how many days are backfilled.
* The true previous close used by the live recorder is the exchange's
  ex-dividend adjusted previous close.  For a trade date ``T`` it is recovered
  exactly from the adjusted daily series as
  ``prev_close(T) = raw_close(T) * fq_close(T-1) / fq_close(T)``
  which equals ``raw_close(T-1)`` on ordinary days and the ex-dividend
  previous close on ex-dividend days.
* Quantities are NOT baked into the cache: the cache stores per-symbol price
  windows only.  Day curves are composed at read time with the quantities of
  that date (the ledger's own holdings for recorded days, the current
  portfolio config as a static estimate for missing days), so rebalancing
  between the recorded past and today does not corrupt the reconstruction.
* The per-symbol window cache is cumulative: each fetch is merged with the
  previous window, so closed trading days persist locally forever.  Re-fetches
  happen only for dates the cache does not cover or for the still-moving
  latest session — a backfilled day is never downloaded twice.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from scripts.intraday_pnl_timeseries import CN_TZ


BACKFILL_SCHEMA = "intraday-pnl.backfill.v1"
SYMBOL_WINDOW_SCHEMA = "intraday-pnl.backfill.symbol-window.v1"

TENCENT_MKLINE_HOSTS = (
    "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
)
TENCENT_FQKLINE_HOSTS = (
    # 2026-10-11 起 fqkline/get 双主机被 WAF 501 拦截，前复权日K改走 newfqkline
    # （需显式起止日期，见 _fq_daily_param）。
    # 三主机顺序与 derivatives_regime.py 一致：proxy.finance.qq.com 与前两台
    # 不同域名（独立网关），WAF 封禁粒度是「IP+主机」，是真正独立的第三条线；
    # _STICKY_HOSTS 的粘性轮换会自动把它纳入轮换范围。
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _fq_daily_param(day_count: int) -> tuple[str, str]:
    """newfqkline 需要显式起止日期；按 bar 数推算（交易日≈日历日×0.62）。"""

    from datetime import date as _date, timedelta as _timedelta

    end_day = _date.today() + _timedelta(days=1)
    start_day = end_day - _timedelta(days=int(day_count * 1.6) + 40)
    return start_day.isoformat(), end_day.isoformat()

M5_COUNT = 800          # ≈ 17 trading days of 48 five-minute bars
DAY_COUNT = 60          # daily qfq bars, covers one extra day before the window
MAX_BACKFILL_DATES = 40
# 仅约束"最新交易日"的缓存：盘中 5 分钟线仍在增长，超过该时长需重新拉取；
# 已收盘日期的 K 线不可变，合并进缓存后永久可用，不受此限制。
SYMBOL_WINDOW_MAX_AGE_SECONDS = 6 * 3600.0
_MATCH_TOLERANCE_SECONDS = 150.0
_CHECK_AMPLITUDE_RATIO = 0.12
_CHECK_MARKET_VALUE_RATIO = 0.0002
_CORRECTION_POINT_RATIO = 0.20

# 做T规则（与逐股日内特征分析同一套）：
#   入场：开盘后 1 小时内（第 1~11 根 5 分钟线）较当日开盘价逆向变动 ≥ 阈值；
#   了结：较入场价同向有利变动 ≥ 阈值，否则收盘平（completed=False）；
#   阈值：0.6 × 该股窗口中位振幅，夹在 [0.5%, 1.5%]。
_T_ENTRY_SCAN_BARS = 12
_T_AMPLITUDE_RATIO = 0.6
_T_THRESHOLD_MIN_PCT = 0.5
_T_THRESHOLD_MAX_PCT = 1.5
_T_PROFILE_MIN_DAYS = 5
_T_SUITABLE_MIN_AMP_PCT = 1.0
_T_SUITABLE_MIN_PER_DAY_BPS = 5.0
_T_PROFILE_CACHE: Dict[str, tuple[str, Dict[str, Any]]] = {}
_T_PROFILE_CACHE_LOCK = threading.Lock()

_THSCODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")

_HTTP_LOCK = threading.Lock()
_HTTP_LAST_CALL = 0.0
_HTTP_MIN_INTERVAL_SECONDS = 0.35
_STICKY_HOSTS: Dict[str, int] = {}
_SESSION = None


class IntradayPnlBackfillError(RuntimeError):
    """Base error with a stable code for HTTP integration."""

    code = "INTRADAY_PNL_BACKFILL_ERROR"


class BackfillSourceError(IntradayPnlBackfillError):
    code = "BACKFILL_SOURCE_UNAVAILABLE"


class BackfillInputError(IntradayPnlBackfillError):
    code = "BACKFILL_INVALID_INPUT"


def _session():
    global _SESSION
    if _SESSION is None:
        import requests

        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"})
        _SESSION = session
    return _SESSION


def _throttle() -> None:
    global _HTTP_LAST_CALL
    with _HTTP_LOCK:
        wait = _HTTP_MIN_INTERVAL_SECONDS - (time.monotonic() - _HTTP_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _HTTP_LAST_CALL = time.monotonic()


def _fetch_json(purpose: str, hosts: Sequence[str], params: Mapping[str, Any]) -> Dict[str, Any]:
    """Sticky-host GET with rotation, mirroring the dashboard's kline client."""

    session = _session()
    start_index = _STICKY_HOSTS.get(purpose, 0)
    last_error: Optional[Exception] = None
    for offset in range(len(hosts)):
        index = (start_index + offset) % len(hosts)
        _throttle()
        try:
            response = session.get(hosts[index], params=dict(params), timeout=(2.5, 6.0))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("upstream returned a non-object payload")
            _STICKY_HOSTS[purpose] = index
            return payload
        except Exception as exc:
            last_error = exc
            continue
    raise BackfillSourceError(f"{purpose} upstream failed: {last_error}")


def tencent_symbol(thscode: str) -> str:
    matched = _THSCODE_RE.fullmatch(str(thscode or "").strip().upper())
    if not matched:
        raise BackfillInputError(f"unsupported symbol for backfill: {thscode!r}")
    return f"{matched.group(2).lower()}{matched.group(1)}"


def _parse_m5_stamp(raw: Any) -> Optional[tuple[str, str]]:
    """``YYYYMMDDHHMM`` bar-end stamp → (iso_time, trade_date)."""

    text = str(raw or "").strip()
    if not re.fullmatch(r"\d{12}", text):
        return None
    try:
        parsed = datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=CN_TZ)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds"), parsed.date().isoformat()


def _bar_close(raw: Any) -> Optional[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    try:
        value = float(raw[2])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _daily_close(row: Any) -> Optional[tuple[str, float]]:
    if not isinstance(row, (list, tuple)) or len(row) < 3:
        return None
    text = str(row[0] or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        close = float(row[2])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(close) or close <= 0:
        return None
    return text, close


def _prev_close_from_daily(
    bars: Mapping[str, Sequence[Sequence[Any]]], fq_close: Mapping[str, float]
) -> Dict[str, float]:
    """Recover the exchange's ex-dividend previous close per trade date.

    ``prev_close(T) = raw_close(T) * fq_close(T-1) / fq_close(T)`` collapses to
    ``raw_close(T-1)`` on ordinary days and to the ex-dividend previous close
    when an adjustment event sits between ``T-1`` and ``T``.
    """

    prev_close: Dict[str, float] = {}
    fq_dates = sorted(fq_close)
    for trade_date, day_bars in bars.items():
        if not day_bars:
            continue
        raw_last_close = float(day_bars[-1][1])
        fq_today = fq_close.get(trade_date)
        earlier = [value for value in fq_dates if value < trade_date]
        fq_prev = fq_close[earlier[-1]] if earlier else None
        if fq_today and fq_prev and fq_today > 0:
            prev_close[trade_date] = round(raw_last_close * fq_prev / fq_today, 6)
    return prev_close


def fetch_symbol_window(
    thscode: str,
    *,
    m5_count: int = M5_COUNT,
    day_count: int = DAY_COUNT,
) -> Dict[str, Any]:
    """One five-minute window plus one adjusted daily window for a symbol."""

    symbol = tencent_symbol(thscode)
    m5_payload = _fetch_json(
        "backfill-m5",
        TENCENT_MKLINE_HOSTS,
        {"param": f"{symbol},m5,,{int(m5_count)}"},
    )
    m5_rows = ((m5_payload.get("data") or {}).get(symbol) or {}).get("m5") or []
    bars: Dict[str, List[List[Any]]] = {}
    for row in m5_rows if isinstance(m5_rows, list) else []:
        stamp = _parse_m5_stamp(row[0]) if isinstance(row, (list, tuple)) and row else None
        close = _bar_close(row)
        if stamp is None or close is None:
            continue
        iso_time, trade_date = stamp
        bars.setdefault(trade_date, []).append([iso_time, close])
    for day_bars in bars.values():
        day_bars.sort(key=lambda item: item[0])

    fq_start, fq_end = _fq_daily_param(int(day_count))
    fq_payload = _fetch_json(
        "backfill-fq",
        TENCENT_FQKLINE_HOSTS,
        {"param": f"{symbol},day,{fq_start},{fq_end},{int(day_count)},qfq"},
    )
    fq_body = (fq_payload.get("data") or {}).get(symbol) or {}
    fq_rows = fq_body.get("qfqday") or fq_body.get("day") or []
    fq_close: Dict[str, float] = {}
    for row in fq_rows if isinstance(fq_rows, list) else []:
        parsed = _daily_close(row)
        if parsed is not None:
            fq_close[parsed[0]] = parsed[1]

    dates = sorted(bars)
    if not dates:
        raise BackfillSourceError(f"no five-minute bars returned for {thscode}")
    return {
        "schema_version": SYMBOL_WINDOW_SCHEMA,
        "thscode": thscode,
        "source": "腾讯财经5分钟K线(未复权)+前复权日线",
        "fetched_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "window": {"start": dates[0], "end": dates[-1], "trade_days": len(dates)},
        "bars": bars,
        "prev_close": _prev_close_from_daily(bars, fq_close),
    }


def merge_symbol_windows(
    old: Optional[Mapping[str, Any]], new: Mapping[str, Any]
) -> Dict[str, Any]:
    """Union of a cached window and a fresh fetch; the new fetch wins per date.

    Upstream only returns the latest ``M5_COUNT`` bars, so a plain overwrite
    silently drops days that scrolled out of the rolling window — backfilled
    history would become permanently unrecoverable.  Merging keeps every past
    day forever; overlapping dates take the fresh fetch (intraday sessions
    accumulate bars, and revised data supersedes the old rows).
    """

    if not isinstance(old, dict) or not old.get("bars"):
        return dict(new)
    merged_bars: Dict[str, List[List[Any]]] = {
        trade_date: [list(bar) for bar in rows]
        for trade_date, rows in (old.get("bars") or {}).items()
        if isinstance(rows, list)
    }
    for trade_date, rows in (new.get("bars") or {}).items():
        if isinstance(rows, list):
            merged_bars[trade_date] = [list(bar) for bar in rows]
    merged_prev = dict(old.get("prev_close") or {})
    merged_prev.update(new.get("prev_close") or {})
    dates = sorted(merged_bars)
    if not dates:
        return dict(new)
    document = dict(new)
    document["bars"] = merged_bars
    document["prev_close"] = merged_prev
    document["window"] = {
        "start": dates[0],
        "end": dates[-1],
        "trade_days": len(dates),
    }
    return document


def _date_served(
    doc: Mapping[str, Any], trade_date: str, *, max_age_seconds: float
) -> bool:
    """Whether the cached window can serve ``trade_date`` without a fetch.

    A closed day's five-minute bars never change, so once the cache holds a
    full picture of that day — a fetch made on a later calendar day, or any
    later bar day in the window — the date is served forever regardless of
    cache age.  Only the most recent day (whose bars still accumulate during
    the session) is subject to the freshness window.
    """

    window = doc.get("window") or {}
    start = str(window.get("start") or "")
    end = str(window.get("end") or "")
    if not (start and end and start <= trade_date <= end):
        return False
    bars = doc.get("bars") or {}
    day_bars = bars.get(trade_date)
    if not day_bars:
        # The window covers this date but no bars were ever stored for it.
        # Treating it as served would silently omit the holding from that
        # day's series instead of triggering a repair fetch.
        return False
    if day_bars and trade_date not in (doc.get("prev_close") or {}):
        # Bars without the recovered previous close cannot compose a curve;
        # treat the date as an unfilled gap so a later fetch can repair it.
        return False
    fetched_raw = str(doc.get("fetched_at") or "")
    fetched_day = fetched_raw[:10]
    if fetched_day and trade_date < fetched_day:
        return True
    if any(day > trade_date for day in (doc.get("bars") or {})):
        return True
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError:
        return False
    age = (datetime.now(CN_TZ) - fetched_at).total_seconds()
    return -300 <= age <= max_age_seconds


def load_symbol_window(data_dir: Path, thscode: str) -> Optional[Dict[str, Any]]:
    path = data_dir / "symbols" / f"{thscode.replace('.', '-')}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("bars") else None


def save_symbol_window(data_dir: Path, document: Mapping[str, Any]) -> None:
    directory = data_dir / "symbols"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{str(document.get('thscode') or '').replace('.', '-')}.json"
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-window-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def ensure_symbol_windows(
    data_dir: Path,
    symbols: Sequence[str],
    need_dates: Sequence[str],
    *,
    force: bool = False,
    max_age_seconds: float = SYMBOL_WINDOW_MAX_AGE_SECONDS,
) -> Dict[str, Dict[str, Any]]:
    """Return per-symbol windows, fetching only dates the cache cannot serve.

    Closed days are served from the persistent cache forever (see
    ``_date_served`` and ``merge_symbol_windows``); a fetch happens only when a
    requested date is missing from the cache or is the still-moving latest day.
    """

    windows: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(set(str(value) for value in symbols if str(value)))
    for thscode in ordered:
        cached = load_symbol_window(data_dir, thscode)
        if (
            not force
            and cached is not None
            and all(_date_served(cached, value, max_age_seconds=max_age_seconds) for value in need_dates)
        ):
            windows[thscode] = cached
            continue
        try:
            document = fetch_symbol_window(thscode)
        except IntradayPnlBackfillError:
            if cached is not None:
                # Serve the stale window rather than losing the whole range,
                # but only when it actually covers the requested dates.
                if all(str((cached.get("window") or {}).get("start") or "") <= value <= str((cached.get("window") or {}).get("end") or "") for value in need_dates):
                    windows[thscode] = cached
                continue
            raise
        document = merge_symbol_windows(cached, document)
        save_symbol_window(data_dir, document)
        windows[thscode] = document
    return windows


def aggregate_holdings_current(portfolio: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Aggregate the current config's holdings by thscode (static estimate)."""

    from scripts.intraday_pnl_timeseries import _aggregate_holdings

    holdings, _snapshot, _series_id = _aggregate_holdings(
        portfolio, datetime.now(CN_TZ).date()
    )
    return holdings


def compose_day_series(
    windows: Mapping[str, Mapping[str, Any]],
    holdings: Sequence[Mapping[str, Any]],
    trade_date: str,
) -> Dict[str, Any]:
    """Compose one day's per-symbol and total P&L series from price windows.

    ``holdings`` entries need ``thscode``, ``name`` and ``quantity``.  A symbol
    without bars or a recoverable previous close for the date is reported as
    not covered instead of being silently dropped.
    """

    per_symbol: Dict[str, Dict[str, Any]] = {}
    totals: Dict[str, Dict[str, Any]] = {}
    covered_symbols = 0

    for holding in holdings:
        thscode = str(holding.get("thscode") or "")
        name = str(holding.get("name") or thscode)
        try:
            quantity = int(holding.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        window = windows.get(thscode) or {}
        day_bars = (window.get("bars") or {}).get(trade_date) or []
        prev_close = (window.get("prev_close") or {}).get(trade_date)
        series: List[Dict[str, Any]] = []
        if day_bars and prev_close and quantity > 0:
            covered_symbols += 1
            last_close = float(day_bars[-1][1])
            for iso_time, close in day_bars:
                pnl = quantity * (float(close) - float(prev_close))
                series.append({"time": iso_time, "pnl": round(pnl, 2)})
                slot = totals.setdefault(
                    iso_time, {"pnl": 0.0, "covered": 0}
                )
                slot["pnl"] += pnl
                slot["covered"] += 1
        else:
            last_close = None
        per_symbol[thscode] = {
            "thscode": thscode,
            "name": name,
            "quantity": quantity,
            "prev_close": prev_close,
            "last_close": last_close,
            "points": series,
            "covered": bool(series),
        }

    required = len([item for item in holdings if int(item.get("quantity") or 0) > 0])
    total_points = [
        {
            "time": iso_time,
            "pnl": round(slot["pnl"], 2),
            "covered": slot["covered"],
            "required": required,
        }
        for iso_time, slot in sorted(totals.items())
    ]
    return {
        "trade_date": trade_date,
        "per_symbol": per_symbol,
        "total": {"points": total_points, "covered_symbols": covered_symbols, "required_symbols": required},
        "buildable": bool(total_points),
    }


def _epoch_seconds(iso_time: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(iso_time)).timestamp()
    except (TypeError, ValueError):
        return None


def _with_stamps(points: Sequence[Mapping[str, Any]]) -> list[tuple[float, Mapping[str, Any]]]:
    """Parse each point's timestamp once so matching stays O(n+m), not O(n·m) parses."""

    stamped: list[tuple[float, Mapping[str, Any]]] = []
    for point in points:
        stamp = _epoch_seconds(str(point.get("time") or ""))
        if stamp is not None:
            stamped.append((stamp, point))
    return stamped


def _nearest_recorded(
    stamped: Sequence[tuple[float, Mapping[str, Any]]], moment: float
) -> Optional[Mapping[str, Any]]:
    best: Optional[tuple[float, Mapping[str, Any]]] = None
    for stamp, point in stamped:
        distance = abs(stamp - moment)
        if distance <= _MATCH_TOLERANCE_SECONDS and (best is None or distance < best[0]):
            best = (distance, point)
    return best[1] if best else None


def compare_and_correct(
    recorded_series: Mapping[str, Sequence[Mapping[str, Any]]],
    backfill_series: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    market_value_estimate: Optional[float] = None,
) -> Dict[str, Any]:
    """Compare recorded points against reconstructed bars, flag and cap fixes.

    Matching pairs a five-minute bar close with recorded points within
    ±150 seconds; the residual deviation is the intra-bar price drift plus any
    recording error, so the flag threshold is deliberately loose (12% of the
    day's reconstructed amplitude, floored at 0.02% of the estimated market
    value).  Corrections are capped: a structurally different day (for example
    an intraday rebalance) yields many large deviations and is reported
    without rewriting values.
    """

    report: Dict[str, Any] = {
        "checked": False,
        "series": {},
        "corrected_count": 0,
        "skipped_reason": "",
    }
    all_corrections: List[Dict[str, Any]] = []
    for key, backfill_points in backfill_series.items():
        recorded_points = recorded_series.get(key) or []
        if not backfill_points or not recorded_points:
            continue
        values = [float(point.get("pnl") or 0.0) for point in backfill_points]
        amplitude = max(values) - min(values)
        floor = amplitude * _CHECK_AMPLITUDE_RATIO
        if market_value_estimate:
            floor = max(floor, abs(market_value_estimate) * _CHECK_MARKET_VALUE_RATIO)
        if floor <= 0:
            continue
        deviations: List[Dict[str, Any]] = []
        matched = 0
        stamped_recorded = _with_stamps(recorded_points)
        for point in backfill_points:
            moment = _epoch_seconds(str(point.get("time") or ""))
            if moment is None:
                continue
            pair = _nearest_recorded(stamped_recorded, moment)
            if pair is None:
                continue
            matched += 1
            deviation = float(pair.get("pnl") or 0.0) - float(point.get("pnl") or 0.0)
            deviations.append(
                {
                    "time": point.get("time"),
                    "deviation": round(deviation, 2),
                    "backfill_pnl": round(float(point.get("pnl") or 0.0), 2),
                    "recorded_pnl": round(float(pair.get("pnl") or 0.0), 2),
                    "beyond": abs(deviation) > floor,
                }
            )
        if not matched:
            continue
        beyond = [item for item in deviations if item["beyond"]]
        worst = max(deviations, key=lambda item: abs(item["deviation"]))
        report["series"][key] = {
            "matched_points": matched,
            "amplitude": round(amplitude, 2),
            "floor": round(floor, 2),
            "max_deviation": {
                "value": worst["deviation"],
                "time": worst["time"],
                "pct_of_amplitude": round(abs(worst["deviation"]) / amplitude, 4) if amplitude > 0 else None,
            },
            "suspicious_count": len(beyond),
            "suspicious": beyond[:8],
        }
        all_corrections.extend(
            {
                "series": key,
                "time": item["time"],
                "recorded_pnl": item["recorded_pnl"],
                "corrected_pnl": item["backfill_pnl"],
                "deviation": item["deviation"],
            }
            for item in beyond
        )
    report["checked"] = bool(report["series"])
    total_matched = sum(item["matched_points"] for item in report["series"].values())
    if total_matched and len(all_corrections) > max(3, total_matched * _CORRECTION_POINT_RATIO):
        report["skipped_reason"] = "可疑点过多，疑似口径差异（如当日调仓），仅报告不矫正"
        return report
    report["corrections"] = all_corrections
    report["corrected_count"] = len(all_corrections)
    return report


def _t_sim_days(
    bars: Mapping[str, Sequence[Sequence[Any]]],
    days: Sequence[str],
    th_pct: float,
    direction: str,
) -> Dict[str, Any]:
    """Run the uniform T rule over days; returns stats plus per-day trades.

    Entry scans the first hour for an adverse move ≥ ``th_pct`` from the day's
    opening bar; exit takes the first favorable move of the same size, else the
    closing bar (``completed=False``).  Costs are excluded here (the caller
    applies stock/ETF round-trip costs when judging expectancy).
    """

    thp = th_pct / 100.0
    tries = wins = completed_n = 0
    bps_values: List[float] = []
    trades: List[Dict[str, Any]] = []
    for trade_date in days:
        day_bars = bars.get(trade_date) or []
        if len(day_bars) < 3:
            continue
        closes = [float(bar[1]) for bar in day_bars]
        times = [str(bar[0]) for bar in day_bars]
        open_ref = closes[0]
        entry_index = None
        scan_end = min(_T_ENTRY_SCAN_BARS, len(closes) - 1)
        for i in range(1, scan_end):
            if direction == "long" and closes[i] <= open_ref * (1 - thp):
                entry_index = i
                break
            if direction == "short" and closes[i] >= open_ref * (1 + thp):
                entry_index = i
                break
        if entry_index is None:
            continue
        tries += 1
        exit_index = len(closes) - 1
        completed = False
        for j in range(entry_index + 1, len(closes)):
            if direction == "long" and closes[j] >= closes[entry_index] * (1 + thp):
                exit_index, completed = j, True
                break
            if direction == "short" and closes[j] <= closes[entry_index] * (1 - thp):
                exit_index, completed = j, True
                break
        raw_bps = (closes[exit_index] / closes[entry_index] - 1) * 10000.0
        if direction == "short":
            raw_bps = -raw_bps
        bps_values.append(raw_bps)
        if raw_bps > 0:
            wins += 1
        if completed:
            completed_n += 1
        trades.append(
            {
                "date": trade_date,
                "direction": direction,
                "entry_time": times[entry_index],
                "entry_price": closes[entry_index],
                "exit_time": times[exit_index],
                "exit_price": closes[exit_index],
                "completed": completed,
                "raw_bps": round(raw_bps, 1),
            }
        )
    return {
        "tries": tries,
        "wins": wins,
        "win_rate": wins / tries if tries else None,
        "avg_bps": sum(bps_values) / tries if tries else None,
        "per_day": sum(bps_values) / len(days) if days else 0.0,
        "completed_rate": completed_n / tries if tries else None,
        "trades": trades,
    }


def t_profile(window: Mapping[str, Any]) -> Dict[str, Any]:
    """Per-symbol intraday T-suitability profile over the cached window.

    Suitability requires enough samples, a meaningful median amplitude and a
    positive net-of-cost expectancy from the uniform rule in at least one
    direction; the recommended direction is the higher-expectancy one.  Results
    are cached per (symbol, window fingerprint).
    """

    thscode = str(window.get("thscode") or "")
    fetched_at = str(window.get("fetched_at") or "")
    with _T_PROFILE_CACHE_LOCK:
        cached = _T_PROFILE_CACHE.get(thscode)
        if cached and cached[0] == fetched_at:
            return cached[1]

    bars = window.get("bars") or {}
    prev_close = window.get("prev_close") or {}
    days = [
        trade_date
        for trade_date in sorted(bars)
        if len(bars.get(trade_date) or []) >= 45 and trade_date in prev_close
    ]
    profile: Dict[str, Any] = {
        "eligible": False,
        "suitable": False,
        "reason": "",
        "days": len(days),
    }
    if len(days) < _T_PROFILE_MIN_DAYS:
        profile["reason"] = "样本不足"
        with _T_PROFILE_CACHE_LOCK:
            _T_PROFILE_CACHE[thscode] = (fetched_at, profile)
        return profile

    amplitudes = []
    rets_all: List[float] = []
    ac_pairs: List[tuple[float, float]] = []
    vr_one = vr_q = 0.0
    q_lag = 6
    double_sided = 0
    for trade_date in days:
        closes = [float(bar[1]) for bar in bars[trade_date]]
        prev = float(prev_close[trade_date])
        if prev <= 0:
            continue
        amplitudes.append((max(closes) - min(closes)) / prev)
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(len(closes) - 1)]
        rets_all.extend(rets)
        ac_pairs.extend((rets[i], rets[i + 1]) for i in range(len(rets) - 1))
        for i in range(0, len(rets) - q_lag, q_lag):
            chunk = rets[i:i + q_lag]
            vr_one += sum(r * r for r in chunk)
            vr_q += sum(chunk) ** 2
        vwap = sum(closes) / len(closes)
        if min(closes) <= vwap * 0.993 and max(closes) >= vwap * 1.007:
            double_sided += 1

    amp_med = sorted(amplitudes)[len(amplitudes) // 2] if amplitudes else 0.0
    n = len(ac_pairs)
    if n > 2 and amplitudes:
        mean_x = sum(a for a, _ in ac_pairs) / n
        mean_y = sum(b for _, b in ac_pairs) / n
        var_x = sum((a - mean_x) ** 2 for a, _ in ac_pairs) / n
        cov = sum((a - mean_x) * (b - mean_y) for a, b in ac_pairs) / n
        ac1 = cov / var_x if var_x > 0 else 0.0
        vr6 = (vr_q / vr_one) if vr_one > 0 else 1.0
    else:
        ac1, vr6 = 0.0, 1.0

    th_pct = min(
        _T_THRESHOLD_MAX_PCT,
        max(_T_THRESHOLD_MIN_PCT, _T_AMPLITUDE_RATIO * amp_med * 100.0),
    )
    is_etf = tencent_symbol(thscode).startswith(("sh51", "sz15"))
    cost_bps = 3.0 if is_etf else 13.0
    # ETF 免印花税、双边成本仅≈3bp，较低振幅即可覆盖成本。
    min_amp_pct = 0.6 if is_etf else _T_SUITABLE_MIN_AMP_PCT
    sim_long = _t_sim_days(bars, days, th_pct, "long")
    sim_short = _t_sim_days(bars, days, th_pct, "short")
    for sim in (sim_long, sim_short):
        sim.pop("trades", None)
        if sim["avg_bps"] is not None:
            sim["avg_net_bps"] = round(sim["avg_bps"] - cost_bps, 1)
            sim["per_day_net"] = round(sim["per_day"] - cost_bps * sim["tries"] / len(days), 1)
        else:
            sim["avg_net_bps"] = None
            sim["per_day_net"] = 0.0

    best_direction = "long" if sim_long["per_day_net"] >= sim_short["per_day_net"] else "short"
    best_per_day = max(sim_long["per_day_net"], sim_short["per_day_net"])
    suitable = (
        amp_med * 100.0 >= min_amp_pct
        and best_per_day >= _T_SUITABLE_MIN_PER_DAY_BPS
    )
    profile.update(
        {
            "eligible": True,
            "suitable": suitable,
            "reason": "" if suitable else "窗口内规则化做T期望为负或振幅不足",
            "amp_med_pct": round(amp_med * 100.0, 2),
            "amp_mean_pct": round(sum(amplitudes) / len(amplitudes) * 100.0, 2) if amplitudes else 0.0,
            "ac1": round(ac1, 3),
            "vr6": round(vr6, 2),
            "double_sided_pct": round(double_sided / len(days) * 100.0, 0),
            "th_pct": round(th_pct, 2),
            "cost_bps": cost_bps,
            "direction": best_direction,
            "best_per_day_net": round(best_per_day, 1),
            "sim_long": sim_long,
            "sim_short": sim_short,
            "window": window.get("window") or {},
        }
    )
    with _T_PROFILE_CACHE_LOCK:
        _T_PROFILE_CACHE[thscode] = (fetched_at, profile)
    return profile


def t_trades_for_date(
    window: Mapping[str, Any], trade_date: str, th_pct: float, direction: str
) -> List[Dict[str, Any]]:
    """The uniform rule's entry/exit points for one trade date (chart markers)."""

    bars = window.get("bars") or {}
    day_bars = bars.get(trade_date) or []
    if len(day_bars) < 3:
        return []
    single_day = {trade_date: day_bars}
    sim = _t_sim_days(single_day, [trade_date], th_pct, direction)
    return sim["trades"]


def apply_backfill_merge(
    recorded_series: Mapping[str, List[Dict[str, Any]]],
    backfill_series: Mapping[str, Sequence[Mapping[str, Any]]],
    check: Optional[Mapping[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Merge reconstructed points into recorded series with provenance.

    Recorded points win; a reconstructed point is inserted only when no
    recorded point exists within ±150 seconds.  Recorded points flagged by the
    check are replaced by the reconstructed value in the returned display copy
    (the ledger itself is never rewritten) and marked ``source="corrected"``.
    """

    corrections: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for correction in (check or {}).get("corrections") or []:
        # Corrections are matched per series by time only; the caller supplies
        # series keyed entries so corrections land on the right series.
        corrections.setdefault(str(correction.get("series") or "total"), {})[
            str(correction.get("time") or "")
        ] = correction

    merged: Dict[str, List[Dict[str, Any]]] = {}
    for key, recorded_points in recorded_series.items():
        points: List[Dict[str, Any]] = []
        for point in recorded_points:
            entry = dict(point)
            stamp = str(entry.get("time") or "")
            correction = corrections.get(key, {}).get(stamp)
            if correction is not None:
                entry["pnl_recorded"] = entry.get("pnl")
                entry["pnl"] = correction.get("corrected_pnl", entry.get("pnl"))
                entry["source"] = "corrected"
            else:
                entry.setdefault("source", "recorded")
            points.append(entry)
        series_backfill = backfill_series.get(key) or []
        recorded_stamps = [_epoch_seconds(str(point.get("time") or "")) for point in recorded_points]
        recorded_stamps = [value for value in recorded_stamps if value is not None]
        for point in series_backfill:
            moment = _epoch_seconds(str(point.get("time") or ""))
            if moment is None:
                continue
            if any(abs(value - moment) <= _MATCH_TOLERANCE_SECONDS for value in recorded_stamps):
                continue
            entry = dict(point)
            entry["source"] = "backfill"
            points.append(entry)
        points.sort(key=lambda item: _epoch_seconds(str(item.get("time") or "")) or 0.0)
        merged[key] = points
    return merged
