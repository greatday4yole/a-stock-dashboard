#!/usr/bin/env python3
"""Persist intraday portfolio P&L points for the local dashboard.

The component is deliberately independent from the HTTP server.  A caller
passes one immutable portfolio snapshot and one quote payload to
``IntradayPnlStore.record``.  Quantities always come from the portfolio
snapshot; quote-payload quantities are ignored.  Holdings in different
accounts are aggregated by ``thscode`` before calculating::

    daily_pnl = aggregate_quantity * (price - last_close)

Only the A-share continuous-auction windows in China time are accepted:
09:30:00 through 11:30:00 and 13:00:00 through 15:00:00, with both endpoints
inclusive.  Consequently, times strictly after 11:30:00 and strictly before
13:00:00 are lunch and are not recorded.  Weekends are rejected.  A supplied
``market_status`` can additionally reject exchange holidays or a stale feed.

Each China trading date has a canonical JSONL ledger under ``data``.  Appends
take an exclusive POSIX file lock, verify the complete SHA-256 chain when the
file is first seen or changes, write one bounded record with ``O_APPEND``, and
fsync before unlocking.  Readers take a shared lock and never consume a partial
or tampered ledger.  Verified records are cached against the open file's
identity and metadata so dashboard polling does not repeatedly decode a large,
unchanged ledger.  Identical points are de-duplicated, changed points are
rate-limited, and an unchanged heartbeat is retained periodically so a flat
price still has visible time extent.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


CN_TZ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "intraday-pnl"

POINT_SCHEMA = "intraday-pnl.point.v1"
TIMESERIES_SCHEMA = "intraday-pnl.timeseries.v1"
GENESIS_HASH = "0" * 64
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
DEFAULT_MIN_INTERVAL_SECONDS = 15.0
DEFAULT_UNCHANGED_HEARTBEAT_SECONDS = 300.0
DEFAULT_MAX_QUOTE_AGE_SECONDS = 120.0
MAX_QUOTE_FUTURE_SKEW_SECONDS = 5.0

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

_THSCODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_PUBLIC_SYMBOL_RE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.I)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_THREAD_LOCKS: Dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_CENT = Decimal("0.01")


class IntradayPnlError(RuntimeError):
    """Base error with a stable code for an HTTP integration."""

    code = "INTRADAY_PNL_ERROR"


class IntradayPnlInputError(IntradayPnlError):
    code = "INVALID_INPUT"


class IntradayPnlIntegrityError(IntradayPnlError):
    code = "LEDGER_INTEGRITY_ERROR"


class IntradayPnlPersistenceError(IntradayPnlError):
    code = "PERSISTENCE_ERROR"


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IntradayPnlInputError("value is not canonical-JSON serializable") from exc


def _sha256_document(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntradayPnlInputError("%s must be an object" % field)
    return value


def _as_sequence(value: Any, field: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise IntradayPnlInputError("%s must be an array" % field)
    return value


def _finite_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise IntradayPnlInputError("%s must be a finite number" % field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise IntradayPnlInputError("%s must be a finite number" % field) from exc
    if not result.is_finite():
        raise IntradayPnlInputError("%s must be a finite number" % field)
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    result = _finite_decimal(value, field)
    if result <= 0:
        raise IntradayPnlInputError("%s must be positive" % field)
    return result


def _quantity(value: Any, field: str) -> int:
    result = _finite_decimal(value, field)
    if result < 0 or result != result.to_integral_value():
        raise IntradayPnlInputError("%s must be a non-negative integer" % field)
    return int(result)


def _float_value(value: Decimal) -> float:
    return float(value)


def _money(value: Decimal) -> float:
    return float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _parse_datetime(value: Any, field: str, *, allow_naive_cn: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise IntradayPnlInputError("%s must be an ISO-8601 timestamp" % field)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = None
        for pattern in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, pattern)
                break
            except ValueError:
                pass
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise IntradayPnlInputError(
                    "%s must be an ISO-8601 timestamp" % field
                ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not allow_naive_cn:
            raise IntradayPnlInputError("%s must include a UTC offset" % field)
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _resolve_observed_at(
    quote_payload: Mapping[str, Any], observed_at: Optional[datetime]
) -> datetime:
    if observed_at is not None:
        result = _parse_datetime(observed_at, "observed_at")
    elif quote_payload.get("generated_at"):
        result = _parse_datetime(
            quote_payload.get("generated_at"), "quote_payload.generated_at", allow_naive_cn=True
        )
    else:
        result = datetime.now(CN_TZ)
    # Preserve sub-seconds until the session boundary is classified.  Otherwise
    # 11:30:00.500 would be rounded back into the accepted morning window.
    return result


def _parse_snapshot_date(value: Any) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        return _parse_datetime(raw, "account snapshot as_of", allow_naive_cn=True).date()
    except (ValueError, IntradayPnlInputError):
        return None


def classify_trading_session(value: datetime) -> Optional[str]:
    """Return ``morning``/``afternoon`` for an accepted China session instant.

    The exact instants 11:30:00 and 13:00:00 are accepted.  A timestamp such as
    11:30:00.001 is already in lunch and is rejected.  Exchange holidays need a
    quote payload's market-status gate because a weekday alone is insufficient.
    """

    observed = _parse_datetime(value, "session timestamp")
    if observed.weekday() >= 5:
        return None
    local_time = observed.time().replace(tzinfo=None)
    if MORNING_OPEN <= local_time <= MORNING_CLOSE:
        return "morning"
    if AFTERNOON_OPEN <= local_time <= AFTERNOON_CLOSE:
        return "afternoon"
    return None


def session_policy() -> Dict[str, Any]:
    return {
        "timezone": "Asia/Shanghai (UTC+08:00)",
        "weekdays_only": True,
        "exchange_holiday_guard": "quote_payload.market_status when supplied",
        "windows": [
            {"session": "morning", "start": "09:30:00", "end": "11:30:00", "end_inclusive": True},
            {"session": "afternoon", "start": "13:00:00", "end": "15:00:00", "end_inclusive": True},
        ],
        "lunch": {
            "excluded": "(11:30:00,13:00:00)",
            "morning_close_accepted": "11:30:00",
            "afternoon_open_accepted": "13:00:00",
        },
    }


def _normalize_thscode(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if _THSCODE_RE.fullmatch(raw):
        return raw
    public_match = _PUBLIC_SYMBOL_RE.fullmatch(raw)
    if public_match:
        exchange, code = public_match.groups()
        return "%s.%s" % (code, exchange.upper())
    # A bare code is ambiguous between Shanghai, Shenzhen, and Beijing and is
    # intentionally not guessed.
    return ""


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _first_present(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping.get(name) not in (None, ""):
            return mapping.get(name)
    return None


def _account_snapshot_state(accounts: Sequence[Mapping[str, Any]], trade_date: date) -> Dict[str, Any]:
    dates = sorted({str(item.get("as_of") or "") for item in accounts if item.get("as_of")})
    stale_ids = [str(item.get("account_id") or "") for item in accounts if not item.get("verified")]
    missing_ids = [str(item.get("account_id") or "") for item in accounts if not item.get("as_of")]
    verified = bool(accounts) and not stale_ids
    if verified:
        state = "CURRENT_DATE_SNAPSHOT"
    elif missing_ids:
        state = "UNVERIFIED_MISSING_AS_OF"
    else:
        state = "UNVERIFIED_STALE_SNAPSHOT"
    return {
        "as_of": dates[0] if len(dates) == 1 else "mixed" if dates else "",
        "dates": dates,
        "trade_date": trade_date.isoformat(),
        "verified": verified,
        "state": state,
        "stale_account_ids": stale_ids,
        "missing_as_of_account_ids": missing_ids,
        "broker_live": False,
        "values_are_estimates": True,
        "note": "数量来自账户快照而非券商实时持仓；过期快照仅供估算展示。",
    }


def _aggregate_holdings(
    portfolio: Mapping[str, Any], trade_date: date
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    accounts_raw = _as_sequence(portfolio.get("accounts"), "portfolio.accounts")
    portfolio_as_of = portfolio.get("as_of")
    aggregate: Dict[str, Dict[str, Any]] = {}
    account_meta: List[Dict[str, Any]] = []
    account_ids = set()

    for account_index, raw_account in enumerate(accounts_raw):
        account = _as_mapping(raw_account, "portfolio.accounts[%d]" % account_index)
        account_id = str(account.get("account_id") or "account-%d" % (account_index + 1))
        if account_id in account_ids:
            raise IntradayPnlInputError("portfolio account_id values must be unique")
        account_ids.add(account_id)
        as_of_raw = account.get("as_of") or portfolio_as_of
        as_of_date = _parse_snapshot_date(as_of_raw)
        account_verified = as_of_date == trade_date
        meta = {
            "account_id": account_id,
            "account_type": str(account.get("account_type") or ""),
            "as_of": as_of_date.isoformat() if as_of_date else "",
            "verified": account_verified,
        }
        account_meta.append(meta)
        positions = _as_sequence(
            account.get("positions") or [],
            "portfolio.accounts[%d].positions" % account_index,
        )
        for position_index, raw_position in enumerate(positions):
            position = _as_mapping(
                raw_position,
                "portfolio.accounts[%d].positions[%d]" % (account_index, position_index),
            )
            thscode = _normalize_thscode(position.get("thscode"))
            if not thscode:
                raise IntradayPnlInputError(
                    "portfolio position thscode must be a six-digit SH/SZ/BJ code"
                )
            quantity = _quantity(
                position.get("quantity"),
                "portfolio position %s quantity" % thscode,
            )
            if quantity == 0:
                continue
            item = aggregate.setdefault(
                thscode,
                {
                    "thscode": thscode,
                    "name": str(position.get("name") or thscode),
                    "quantity": 0,
                    "accounts": [],
                },
            )
            item["quantity"] += quantity
            item["accounts"].append(
                {
                    **meta,
                    "quantity": quantity,
                }
            )

    if not aggregate:
        raise IntradayPnlInputError("portfolio contains no positive-quantity positions")
    holdings = sorted(aggregate.values(), key=lambda item: item["thscode"])
    snapshot = _account_snapshot_state(account_meta, trade_date)
    series_binding = {
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "trade_date": trade_date.isoformat(),
        "holdings": [
            {
                "thscode": item["thscode"],
                "name": item["name"],
                "quantity": item["quantity"],
                "accounts": item["accounts"],
            }
            for item in holdings
        ],
    }
    return holdings, snapshot, _sha256_document(series_binding)


def _quote_rows(quote_payload: Mapping[str, Any]) -> Dict[str, List[Mapping[str, Any]]]:
    raw_rows = _as_sequence(quote_payload.get("positions") or [], "quote_payload.positions")
    result: Dict[str, List[Mapping[str, Any]]] = {}
    for index, raw_row in enumerate(raw_rows):
        row = _as_mapping(raw_row, "quote_payload.positions[%d]" % index)
        thscode = _normalize_thscode(
            _first_present(row, ("thscode", "symbol", "ticker", "security_code"))
        )
        if thscode:
            result.setdefault(thscode, []).append(row)
    return result


def _parse_quote_candidate(
    row: Mapping[str, Any], observed: datetime, max_quote_age_seconds: float
) -> Tuple[Optional[Dict[str, Any]], str]:
    if _bool_value(_first_present(row, ("is_stale", "isStale", "stale"))):
        return None, "QUOTE_MARKED_STALE"
    quote_status = str(_first_present(row, ("quote_status", "status")) or "").lower()
    source = str(_first_present(row, ("data_source", "source")) or "")
    if quote_status in {"snapshot", "missing"} or "持仓快照" in source:
        return None, "SNAPSHOT_PRICE_NOT_LIVE_QUOTE"
    try:
        price = _positive_decimal(
            _first_present(row, ("price", "last_price", "lastPrice", "current", "close")),
            "quote price",
        )
        last_close = _positive_decimal(
            _first_present(
                row,
                (
                    "last_close",
                    "prev_close",
                    "prev_price",
                    "prevClose",
                    "previous_close",
                    "pre_close",
                ),
            ),
            "quote last_close",
        )
    except IntradayPnlInputError:
        return None, "QUOTE_PRICE_OR_LAST_CLOSE_INVALID"

    quote_time_raw = _first_present(
        row,
        ("quote_time", "quoteTime", "trade_time", "timestamp", "updated_at", "datetime"),
    )
    try:
        quote_at = _parse_datetime(quote_time_raw, "quote timestamp", allow_naive_cn=True)
    except IntradayPnlInputError:
        return None, "QUOTE_TIMESTAMP_INVALID"
    quote_at = quote_at.replace(microsecond=0)
    if quote_at.date() != observed.date():
        return None, "QUOTE_DATE_MISMATCH"
    age = (observed - quote_at).total_seconds()
    if age < -MAX_QUOTE_FUTURE_SKEW_SECONDS:
        return None, "QUOTE_TIMESTAMP_IN_FUTURE"
    if age > max_quote_age_seconds:
        return None, "QUOTE_TOO_OLD"
    return (
        {
            "price_decimal": price,
            "last_close_decimal": last_close,
            "quote_at_datetime": quote_at,
            "price": _float_value(price),
            "last_close": _float_value(last_close),
            "quote_at": quote_at.isoformat(timespec="seconds"),
            "source": source,
        },
        "",
    )


def _select_quote(
    rows: Sequence[Mapping[str, Any]], observed: datetime, max_quote_age_seconds: float
) -> Tuple[Optional[Dict[str, Any]], str]:
    if not rows:
        return None, "QUOTE_MISSING"
    valid: List[Dict[str, Any]] = []
    errors: List[str] = []
    for row in rows:
        candidate, error = _parse_quote_candidate(row, observed, max_quote_age_seconds)
        if candidate is not None:
            valid.append(candidate)
        elif error:
            errors.append(error)
    if not valid:
        return None, errors[0] if errors else "QUOTE_INVALID"
    latest_time = max(item["quote_at_datetime"] for item in valid)
    latest = [item for item in valid if item["quote_at_datetime"] == latest_time]
    price_pairs = {
        (item["price_decimal"], item["last_close_decimal"])
        for item in latest
    }
    if len(price_pairs) != 1:
        return None, "CONFLICTING_QUOTES_AT_SAME_TIMESTAMP"
    selected = dict(latest[0])
    selected.pop("price_decimal", None)
    selected.pop("last_close_decimal", None)
    selected.pop("quote_at_datetime", None)
    return selected, ""


def _market_gate_reason(quote_payload: Mapping[str, Any]) -> str:
    raw_status = quote_payload.get("market_status")
    if not isinstance(raw_status, Mapping):
        return ""
    status = raw_status
    state = str(status.get("state") or status.get("phase") or "").strip().lower()
    if state and state not in {"open", "trading", "continuous_auction"}:
        return "market_not_open"
    if _bool_value(status.get("feed_stale")):
        return "market_feed_stale"
    return ""


def build_intraday_pnl_point(
    portfolio: Mapping[str, Any],
    quote_payload: Mapping[str, Any],
    *,
    observed_at: Optional[datetime] = None,
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> Dict[str, Any]:
    """Build one self-contained, aggregate P&L point without persisting it."""

    portfolio = _as_mapping(portfolio, "portfolio")
    quote_payload = _as_mapping(quote_payload, "quote_payload")
    try:
        max_age = float(max_quote_age_seconds)
    except (TypeError, ValueError) as exc:
        raise IntradayPnlInputError("max_quote_age_seconds must be positive") from exc
    if not math.isfinite(max_age) or max_age <= 0:
        raise IntradayPnlInputError("max_quote_age_seconds must be positive")

    observed = _resolve_observed_at(quote_payload, observed_at)
    session = classify_trading_session(observed)
    if session is None:
        raise IntradayPnlInputError("observed_at is outside an accepted trading session")
    # Once boundary semantics have been enforced, whole-second storage keeps
    # de-duplication deterministic across refresh workers.
    observed = observed.replace(microsecond=0)

    holdings, account_snapshot, series_id = _aggregate_holdings(portfolio, observed.date())
    by_symbol = _quote_rows(quote_payload)
    positions: List[Dict[str, Any]] = []
    missing_symbols: List[str] = []
    covered_total = Decimal("0")

    for holding in holdings:
        thscode = holding["thscode"]
        quote, quote_error = _select_quote(by_symbol.get(thscode, []), observed, max_age)
        account_verified = all(bool(item.get("verified")) for item in holding["accounts"])
        base = {
            "thscode": thscode,
            "name": holding["name"],
            "quantity": holding["quantity"],
            "accounts": copy.deepcopy(holding["accounts"]),
            "account_snapshot_verified": account_verified,
            "values_are_estimates": True,
        }
        if quote is None:
            missing_symbols.append(thscode)
            positions.append(
                {
                    **base,
                    "price": None,
                    "last_close": None,
                    "daily_pnl_cny": None,
                    "quote_at": "",
                    "quote_source": "",
                    "quote_verified": False,
                    "verified": False,
                    "verification_reasons": [quote_error],
                }
            )
            continue
        price = Decimal(str(quote["price"]))
        last_close = Decimal(str(quote["last_close"]))
        daily_pnl = Decimal(holding["quantity"]) * (price - last_close)
        covered_total += daily_pnl
        reasons = [] if account_verified else ["ACCOUNT_SNAPSHOT_NOT_CURRENT_TRADE_DATE"]
        positions.append(
            {
                **base,
                "price": quote["price"],
                "last_close": quote["last_close"],
                "daily_pnl_cny": _money(daily_pnl),
                "quote_at": quote["quote_at"],
                "quote_source": quote["source"],
                "quote_verified": True,
                "verified": account_verified,
                "verification_reasons": reasons,
            }
        )

    covered_count = len(positions) - len(missing_symbols)
    complete = covered_count == len(positions)
    total_reasons: List[str] = []
    if not complete:
        total_reasons.append("INCOMPLETE_QUOTE_COVERAGE")
    if not account_snapshot["verified"]:
        total_reasons.append("ACCOUNT_SNAPSHOT_NOT_CURRENT_TRADE_DATE")
    total_verified = not total_reasons
    total_value = _money(covered_total) if complete else None
    coverage = {
        "required_count": len(positions),
        "covered_count": covered_count,
        "complete": complete,
        "missing_symbols": missing_symbols,
    }
    total = {
        "daily_pnl_cny": total_value,
        "covered_daily_pnl_cny": _money(covered_total),
        "verified": total_verified,
        "verification_reasons": total_reasons,
        "values_are_estimates": True,
    }
    fingerprint_binding = {
        "series_id": series_id,
        "coverage": coverage,
        "positions": [
            {
                "thscode": item["thscode"],
                "quantity": item["quantity"],
                "price": item["price"],
                "last_close": item["last_close"],
                "daily_pnl_cny": item["daily_pnl_cny"],
                "quote_verified": item["quote_verified"],
                "account_snapshot_verified": item["account_snapshot_verified"],
            }
            for item in positions
        ],
    }
    content_fingerprint = _sha256_document(fingerprint_binding)
    point = {
        "schema_version": POINT_SCHEMA,
        "trade_date": observed.date().isoformat(),
        "observed_at": observed.isoformat(timespec="seconds"),
        "session": session,
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "series_id": series_id,
        "content_fingerprint": content_fingerprint,
        "account_snapshot": account_snapshot,
        "coverage": coverage,
        "total": total,
        "positions": positions,
        "values_are_estimates": True,
    }
    # Catch unexpected Decimal/NaN leakage before persistence.
    _canonical_json(point)
    return point


def _path_thread_lock(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


def _record_hash(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_hash", None)
    return _sha256_document(unsigned)


def _read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_LEDGER_BYTES:
            raise IntradayPnlIntegrityError("daily intraday P&L ledger is unexpectedly large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_ledger(raw: bytes, expected_date: date) -> List[Dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise IntradayPnlIntegrityError("daily intraday P&L ledger has a truncated final line")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntradayPnlIntegrityError("daily intraday P&L ledger is not UTF-8") from exc
    records: List[Dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    previous_at: Optional[datetime] = None
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise IntradayPnlIntegrityError("ledger contains an empty line at %d" % line_number)
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntradayPnlIntegrityError("invalid JSON at ledger line %d" % line_number) from exc
        if not isinstance(value, dict):
            raise IntradayPnlIntegrityError("ledger line %d must be an object" % line_number)
        try:
            canonical = _canonical_json(value)
        except IntradayPnlInputError as exc:
            raise IntradayPnlIntegrityError("non-canonical value at ledger line %d" % line_number) from exc
        if canonical != line:
            raise IntradayPnlIntegrityError("ledger line %d is not canonical JSON" % line_number)
        if value.get("schema_version") != POINT_SCHEMA:
            raise IntradayPnlIntegrityError("unsupported schema at ledger line %d" % line_number)
        if value.get("trade_date") != expected_date.isoformat():
            raise IntradayPnlIntegrityError("trade-date mismatch at ledger line %d" % line_number)
        if value.get("sequence") != line_number:
            raise IntradayPnlIntegrityError("sequence mismatch at ledger line %d" % line_number)
        if value.get("previous_record_hash") != previous_hash:
            raise IntradayPnlIntegrityError("hash-chain mismatch at ledger line %d" % line_number)
        actual_hash = str(value.get("record_hash") or "")
        if not _HASH_RE.fullmatch(actual_hash) or actual_hash != _record_hash(value):
            raise IntradayPnlIntegrityError("record hash mismatch at ledger line %d" % line_number)
        fingerprint = str(value.get("content_fingerprint") or "")
        series_id = str(value.get("series_id") or "")
        if not _HASH_RE.fullmatch(fingerprint) or not _HASH_RE.fullmatch(series_id):
            raise IntradayPnlIntegrityError("invalid binding hash at ledger line %d" % line_number)
        try:
            observed = _parse_datetime(value.get("observed_at"), "ledger observed_at")
        except IntradayPnlInputError as exc:
            raise IntradayPnlIntegrityError("invalid observed_at at ledger line %d" % line_number) from exc
        if observed.date() != expected_date or classify_trading_session(observed) != value.get("session"):
            raise IntradayPnlIntegrityError("invalid session binding at ledger line %d" % line_number)
        if previous_at is not None and observed <= previous_at:
            raise IntradayPnlIntegrityError("ledger timestamps are not strictly increasing")
        previous_at = observed
        previous_hash = actual_hash
        records.append(value)
    return records


class IntradayPnlStore:
    """Concurrency-safe daily JSONL store and chart-oriented read API."""

    def __init__(
        self,
        data_dir: Path = DEFAULT_DATA_DIR,
        *,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        unchanged_heartbeat_seconds: float = DEFAULT_UNCHANGED_HEARTBEAT_SECONDS,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    ) -> None:
        self.data_dir = Path(data_dir)
        for field, value in (
            ("min_interval_seconds", min_interval_seconds),
            ("unchanged_heartbeat_seconds", unchanged_heartbeat_seconds),
            ("max_quote_age_seconds", max_quote_age_seconds),
        ):
            try:
                parsed = float(value)
            except (TypeError, ValueError) as exc:
                raise IntradayPnlInputError("%s must be positive" % field) from exc
            if not math.isfinite(parsed) or parsed <= 0:
                raise IntradayPnlInputError("%s must be positive" % field)
            setattr(self, field, parsed)
        if self.unchanged_heartbeat_seconds < self.min_interval_seconds:
            raise IntradayPnlInputError(
                "unchanged_heartbeat_seconds must be at least min_interval_seconds"
            )
        self._verified_records_cache: Dict[
            str, Tuple[Tuple[int, int, int, int, int], Tuple[Dict[str, Any], ...]]
        ] = {}
        self._verified_records_cache_guard = threading.RLock()

    @staticmethod
    def _fd_fingerprint(fd: int) -> Tuple[int, int, int, int, int]:
        stat_result = os.fstat(fd)
        return (
            int(stat_result.st_dev),
            int(stat_result.st_ino),
            int(stat_result.st_size),
            int(stat_result.st_mtime_ns),
            int(stat_result.st_ctime_ns),
        )

    @staticmethod
    def _cache_key(path: Path) -> str:
        return os.path.abspath(os.fspath(path))

    def _forget_verified_records(self, ledger_path: Path) -> None:
        with self._verified_records_cache_guard:
            self._verified_records_cache.pop(self._cache_key(ledger_path), None)

    def _verified_records_from_fd(
        self,
        fd: int,
        ledger_path: Path,
        trade_date: date,
    ) -> Tuple[Dict[str, Any], ...]:
        """Return verified immutable-by-convention records for a locked file.

        File identity, size, mtime and ctime are read from the already-open file
        descriptor.  Any append, replacement or in-place edit invalidates the
        cache before the ledger is decoded and its hash chain is checked again.
        """

        cache_key = self._cache_key(ledger_path)
        fingerprint = self._fd_fingerprint(fd)
        with self._verified_records_cache_guard:
            cached = self._verified_records_cache.get(cache_key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
            self._verified_records_cache.pop(cache_key, None)

        records = tuple(_decode_ledger(_read_fd(fd), trade_date))
        with self._verified_records_cache_guard:
            self._verified_records_cache[cache_key] = (fingerprint, records)
        return records

    def _remember_verified_records(
        self,
        fd: int,
        ledger_path: Path,
        records: Sequence[Dict[str, Any]],
    ) -> None:
        cache_key = self._cache_key(ledger_path)
        fingerprint = self._fd_fingerprint(fd)
        with self._verified_records_cache_guard:
            self._verified_records_cache[cache_key] = (fingerprint, tuple(records))

    def path_for(self, trade_date: date) -> Path:
        if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
            raise IntradayPnlInputError("trade_date must be a date")
        return self.data_dir / ("intraday-pnl-%s.jsonl" % trade_date.isoformat())

    def _skip_result(
        self, reason: str, observed: datetime, *, point: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "recorded": False,
            "status": "skipped",
            "reason": reason,
            "trade_date": observed.date().isoformat(),
            "observed_at": observed.isoformat(timespec="seconds"),
            "point": copy.deepcopy(point) if point is not None else None,
        }

    def record(
        self,
        portfolio: Mapping[str, Any],
        quote_payload: Mapping[str, Any],
        *,
        observed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Build and conditionally append one intraday point.

        Calling this method outside continuous trading hours is safe: it returns
        a structured skipped result and does not create a ledger file.
        """

        quote_payload = _as_mapping(quote_payload, "quote_payload")
        observed = _resolve_observed_at(quote_payload, observed_at)
        if classify_trading_session(observed) is None:
            return self._skip_result("outside_trading_session", observed)
        market_reason = _market_gate_reason(quote_payload)
        if market_reason:
            return self._skip_result(market_reason, observed)
        point = build_intraday_pnl_point(
            portfolio,
            quote_payload,
            observed_at=observed,
            max_quote_age_seconds=self.max_quote_age_seconds,
        )
        if int(point["coverage"]["covered_count"]) == 0:
            return self._skip_result("no_usable_quotes", observed, point=point)

        ledger_path = self.path_for(observed.date())
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise IntradayPnlPersistenceError("cannot create intraday P&L data directory") from exc

        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock = _path_thread_lock(ledger_path)
        with lock:
            try:
                fd = os.open(str(ledger_path), flags, 0o600)
            except OSError as exc:
                raise IntradayPnlPersistenceError("cannot open intraday P&L ledger") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                if os.fstat(fd).st_mode & 0o777 != 0o600:
                    os.fchmod(fd, 0o600)
                records = self._verified_records_from_fd(
                    fd,
                    ledger_path,
                    observed.date(),
                )
                if records:
                    latest = records[-1]
                    latest_at = _parse_datetime(latest["observed_at"], "latest observed_at")
                    delta = (observed - latest_at).total_seconds()
                    same_content = latest.get("content_fingerprint") == point["content_fingerprint"]
                    if delta < 0:
                        return self._skip_result("out_of_order", observed, point=point)
                    if delta == 0:
                        return self._skip_result(
                            "duplicate" if same_content else "timestamp_collision",
                            observed,
                            point=point,
                        )
                    if delta < self.min_interval_seconds:
                        return self._skip_result("rate_limited", observed, point=point)
                    if same_content and delta < self.unchanged_heartbeat_seconds:
                        return self._skip_result("duplicate", observed, point=point)
                    previous_hash = str(latest["record_hash"])
                else:
                    previous_hash = GENESIS_HASH

                record = {
                    **point,
                    "sequence": len(records) + 1,
                    "previous_record_hash": previous_hash,
                }
                record["record_hash"] = _record_hash(record)
                encoded = (_canonical_json(record) + "\n").encode("utf-8")
                if len(encoded) > MAX_RECORD_BYTES:
                    raise IntradayPnlPersistenceError("intraday P&L record exceeds size limit")
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise IntradayPnlPersistenceError("short append to intraday P&L ledger")
                os.fsync(fd)
                self._remember_verified_records(
                    fd,
                    ledger_path,
                    (*records, record),
                )
                return {
                    "ok": True,
                    "recorded": True,
                    "status": "recorded",
                    "reason": "",
                    "trade_date": observed.date().isoformat(),
                    "observed_at": point["observed_at"],
                    "path": str(ledger_path),
                    "point": copy.deepcopy(record),
                }
            except IntradayPnlError:
                raise
            except OSError as exc:
                raise IntradayPnlPersistenceError("cannot append intraday P&L ledger") from exc
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def records(self, trade_date: date) -> List[Dict[str, Any]]:
        ledger_path = self.path_for(trade_date)
        lock = _path_thread_lock(ledger_path)
        with lock:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(str(ledger_path), flags)
            except FileNotFoundError:
                self._forget_verified_records(ledger_path)
                return []
            except OSError as exc:
                raise IntradayPnlPersistenceError("cannot open intraday P&L ledger") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_SH)
                records = self._verified_records_from_fd(fd, ledger_path, trade_date)
                return copy.deepcopy(list(records))
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def verify(self, trade_date: date) -> Dict[str, Any]:
        records = self.records(trade_date)
        return {
            "ok": True,
            "trade_date": trade_date.isoformat(),
            "path": str(self.path_for(trade_date)),
            "record_count": len(records),
            "head_hash": records[-1]["record_hash"] if records else GENESIS_HASH,
        }

    def read_timeseries(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """Return a chart-oriented total series and one series per security."""

        target = trade_date or datetime.now(CN_TZ).date()
        records = self.records(target)
        total_points: List[Dict[str, Any]] = []
        symbol_series: Dict[str, Dict[str, Any]] = {}
        for record in records:
            total = record["total"]
            total_points.append(
                {
                    "observed_at": record["observed_at"],
                    "session": record["session"],
                    "daily_pnl_cny": total.get("daily_pnl_cny"),
                    "covered_daily_pnl_cny": total.get("covered_daily_pnl_cny"),
                    "verified": bool(total.get("verified")),
                    "verification_reasons": copy.deepcopy(total.get("verification_reasons") or []),
                    "series_id": record["series_id"],
                }
            )
            for position in record["positions"]:
                thscode = position["thscode"]
                series = symbol_series.setdefault(
                    thscode,
                    {
                        "thscode": thscode,
                        "name": position["name"],
                        "points": [],
                    },
                )
                series["name"] = position["name"]
                series["points"].append(
                    {
                        "observed_at": record["observed_at"],
                        "session": record["session"],
                        "daily_pnl_cny": position.get("daily_pnl_cny"),
                        "price": position.get("price"),
                        "last_close": position.get("last_close"),
                        "quantity": position.get("quantity"),
                        "quote_at": position.get("quote_at"),
                        "quote_verified": bool(position.get("quote_verified")),
                        "account_snapshot_verified": bool(
                            position.get("account_snapshot_verified")
                        ),
                        "verified": bool(position.get("verified")),
                        "verification_reasons": copy.deepcopy(
                            position.get("verification_reasons") or []
                        ),
                        "series_id": record["series_id"],
                    }
                )
        latest = records[-1] if records else None
        return {
            "schema_version": TIMESERIES_SCHEMA,
            "ok": True,
            "available": bool(records),
            "trade_date": target.isoformat(),
            "session_policy": session_policy(),
            "ledger": {
                "path": str(self.path_for(target)),
                "record_count": len(records),
                "head_hash": latest["record_hash"] if latest else GENESIS_HASH,
                "integrity_verified": True,
            },
            "account_snapshot": copy.deepcopy(latest.get("account_snapshot")) if latest else {},
            "latest_coverage": copy.deepcopy(latest.get("coverage")) if latest else {},
            "total": {"points": total_points},
            "positions": [symbol_series[key] for key in sorted(symbol_series)],
            "latest_point": copy.deepcopy(latest) if latest else None,
        }

    # A short alias reads naturally in an HTTP endpoint implementation.
    def read_day(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        return self.read_timeseries(trade_date)
