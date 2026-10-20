#!/usr/bin/env python3
"""Auditable, fail-closed per-position advice state for the local dashboard.

This module is intentionally independent from the HTTP server.  It consumes an
already captured decision snapshot; it does not fetch data, connect to a broker,
or place orders.  A position is updated only when evidence unseen by its prior
advice record is present.  Unverified account or decision-critical inputs always
collapse to a non-sized, reduce-only review message.

The public integration entry point is :func:`update_position_advice_store`.
The pure :func:`evaluate_position_advice` function is useful for tests and for
callers that own persistence themselves.
"""

from __future__ import annotations

import contextlib
import copy
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import zlib
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "position-advice-state.json"
DEFAULT_AUDIT_PATH = ROOT / "data" / "position-advice-audit.jsonl"
CN_TZ = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1
PAYLOAD_TYPE = "portfolio-position-advice.v1"
AUDIT_PAYLOAD_TYPE = "portfolio-position-advice-audit.v1"
MAX_SEEN_EVIDENCE_IDS = 512
MAX_TRIGGER_SUMMARIES = 5
MAX_SUMMARY_CHARS = 180
DEFAULT_POSITION_NEWS_MAX_AGE_SECONDS = 3_600.0
DEFAULT_MARKET_NEWS_MAX_AGE_SECONDS = 300.0
DEFAULT_AUDIT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_AUDIT_KEEP_RECENT_TRANSITIONS = 24
AUDIT_CHECKPOINT_PAYLOAD_TYPE = "portfolio-position-advice-audit-checkpoint.v1"
AUDIT_CHANGED_POSITIONS_CODEC = "zlib-json-v1"
MAX_AUDIT_CHANGED_POSITIONS_BYTES = 16 * 1024 * 1024


class AdviceIntegrityError(RuntimeError):
    """Raised when persisted state or its append-only audit chain is invalid."""


ADVICE_TEXTS = {
    "DATA_UNVERIFIED": "暂停新增；复核关键数据，若需动作仅考虑减险方向。",
    "REDUCE_RISK_REVIEW": "暂停新增；复核风险证据后，仅考虑减险方向。",
    "HOLD_NO_CHASE": "维持观察；短线波动放大，避免追涨，复核后再决定。",
    "HOLD_OBSERVE": "维持观察；等待下一次已核验证据后再复核。",
}

FAIL_CLOSED_CODES = frozenset({"DATA_UNVERIFIED", "REDUCE_RISK_REVIEW"})
NEGATIVE_NEWS_TERMS = (
    "立案",
    "调查",
    "处罚",
    "问询",
    "警示",
    "诉讼",
    "仲裁",
    "减持",
    "亏损",
    "下修",
    "终止",
    "退市",
    "停牌",
    "违约",
    "风险提示",
    "业绩下降",
)
POSITIVE_NEWS_TERMS = (
    "中标",
    "增持",
    "回购",
    "预增",
    "扭亏",
    "创新高",
    "战略合作",
    "订单",
)
HIGH_RISK_FLAGS = frozenset(
    {
        "POSITION_OR_TOP5_CAP",
        "THEME_CONCENTRATION",
        "VOLATILITY_LIMIT",
        "OUTSIZED_RISK_CONTRIBUTION",
        "LIQUIDITY_HORIZON",
        "HIGH_EVENT_RISK",
        "TRADING_BLOCKED_EVENT",
        "VOLATILITY_UNKNOWN",
        "RISK_CONTRIBUTION_UNKNOWN",
        "LIQUIDITY_UNKNOWN",
        "EVENT_DATA_UNKNOWN",
    }
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(authorization\s*:?\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret)"
        r"\s*[:=]\s*[^\s,;&]+"
    ),
    re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|secret)=)[^&#\s]+"),
)

_FORBIDDEN_SIZING_KEYS = frozenset(
    {
        "amount",
        "amount_cny",
        "notional",
        "notional_cny",
        "quantity",
        "shares",
        "share_count",
        "weight",
        "weight_pct",
        "target",
        "target_weight",
        "target_amount",
        "target_quantity",
        "order",
        "orders",
        "lot_count",
        "trade_size",
    }
)

_GATE_REASON_LABELS = {
    "PORTFOLIO_ID_MISSING": "组合标识缺失",
    "PORTFOLIO_AS_OF_INVALID": "账户日期无法核验",
    "ACCOUNT_SNAPSHOT_UNVERIFIED": "账户持仓、现金或负债未核验为当前状态",
    "QUOTE_PAYLOAD_UNVERIFIED": "持仓行情未通过完整性与交叉验证",
    "QUOTE_COVERAGE_INCOMPLETE": "持仓行情覆盖不完整",
    "QUOTE_ROW_UNVERIFIED": "个股行情缺失、延迟或价格无效",
    "RISK_SNAPSHOT_UNVERIFIED": "风险快照未通过数据质量门禁",
    "RISK_POSITION_COVERAGE_INCOMPLETE": "风险快照未覆盖全部持仓",
    "POSITION_NEWS_UNVERIFIED": "个股新闻源或时间戳未核验",
    "MARKET_NEWS_UNVERIFIED": "市场快讯源或时间戳未核验",
    "MARKET_STATE_UNVERIFIED": "大盘状态证据未通过门禁",
    "REBALANCE_GATE_UNVERIFIED": "再平衡、流动性或事件门禁未通过",
    "INPUT_ERROR": "统一决策输入存在错误",
}


def _now() -> datetime:
    return datetime.now(CN_TZ)


def parse_datetime(value: Any) -> datetime | None:
    """Parse common collector timestamps and normalize them to China time."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        if number > 10_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if re.fullmatch(r"\d{14}", raw):
            try:
                parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
            except ValueError:
                return None
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
                for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(raw, pattern)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _iso(value: datetime) -> str:
    return value.astimezone(CN_TZ).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _weekday_lag(older: date, newer: date) -> int | None:
    """Return a conservative trading-session lag without external calendars."""

    if older > newer:
        return None
    cursor = older
    lag = 0
    while cursor < newer:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            lag += 1
    return lag


def normalize_thscode(value: Any) -> str:
    raw = str(value or "").strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = re.fullmatch(r"(\d{6})[.\-]?(SH|SZ|BJ)", raw)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    if re.fullmatch(r"\d{6}", raw):
        exchange = "SH" if raw.startswith(("5", "6", "9")) else "BJ" if raw.startswith(("4", "8")) else "SZ"
        return f"{raw}.{exchange}"
    return raw


def redact_text(value: Any, limit: int = 500) -> str:
    """Remove credential-shaped strings before they can enter state or logs."""

    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)([?&]"):
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[: max(0, int(limit))]


def contains_sensitive_text(value: Any) -> bool:
    """Return true if a nested payload still contains credential-like text."""

    if isinstance(value, Mapping):
        return any(contains_sensitive_text(key) or contains_sensitive_text(item) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_sensitive_text(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    return False


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {redact_text(key, 120): _sanitize(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


def _canonical_hash(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def portfolio_fingerprint_sha256(portfolio: Mapping[str, Any]) -> str:
    """Bind advice to the complete account snapshot, not only its symbols.

    The display-only ``label`` key never affects positions, cash or risk, so
    renaming a portfolio must not invalidate advice envelopes or P&L caches.
    """

    if not isinstance(portfolio, Mapping):
        raise TypeError("portfolio must be an object")
    return _canonical_hash(_without_key(copy.deepcopy(dict(portfolio)), "label"))


def _without_key(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {name: copy.deepcopy(item) for name, item in value.items() if name != key}


def _record_hash(record: Mapping[str, Any]) -> str:
    return _canonical_hash(_without_key(record, "advice_hash_sha256"))


def _state_core(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": state.get("schema_version"),
        "payload_type": state.get("payload_type"),
        "portfolio_id": state.get("portfolio_id"),
        "portfolio_as_of": state.get("portfolio_as_of"),
        "portfolio_fingerprint_sha256": state.get("portfolio_fingerprint_sha256"),
        "generated_at": state.get("generated_at"),
        "positions": copy.deepcopy(state.get("positions") or []),
    }


def _state_from_core(
    core: Mapping[str, Any], *, audit_sequence: int, audit_tail_sha256: str | None
) -> dict[str, Any]:
    sanitized = _sanitize(copy.deepcopy(dict(core)))
    state = {
        **sanitized,
        "content_sha256": _canonical_hash(sanitized),
        "audit": {
            "sequence": int(audit_sequence),
            "tail_sha256": audit_tail_sha256,
        },
    }
    _assert_safe_output(state)
    return state


def _position_records(portfolio: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    for account in portfolio.get("accounts") or []:
        if not isinstance(account, Mapping):
            continue
        for item in account.get("positions") or []:
            if not isinstance(item, Mapping):
                continue
            symbol = normalize_thscode(item.get("thscode") or item.get("symbol") or item.get("code"))
            if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol):
                continue
            records.setdefault(
                symbol,
                {
                    "thscode": symbol,
                    "name": redact_text(item.get("name") or symbol, 80),
                },
            )
    if not records:
        raise ValueError("portfolio contains no valid positions")
    return records


def _rows(payload: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    data = payload.get("data")
    if isinstance(data, Mapping):
        return _rows(data, keys)
    return []


def _quote_rows(quotes: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _rows(quotes, ("positions", "quotes", "items")):
        symbol = normalize_thscode(row.get("thscode") or row.get("symbol") or row.get("code"))
        if symbol:
            result[symbol] = row
    if not result and isinstance(quotes, Mapping):
        for key, value in quotes.items():
            if not isinstance(value, Mapping):
                continue
            symbol = normalize_thscode(value.get("thscode") or value.get("symbol") or key)
            if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol):
                result[symbol] = dict(value)
    return result


def _symbol_rows(payload: Any, keys: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(payload, keys):
        symbol = normalize_thscode(row.get("thscode") or row.get("symbol") or row.get("code"))
        if symbol:
            result.setdefault(symbol, []).append(row)
    return result


def _payload_time(payload: Any) -> datetime | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("generated_at", "last_success_at", "observed_at", "quote_time"):
        parsed = parse_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _item_time(item: Mapping[str, Any]) -> datetime | None:
    for key in (
        "published_at",
        "event_time",
        "publish_time",
        "time",
        "datetime",
        "timestamp",
        "quote_time",
        "observed_at",
    ):
        parsed = parse_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _quote_max_age(quotes: Mapping[str, Any]) -> float:
    status = quotes.get("market_status") if isinstance(quotes.get("market_status"), Mapping) else {}
    phase = str(status.get("state") or "unknown").lower()
    if phase in {"open", "auction"}:
        return 300.0
    if phase == "lunch":
        return 7_200.0
    if phase == "closed":
        return 259_200.0
    return 900.0


def _source_status_healthy(payload: Mapping[str, Any]) -> bool:
    statuses = payload.get("source_status")
    if not isinstance(statuses, Mapping) or not statuses:
        return False
    checked = 0
    for value in statuses.values():
        if not isinstance(value, Mapping):
            return False
        checked += 1
        if value.get("ok") is not True:
            return False
    return checked > 0


def _matches_position_news(item: Mapping[str, Any], symbol: str, name: str) -> bool:
    direct_values = [
        item.get("thscode"),
        item.get("symbol"),
        item.get("code"),
        item.get("stock_code"),
    ]
    if symbol in {normalize_thscode(value) for value in direct_values if value is not None}:
        return True
    raw_symbols = item.get("symbols") or item.get("affected_symbols")
    if isinstance(raw_symbols, Sequence) and not isinstance(raw_symbols, (str, bytes, bytearray)):
        if symbol in {normalize_thscode(value) for value in raw_symbols}:
            return True
    item_name = str(item.get("stock_name") or item.get("name") or "").strip()
    if item_name and item_name == name:
        return True
    title = str(item.get("title") or "")
    return bool(name and len(name) >= 2 and name in title)


def _news_time_validity(
    payload: Any,
    holdings: Mapping[str, Mapping[str, str]],
    *,
    market_wide: bool,
    now: datetime,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    generated = _payload_time(payload)
    if generated is None or generated > now + timedelta(seconds=120):
        return False
    raw_last_success = payload.get("last_success_at")
    last_success = parse_datetime(raw_last_success)
    if raw_last_success not in (None, "") and last_success is None:
        return False
    last_success = last_success or generated
    default_max_age = (
        DEFAULT_MARKET_NEWS_MAX_AGE_SECONDS
        if market_wide
        else DEFAULT_POSITION_NEWS_MAX_AGE_SECONDS
    )
    declared_max_age = _number(payload.get("ttl_seconds"))
    if declared_max_age is None:
        cache = payload.get("cache") if isinstance(payload.get("cache"), Mapping) else {}
        declared_max_age = _number(cache.get("ttl_seconds"))
    maximum_age = declared_max_age if declared_max_age and declared_max_age > 0 else default_max_age
    success_age = (now - last_success).total_seconds()
    cache_age = _number(payload.get("cache_age_seconds"))
    if (
        success_age < -120
        or success_age > maximum_age
        or (cache_age is not None and (cache_age < -120 or cache_age > maximum_age))
    ):
        return False
    for item in _rows(payload, ("items", "news", "events")):
        relevant = market_wide or any(
            _matches_position_news(item, symbol, row["name"])
            for symbol, row in holdings.items()
        )
        if relevant:
            timestamp = _item_time(item)
            if timestamp is None or timestamp > now + timedelta(seconds=120):
                return False
    return True


def _verification_gate(
    portfolio: Mapping[str, Any],
    holdings: Mapping[str, Mapping[str, str]],
    *,
    quotes: Any,
    position_news: Any,
    market_news: Any,
    market_state: Any,
    risk: Any,
    rebalance: Any,
    input_errors: Sequence[str],
    now: datetime,
) -> dict[str, Any]:
    reasons: set[str] = set()
    portfolio_id = str(portfolio.get("portfolio_id") or "").strip()
    if not portfolio_id:
        reasons.add("PORTFOLIO_ID_MISSING")
    account_date: date | None = None
    try:
        account_date = date.fromisoformat(str(portfolio.get("as_of") or "")[:10])
    except ValueError:
        reasons.add("PORTFOLIO_AS_OF_INVALID")
    if account_date is not None:
        account_lag = _weekday_lag(account_date, now.date())
        if account_lag is None:
            reasons.add("PORTFOLIO_AS_OF_INVALID")
        elif account_lag > 0:
            reasons.add("ACCOUNT_SNAPSHOT_UNVERIFIED")

    if not isinstance(quotes, Mapping):
        reasons.add("QUOTE_PAYLOAD_UNVERIFIED")
        quote_by_symbol: dict[str, dict[str, Any]] = {}
    else:
        quote_by_symbol = _quote_rows(quotes)
        validation = quotes.get("quote_validation") if isinstance(quotes.get("quote_validation"), Mapping) else {}
        cache = quotes.get("cache") if isinstance(quotes.get("cache"), Mapping) else {}
        status = quotes.get("market_status") if isinstance(quotes.get("market_status"), Mapping) else {}
        if (
            quotes.get("ok") is not True
            or validation.get("decision_verified") is not True
            or cache.get("expired") is True
            or status.get("feed_stale") is True
        ):
            reasons.add("QUOTE_PAYLOAD_UNVERIFIED")
    expected = set(holdings)
    if set(quote_by_symbol) & expected != expected:
        reasons.add("QUOTE_COVERAGE_INCOMPLETE")
    maximum_quote_age = _quote_max_age(quotes if isinstance(quotes, Mapping) else {})
    for symbol in expected:
        row = quote_by_symbol.get(symbol) or {}
        timestamp = _item_time(row)
        age = (now - timestamp).total_seconds() if timestamp else None
        official = row.get("official_validation") if isinstance(row.get("official_validation"), Mapping) else {}
        official_state = str(official.get("state") or "").lower()
        official_required = official.get("required")
        official_failed = bool(
            not official
            or official_state not in {"matched", "official_fallback", "excluded_non_stock"}
            or official.get("passed") is False
            or (official_required is True and official.get("passed") is not True)
            or (official_required is False and official_state != "excluded_non_stock")
        )
        if (
            (_number(row.get("price")) or 0) <= 0
            or row.get("is_stale") is True
            or timestamp is None
            or age is None
            or age < -120
            or age > maximum_quote_age
            or official_failed
        ):
            reasons.add("QUOTE_ROW_UNVERIFIED")

    risk_rows = _symbol_rows(risk, ("positions",))
    if not isinstance(risk, Mapping):
        reasons.add("RISK_SNAPSHOT_UNVERIFIED")
    else:
        quality = risk.get("data_quality") if isinstance(risk.get("data_quality"), Mapping) else {}
        account = quality.get("account_snapshot") if isinstance(quality.get("account_snapshot"), Mapping) else {}
        cache = risk.get("cache") if isinstance(risk.get("cache"), Mapping) else {}
        if (
            risk.get("ok") is not True
            or str(risk.get("state") or "").upper() == "DATA_UNVERIFIED"
            or quality.get("status") != "VERIFIED"
            or bool(quality.get("critical_failures"))
            or cache.get("stale") is True
            or cache.get("fresh") is False
        ):
            reasons.add("RISK_SNAPSHOT_UNVERIFIED")
        if (
            account.get("current_verified") is not True
            or account.get("verified") is not True
            or account.get("stale") is not False
        ):
            reasons.add("ACCOUNT_SNAPSHOT_UNVERIFIED")
        risk_account_date: date | None = None
        try:
            risk_account_date = date.fromisoformat(str(account.get("as_of") or "")[:10])
        except ValueError:
            pass
        if (
            risk_account_date is None
            or account_date is None
            or risk_account_date != account_date
            or (_weekday_lag(risk_account_date, now.date()) or 0) > 0
        ):
            reasons.add("ACCOUNT_SNAPSHOT_UNVERIFIED")
    if set(risk_rows) & expected != expected:
        reasons.add("RISK_POSITION_COVERAGE_INCOMPLETE")

    if (
        not isinstance(position_news, Mapping)
        or position_news.get("ok") is not True
        or position_news.get("stale") is True
        or str(position_news.get("portfolio_id") or "") != portfolio_id
        or not _source_status_healthy(position_news)
        or not _news_time_validity(position_news, holdings, market_wide=False, now=now)
    ):
        reasons.add("POSITION_NEWS_UNVERIFIED")

    if (
        not isinstance(market_news, Mapping)
        or market_news.get("ok") is not True
        or market_news.get("stale") is True
        or market_news.get("degraded") is True
        or str(market_news.get("status") or "").lower() != "healthy"
        or not _source_status_healthy(market_news)
        or not _news_time_validity(market_news, holdings, market_wide=True, now=now)
    ):
        reasons.add("MARKET_NEWS_UNVERIFIED")

    if not isinstance(market_state, Mapping):
        reasons.add("MARKET_STATE_UNVERIFIED")
    else:
        quality = market_state.get("quality") if isinstance(market_state.get("quality"), Mapping) else {}
        if (
            market_state.get("ok") is not True
            or str(market_state.get("regime") or "").upper() == "UNVERIFIED"
            or bool(quality.get("critical_failures"))
            or _payload_time(market_state) is None
        ):
            reasons.add("MARKET_STATE_UNVERIFIED")

    if not isinstance(rebalance, Mapping):
        reasons.add("REBALANCE_GATE_UNVERIFIED")
    else:
        gate = rebalance.get("gate") if isinstance(rebalance.get("gate"), Mapping) else {}
        liquidity = rebalance.get("liquidity_gate") if isinstance(rebalance.get("liquidity_gate"), Mapping) else {}
        events = rebalance.get("event_gate") if isinstance(rebalance.get("event_gate"), Mapping) else {}
        binding = rebalance.get("input_binding") if isinstance(rebalance.get("input_binding"), Mapping) else {}
        if (
            str(rebalance.get("status") or "").upper() in {"DATA_UNVERIFIED", "ERROR", "UNAVAILABLE"}
            or gate.get("verified") is not True
            or liquidity.get("verified") is not True
            or events.get("verified") is not True
            or (binding.get("portfolio_id") is not None and str(binding.get("portfolio_id")) != portfolio_id)
        ):
            reasons.add("REBALANCE_GATE_UNVERIFIED")

    if input_errors:
        reasons.add("INPUT_ERROR")

    sorted_reasons = sorted(reasons)
    data_verified = not sorted_reasons
    risk_new_buy = isinstance(risk, Mapping) and risk.get("new_buy_allowed") is True
    buy_gate = rebalance.get("buy_gate") if isinstance(rebalance, Mapping) and isinstance(rebalance.get("buy_gate"), Mapping) else {}
    buy_gate_open = not buy_gate or buy_gate.get("verified") is True
    regime = str(market_state.get("regime") or "UNVERIFIED").upper() if isinstance(market_state, Mapping) else "UNVERIFIED"
    increase_allowed = bool(data_verified and risk_new_buy and buy_gate_open and regime != "RISK_OFF")
    mode = "DATA_UNVERIFIED" if not data_verified else "VERIFIED" if increase_allowed else "REDUCE_ONLY"
    basis = {
        "mode": mode,
        "data_verified": data_verified,
        "increase_allowed": increase_allowed,
        "reason_codes": sorted_reasons,
        "portfolio_as_of": str(portfolio.get("as_of") or ""),
        "quote_validation": (
            (quotes.get("quote_validation") or {}).get("decision_verified")
            if isinstance(quotes, Mapping)
            else False
        ),
        "risk_state": str(risk.get("state") or "") if isinstance(risk, Mapping) else "",
        "market_regime": regime,
        "rebalance_status": str(rebalance.get("status") or "") if isinstance(rebalance, Mapping) else "",
    }
    return {
        "mode": mode,
        "data_verified": data_verified,
        "increase_allowed": increase_allowed,
        "reason_codes": sorted_reasons,
        "fingerprint_sha256": _canonical_hash(basis),
    }


def _news_impact(item: Mapping[str, Any]) -> int:
    title = f"{item.get('title') or ''} {item.get('summary') or ''}"
    risk_tags = item.get("risk_tags") or []
    opportunity_tags = item.get("opportunity_tags") or []
    if risk_tags or any(term in title for term in NEGATIVE_NEWS_TERMS):
        return -2 if str(item.get("importance") or "").lower() in {"high", "critical"} else -1
    if opportunity_tags or any(term in title for term in POSITIVE_NEWS_TERMS):
        return 1
    return 0


def _evidence_id(kind: str, symbol: str, seed: Any) -> str:
    return _canonical_hash({"kind": kind, "symbol": symbol, "seed": seed})


def _news_evidence(
    payload: Any,
    *,
    symbol: str,
    name: str,
    kind: str,
    cutoff: datetime | None,
    previous_seen: set[str],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(payload, Mapping):
        return [], []
    collected = _payload_time(payload)
    market_wide = kind == "market_news"
    evidence: list[dict[str, Any]] = []
    current_ids: list[str] = []
    for item in _rows(payload, ("items", "news", "events")):
        if not market_wide and not _matches_position_news(item, symbol, name):
            continue
        occurred = _item_time(item)
        if occurred is None or occurred > now:
            continue
        title = redact_text(item.get("title") or item.get("summary") or "未命名资讯", MAX_SUMMARY_CHARS)
        raw_identity = item.get("id") or item.get("news_id") or {
            "title": title,
            "source": redact_text(item.get("source") or "", 80),
            "occurred_at": _iso(occurred),
        }
        identifier = _evidence_id(kind, symbol, raw_identity)
        current_ids.append(identifier)
        acquired_after = bool(collected and cutoff and collected > cutoff)
        published_after = bool(cutoff and occurred > cutoff)
        is_new = cutoff is None or (identifier not in previous_seen and (acquired_after or published_after))
        if not is_new:
            continue
        label = "市场快讯" if market_wide else "个股新闻"
        evidence.append(
            {
                "id": identifier,
                "kind": kind,
                "occurred_at": _iso(occurred),
                "summary": redact_text(f"{label}：{title}", MAX_SUMMARY_CHARS),
                "impact": _news_impact(item),
            }
        )
    # Let the caller merge the current identities from every news source before
    # filling the bounded cursor with historical IDs.  Returning a pre-filled
    # list here let the first source consume the entire allowance with old IDs,
    # which evicted current market-news identities and replayed them as "new" on
    # every collector refresh.
    return evidence, list(dict.fromkeys(current_ids))


def _quote_evidence(
    row: Mapping[str, Any],
    *,
    symbol: str,
    cutoff: datetime | None,
    previous_fingerprint: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    timestamp = _item_time(row)
    fingerprint_body = {
        key: row.get(key)
        for key in ("price", "prev_close", "change", "change_pct", "open", "high", "low", "is_stale")
    }
    fingerprint_body["official_state"] = (
        (row.get("official_validation") or {}).get("state")
        if isinstance(row.get("official_validation"), Mapping)
        else None
    )
    fingerprint = _canonical_hash(fingerprint_body)
    is_new = bool(
        timestamp
        and timestamp <= now
        and fingerprint != previous_fingerprint
        and (cutoff is None or timestamp > cutoff)
    )
    if not is_new:
        return [], fingerprint
    change = _number(row.get("change_pct") if row.get("change_pct") is not None else row.get("pct_change"))
    high = _number(row.get("high"))
    low = _number(row.get("low"))
    previous_close = _number(row.get("prev_close"))
    amplitude = ((high - low) / previous_close * 100) if high is not None and low is not None and previous_close and previous_close > 0 else None
    parts = ["个股行情更新"]
    if change is not None:
        parts.append(f"日内涨跌{change:+.2f}%")
    if amplitude is not None:
        parts.append(f"振幅{amplitude:.2f}%")
    impact = -2 if change is not None and change <= -5 else -1 if change is not None and change <= -2 else 1 if change is not None and change >= 5 else 0
    return [
        {
            "id": _evidence_id("position_quote", symbol, fingerprint_body),
            "kind": "position_quote",
            "occurred_at": _iso(timestamp),
            "summary": "；".join(parts),
            "impact": impact,
        }
    ], fingerprint


def _component_evidence(
    kind: str,
    symbol: str,
    body: Mapping[str, Any],
    *,
    occurred_at: datetime | None,
    cutoff: datetime | None,
    previous_fingerprint: str,
    summary: str,
    impact: int = 0,
    allow_change_without_time: bool = False,
    now: datetime,
) -> tuple[list[dict[str, Any]], str]:
    fingerprint = _canonical_hash(body)
    changed = fingerprint != previous_fingerprint
    after_cutoff = bool(occurred_at and occurred_at <= now and (cutoff is None or occurred_at > cutoff))
    if not changed or (not after_cutoff and not allow_change_without_time):
        return [], fingerprint
    effective_time = occurred_at if occurred_at and occurred_at <= now else now
    return [
        {
            "id": _evidence_id(kind, symbol, body),
            "kind": kind,
            "occurred_at": _iso(effective_time),
            "summary": redact_text(summary, MAX_SUMMARY_CHARS),
            "impact": int(max(-3, min(3, impact))),
        }
    ], fingerprint


def _risk_rows_for_symbol(risk: Any, symbol: str) -> list[dict[str, Any]]:
    return _symbol_rows(risk, ("positions",)).get(symbol, [])


def _priority_for_symbol(rebalance: Any, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(rebalance, Mapping):
        return []
    return [
        dict(row)
        for row in rebalance.get("priority_draft") or []
        if isinstance(row, Mapping)
        and normalize_thscode(row.get("thscode") or row.get("symbol") or row.get("code")) == symbol
    ]


def _adjustment_direction(rebalance: Any, symbol: str) -> str:
    if not isinstance(rebalance, Mapping):
        return ""
    candidates: list[Mapping[str, Any]] = []
    for key in ("adjustments", "actions"):
        value = rebalance.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            candidates.extend(item for item in value if isinstance(item, Mapping))
    for row in candidates:
        if normalize_thscode(row.get("thscode") or row.get("symbol") or row.get("code")) != symbol:
            continue
        raw = " ".join(
            str(row.get(key) or "")
            for key in ("direction", "action", "side", "action_class", "recommendation")
        ).lower()
        if any(term in raw for term in ("sell", "reduce", "decrease", "卖", "减")):
            return "reduce"
        if any(term in raw for term in ("buy", "increase", "add", "买", "增")):
            return "increase"
    return ""


def _risk_flags(risk: Any, rebalance: Any, symbol: str) -> set[str]:
    flags: set[str] = set()
    for row in _risk_rows_for_symbol(risk, symbol):
        for key in ("risk_flags", "flags"):
            value = row.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                flags.update(str(item) for item in value)
    for row in _priority_for_symbol(rebalance, symbol):
        value = row.get("risk_flags")
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            flags.update(str(item) for item in value)
    return flags


def _choose_advice(
    gate: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    quote: Mapping[str, Any],
    market_state: Any,
    risk: Any,
    rebalance: Any,
) -> tuple[str, list[str]]:
    if gate.get("data_verified") is not True:
        labels = [
            _GATE_REASON_LABELS.get(code, "关键数据未核验")
            for code in gate.get("reason_codes") or []
        ]
        return "DATA_UNVERIFIED", list(dict.fromkeys(labels))[:3]

    flags = _risk_flags(risk, rebalance, symbol)
    direction = _adjustment_direction(rebalance, symbol)
    regime = str(market_state.get("regime") or "") if isinstance(market_state, Mapping) else ""
    change = _number(quote.get("change_pct") if quote.get("change_pct") is not None else quote.get("pct_change"))
    evidence_score = sum(int(_number(item.get("impact")) or 0) for item in evidence)
    rationales: list[str] = []

    if flags & HIGH_RISK_FLAGS:
        rationales.append("集中度、波动、流动性或事件风险触发人工复核")
    if direction == "reduce":
        rationales.append("已核验再平衡草案指向降低风险敞口")
    if regime in {"RISK_OFF", "CAUTIOUS"}:
        rationales.append("大盘风险偏好处于收缩或谨慎状态")
    if change is not None and change <= -5:
        rationales.append("个股日内跌幅达到高波动复核阈值")
    if evidence_score <= -2:
        rationales.append("新增证据的风险信号占优")

    if rationales:
        return "REDUCE_RISK_REVIEW", rationales[:3]
    if change is not None and change >= 5:
        return "HOLD_NO_CHASE", ["个股短线涨幅较大，需防止追涨决策"]
    if evidence_score > 0:
        return "HOLD_OBSERVE", ["新增正向证据尚不足以单独形成加仓依据"]
    return "HOLD_OBSERVE", ["新增证据未触发减险阈值，继续等待核验"]


def _source_fingerprints(
    previous: Mapping[str, Any] | None,
    *,
    symbol: str,
    quote: Mapping[str, Any],
    market_state: Any,
    risk: Any,
    rebalance: Any,
    gate: Mapping[str, Any],
    quotes: Any,
    cutoff: datetime | None,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    old = previous.get("source_fingerprints") if isinstance(previous, Mapping) and isinstance(previous.get("source_fingerprints"), Mapping) else {}
    evidence: list[dict[str, Any]] = []
    result: dict[str, str] = {}

    quote_evidence, result["position_quote"] = _quote_evidence(
        quote,
        symbol=symbol,
        cutoff=cutoff,
        previous_fingerprint=str(old.get("position_quote") or ""),
        now=now,
    )
    evidence.extend(quote_evidence)

    market_body = {
        key: market_state.get(key)
        for key in ("snapshot_id", "regime", "score", "confidence", "reason_codes")
    } if isinstance(market_state, Mapping) else {"missing": True}
    market_items, result["market_state"] = _component_evidence(
        "market_state",
        symbol,
        market_body,
        occurred_at=_payload_time(market_state),
        cutoff=cutoff,
        previous_fingerprint=str(old.get("market_state") or ""),
        summary=f"大盘状态更新：{market_body.get('regime') or '待核验'}",
        impact=-2 if market_body.get("regime") == "RISK_OFF" else -1 if market_body.get("regime") == "CAUTIOUS" else 1 if market_body.get("regime") == "RISK_ON" else 0,
        now=now,
    )
    evidence.extend(market_items)

    market_status = quotes.get("market_status") if isinstance(quotes, Mapping) and isinstance(quotes.get("market_status"), Mapping) else {}
    status_body = {
        "state": market_status.get("state"),
        "feed_stale": market_status.get("feed_stale"),
    }
    status_items, result["market_status"] = _component_evidence(
        "market_status",
        symbol,
        status_body,
        occurred_at=parse_datetime(market_status.get("quote_time")),
        cutoff=cutoff,
        previous_fingerprint=str(old.get("market_status") or ""),
        summary=f"交易时段状态更新：{status_body.get('state') or 'unknown'}",
        allow_change_without_time=True,
        now=now,
    )
    evidence.extend(status_items)

    risk_rows = _risk_rows_for_symbol(risk, symbol)
    priority = _priority_for_symbol(rebalance, symbol)
    risk_body = {
        "risk_state": risk.get("risk_state") if isinstance(risk, Mapping) else None,
        "position": [
            {
                key: row.get(key)
                for key in ("annualized_volatility_pct", "risk_contribution_pct", "quote_status", "history_status", "risk_flags")
            }
            for row in risk_rows
        ],
        "priority": [
            {"risk_flags": row.get("risk_flags"), "rank": row.get("rank")}
            for row in priority
        ],
        "direction": _adjustment_direction(rebalance, symbol),
    }
    risk_time = max(
        (_payload_time(value) for value in (risk, rebalance) if _payload_time(value) is not None),
        default=None,
    )
    risk_items, result["risk_rebalance"] = _component_evidence(
        "risk_rebalance",
        symbol,
        risk_body,
        occurred_at=risk_time,
        cutoff=cutoff,
        previous_fingerprint=str(old.get("risk_rebalance") or ""),
        summary="个股风险与再平衡证据已更新",
        impact=-2 if (_risk_flags(risk, rebalance, symbol) & HIGH_RISK_FLAGS or _adjustment_direction(rebalance, symbol) == "reduce") else 0,
        allow_change_without_time=True,
        now=now,
    )
    evidence.extend(risk_items)

    gate_body = {
        "mode": gate.get("mode"),
        "data_verified": gate.get("data_verified"),
        "increase_allowed": gate.get("increase_allowed"),
        "reason_codes": gate.get("reason_codes"),
        "fingerprint_sha256": gate.get("fingerprint_sha256"),
    }
    gate_items, result["decision_gate"] = _component_evidence(
        "decision_gate",
        symbol,
        gate_body,
        occurred_at=now,
        cutoff=cutoff,
        previous_fingerprint=str(old.get("decision_gate") or ""),
        summary=(
            "关键数据门禁未通过，已切换为暂停新增"
            if not gate.get("data_verified")
            else "关键数据门禁状态已更新"
        ),
        impact=-3 if not gate.get("data_verified") else -1 if gate.get("mode") == "REDUCE_ONLY" else 0,
        now=now,
    )
    evidence.extend(gate_items)
    return evidence, result


def _new_position_record(
    *,
    identity: Mapping[str, str],
    previous: Mapping[str, Any] | None,
    quotes: Any,
    position_news: Any,
    market_news: Any,
    market_state: Any,
    risk: Any,
    rebalance: Any,
    gate: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    symbol = identity["thscode"]
    prior_time = parse_datetime(previous.get("advice_time")) if isinstance(previous, Mapping) else None
    if previous is not None and prior_time is None:
        raise AdviceIntegrityError(f"invalid prior advice_time for {symbol}")
    old_seen = set(previous.get("seen_evidence_ids") or []) if isinstance(previous, Mapping) else set()
    quote = _quote_rows(quotes).get(symbol) or {}

    component_evidence, fingerprints = _source_fingerprints(
        previous,
        symbol=symbol,
        quote=quote,
        market_state=market_state,
        risk=risk,
        rebalance=rebalance,
        gate=gate,
        quotes=quotes,
        cutoff=prior_time,
        now=now,
    )
    position_items, current_position_ids = _news_evidence(
        position_news,
        symbol=symbol,
        name=identity["name"],
        kind="position_news",
        cutoff=prior_time,
        previous_seen=old_seen,
        now=now,
    )
    market_items, current_market_ids = _news_evidence(
        market_news,
        symbol=symbol,
        name=identity["name"],
        kind="market_news",
        cutoff=prior_time,
        previous_seen=old_seen,
        now=now,
    )
    evidence = [*component_evidence, *position_items, *market_items]
    evidence.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("id") or "")))
    bootstrap = previous is None
    identity_changed = bool(previous and (previous.get("name") != identity["name"] or previous.get("thscode") != symbol))
    if identity_changed:
        evidence.append(
            {
                "id": _evidence_id("portfolio_identity", symbol, identity),
                "kind": "portfolio_identity",
                "occurred_at": _iso(now),
                "summary": "持仓标识信息已更新",
                "impact": 0,
            }
        )
    if not evidence and not bootstrap:
        return None
    if bootstrap and not evidence:
        evidence.append(
            {
                "id": _evidence_id("bootstrap", symbol, gate.get("fingerprint_sha256")),
                "kind": "bootstrap",
                "occurred_at": _iso(now),
                "summary": "建立首个建议基线",
                "impact": 0,
            }
        )

    advice_code, rationale = _choose_advice(
        gate,
        evidence,
        symbol=symbol,
        quote=quote,
        market_state=market_state,
        risk=risk,
        rebalance=rebalance,
    )
    trigger_summaries = list(
        dict.fromkeys(redact_text(item.get("summary") or "", MAX_SUMMARY_CHARS) for item in evidence)
    )[:MAX_TRIGGER_SUMMARIES]
    evidence_ids = sorted({str(item["id"]) for item in evidence})
    occurred = [parse_datetime(item.get("occurred_at")) for item in evidence]
    through = max((value for value in occurred if value is not None), default=now)
    seen = list(
        dict.fromkeys(
            [
                # Identities present in this capture take priority over history
                # across both feeds.  The remaining allowance preserves older
                # cursors so temporarily missing articles are still deduped.
                *current_position_ids,
                *current_market_ids,
                *sorted(old_seen),
            ]
        )
    )[:MAX_SEEN_EVIDENCE_IDS]
    record: dict[str, Any] = {
        "thscode": symbol,
        "name": redact_text(identity["name"], 80),
        "advice_code": advice_code,
        "current_advice": ADVICE_TEXTS[advice_code],
        "advice_time": _iso(now),
        "evidence_since": _iso(prior_time) if prior_time else None,
        "evidence_through": _iso(through),
        "evidence_count": len(evidence_ids),
        "evidence_ids": evidence_ids,
        "evidence_digest_sha256": _canonical_hash(evidence),
        "trigger_evidence_summary": trigger_summaries,
        "rationale": [redact_text(item, MAX_SUMMARY_CHARS) for item in rationale],
        "decision_mode": gate.get("mode"),
        "data_verified": gate.get("data_verified") is True,
        "increase_allowed": gate.get("increase_allowed") is True,
        "gate_reason_codes": list(gate.get("reason_codes") or []),
        "source_fingerprints": fingerprints,
        "seen_evidence_ids": seen,
    }
    record = _sanitize(record)
    record["advice_hash_sha256"] = _record_hash(record)
    _validate_position_record(record)
    return record


def _assert_safe_output(value: Any) -> None:
    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(key)
                if str(key).lower() in _FORBIDDEN_SIZING_KEYS:
                    raise AdviceIntegrityError(f"sizing field is forbidden in advice output: {key}")
                visit(child)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for child in item:
                visit(child)
        elif isinstance(item, str) and any(
            pattern.search(item) for pattern in _SECRET_PATTERNS
        ):
            raise AdviceIntegrityError("credential-shaped text remains in advice output")

    visit(value)


def _validate_position_record(
    record: Mapping[str, Any], *, check_safe_output: bool = True
) -> None:
    symbol = normalize_thscode(record.get("thscode"))
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", symbol):
        raise AdviceIntegrityError("invalid advice symbol")
    code = str(record.get("advice_code") or "")
    if code not in ADVICE_TEXTS or record.get("current_advice") != ADVICE_TEXTS[code]:
        raise AdviceIntegrityError("unrecognized or non-canonical advice text")
    if code == "DATA_UNVERIFIED" and record.get("data_verified") is not False:
        raise AdviceIntegrityError("DATA_UNVERIFIED record claims verified data")
    if code in FAIL_CLOSED_CODES and any(
        term in str(record.get("current_advice") or "") for term in ("买入", "加仓", "股", "元", "%")
    ):
        raise AdviceIntegrityError("fail-closed advice contains a sized or risk-increasing instruction")
    if parse_datetime(record.get("advice_time")) is None:
        raise AdviceIntegrityError("advice_time is invalid")
    expected = _record_hash(record)
    if record.get("advice_hash_sha256") != expected:
        raise AdviceIntegrityError("position advice hash mismatch")
    if check_safe_output:
        _assert_safe_output(record)


def validate_advice_state(state: Mapping[str, Any]) -> None:
    """Validate schema, content hashes, canonical messages, and output safety."""

    if state.get("schema_version") != SCHEMA_VERSION or state.get("payload_type") != PAYLOAD_TYPE:
        raise AdviceIntegrityError("advice state schema mismatch")
    if not str(state.get("portfolio_id") or ""):
        raise AdviceIntegrityError("advice state portfolio_id missing")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(state.get("portfolio_fingerprint_sha256") or "")
    ):
        raise AdviceIntegrityError("advice state portfolio fingerprint missing or invalid")
    positions = state.get("positions")
    if not isinstance(positions, list) or not positions:
        raise AdviceIntegrityError("advice state positions missing")
    symbols: list[str] = []
    for record in positions:
        if not isinstance(record, Mapping):
            raise AdviceIntegrityError("advice position row must be an object")
        _validate_position_record(record)
        symbols.append(str(record.get("thscode")))
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise AdviceIntegrityError("advice positions must be unique and sorted")
    core = _state_core(state)
    if state.get("content_sha256") != _canonical_hash(core):
        raise AdviceIntegrityError("advice state content hash mismatch")
    audit = state.get("audit")
    if not isinstance(audit, Mapping) or not isinstance(audit.get("sequence"), int):
        raise AdviceIntegrityError("advice audit tail missing")
    _assert_safe_output(state)


def evaluate_position_advice(
    portfolio: Mapping[str, Any],
    *,
    quotes: Any,
    position_news: Any,
    market_news: Any,
    market_state: Any,
    risk: Any,
    rebalance: Any,
    previous_state: Mapping[str, Any] | None = None,
    input_errors: Sequence[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Purely evaluate unseen evidence and return a proposed immutable state core.

    ``state_core`` is present only when ``changed`` is true.  The function never
    mutates ``previous_state``.  A repeated call with identical inputs therefore
    returns ``changed=false`` and does not advance any advice timestamp.
    """

    if not isinstance(portfolio, Mapping):
        raise TypeError("portfolio must be an object")
    observed_at = (now or _now()).astimezone(CN_TZ)
    holdings = _position_records(portfolio)
    portfolio_fingerprint = portfolio_fingerprint_sha256(portfolio)
    previous_by_symbol: dict[str, Mapping[str, Any]] = {}
    prior_symbols: set[str] = set()
    portfolio_replaced = False
    if previous_state is not None:
        validate_advice_state(previous_state)
        loaded_previous = {
            str(row["thscode"]): row
            for row in previous_state.get("positions") or []
            if isinstance(row, Mapping)
        }
        prior_symbols = set(loaded_previous)
        portfolio_replaced = bool(
            str(previous_state.get("portfolio_id"))
            != str(portfolio.get("portfolio_id") or "")
            or str(previous_state.get("portfolio_fingerprint_sha256") or "")
            != portfolio_fingerprint
        )
        if not portfolio_replaced:
            previous_by_symbol = loaded_previous

    gate = _verification_gate(
        portfolio,
        holdings,
        quotes=quotes,
        position_news=position_news,
        market_news=market_news,
        market_state=market_state,
        risk=risk,
        rebalance=rebalance,
        input_errors=input_errors,
        now=observed_at,
    )
    next_by_symbol: dict[str, dict[str, Any]] = {
        symbol: copy.deepcopy(dict(row)) for symbol, row in previous_by_symbol.items() if symbol in holdings
    }
    changed_symbols: list[str] = []
    for symbol, identity in sorted(holdings.items()):
        updated = _new_position_record(
            identity=identity,
            previous=previous_by_symbol.get(symbol),
            quotes=quotes,
            position_news=position_news,
            market_news=market_news,
            market_state=market_state,
            risk=risk,
            rebalance=rebalance,
            gate=gate,
            now=observed_at,
        )
        if updated is not None:
            next_by_symbol[symbol] = updated
            changed_symbols.append(symbol)
    removed_symbols = sorted(
        prior_symbols - set(holdings)
        if portfolio_replaced
        else set(previous_by_symbol) - set(holdings)
    )
    portfolio_metadata_changed = bool(
        previous_state
        and str(previous_state.get("portfolio_as_of") or "") != str(portfolio.get("as_of") or "")
    )
    changed = bool(
        changed_symbols
        or removed_symbols
        or portfolio_metadata_changed
        or portfolio_replaced
        or previous_state is None
    )
    result: dict[str, Any] = {
        "changed": changed,
        "evaluated_at": _iso(observed_at),
        "changed_symbols": sorted(changed_symbols),
        "removed_symbols": removed_symbols,
        "gate": copy.deepcopy(gate),
    }
    if not changed:
        return result
    core = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": PAYLOAD_TYPE,
        "portfolio_id": redact_text(portfolio.get("portfolio_id") or "", 120),
        "portfolio_as_of": redact_text(portfolio.get("as_of") or "", 32),
        "portfolio_fingerprint_sha256": portfolio_fingerprint,
        "generated_at": _iso(observed_at),
        "positions": [next_by_symbol[symbol] for symbol in sorted(next_by_symbol)],
    }
    _assert_safe_output(core)
    result["state_core"] = core
    return result


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _append_audit(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        encoded = line.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _audit_hash(record: Mapping[str, Any]) -> str:
    return _canonical_hash(_without_key(record, "record_hash_sha256"))


def _compress_changed_positions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_AUDIT_CHANGED_POSITIONS_BYTES:
        raise AdviceIntegrityError("audit changed positions exceed compression limit")
    return {
        "codec": AUDIT_CHANGED_POSITIONS_CODEC,
        "uncompressed_bytes": len(raw),
        "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
        "data_base64": base64.b64encode(zlib.compress(raw, level=9)).decode("ascii"),
    }


def _audit_changed_positions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    compressed = record.get("changed_positions_compressed")
    if compressed is None:
        rows = record.get("changed_positions") or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            raise AdviceIntegrityError("audit changed positions must be a list")
        if not all(isinstance(row, Mapping) for row in rows):
            raise AdviceIntegrityError("audit changed position invalid")
        return [dict(row) for row in rows]
    if "changed_positions" in record:
        raise AdviceIntegrityError("audit changed positions encoding is ambiguous")
    if not isinstance(compressed, Mapping) or set(compressed) != {
        "codec",
        "uncompressed_bytes",
        "uncompressed_sha256",
        "data_base64",
    }:
        raise AdviceIntegrityError("audit compressed positions fields mismatch")
    if compressed.get("codec") != AUDIT_CHANGED_POSITIONS_CODEC:
        raise AdviceIntegrityError("audit compressed positions codec mismatch")
    expected_bytes = compressed.get("uncompressed_bytes")
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
        or expected_bytes > MAX_AUDIT_CHANGED_POSITIONS_BYTES
    ):
        raise AdviceIntegrityError("audit compressed positions size is invalid")
    expected_hash = str(compressed.get("uncompressed_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise AdviceIntegrityError("audit compressed positions hash is invalid")
    encoded = compressed.get("data_base64")
    if not isinstance(encoded, str) or len(encoded) > MAX_AUDIT_CHANGED_POSITIONS_BYTES * 2:
        raise AdviceIntegrityError("audit compressed positions payload is invalid")
    try:
        packed = base64.b64decode(encoded.encode("ascii"), validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(
            packed, MAX_AUDIT_CHANGED_POSITIONS_BYTES + 1
        )
        if decompressor.unconsumed_tail:
            raise AdviceIntegrityError("audit compressed positions exceed size limit")
        raw += decompressor.flush(
            MAX_AUDIT_CHANGED_POSITIONS_BYTES + 1 - len(raw)
        )
    except (UnicodeEncodeError, ValueError, zlib.error) as exc:
        raise AdviceIntegrityError("audit compressed positions payload is invalid") from exc
    if (
        not decompressor.eof
        or decompressor.unused_data
        or len(raw) != expected_bytes
        or len(raw) > MAX_AUDIT_CHANGED_POSITIONS_BYTES
    ):
        raise AdviceIntegrityError("audit compressed positions length mismatch")
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise AdviceIntegrityError("audit compressed positions hash mismatch")
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdviceIntegrityError("audit compressed positions JSON is invalid") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise AdviceIntegrityError("audit compressed positions must decode to a list")
    return rows


def _checkpoint_history_hash(checkpoint: Mapping[str, Any]) -> str:
    return _canonical_hash(_without_key(checkpoint, "history_commitment_sha256"))


def _validate_checkpoint(record: Mapping[str, Any], index: int) -> None:
    checkpoint = record.get("checkpoint")
    if checkpoint is None:
        return
    if index != 1 or not isinstance(checkpoint, Mapping):
        raise AdviceIntegrityError("audit checkpoint is only valid on record 1")
    expected_fields = {
        "payload_type",
        "compacted_at",
        "generation",
        "prior_chain_head_sha256",
        "prior_record_count",
        "superseded_record_count",
        "retained_transition_count",
        "previous_history_commitment_sha256",
        "checkpoint_state_sha256",
        "history_commitment_sha256",
    }
    if set(checkpoint) != expected_fields:
        raise AdviceIntegrityError("audit checkpoint fields mismatch")
    if checkpoint.get("payload_type") != AUDIT_CHECKPOINT_PAYLOAD_TYPE:
        raise AdviceIntegrityError("audit checkpoint schema mismatch")
    if parse_datetime(checkpoint.get("compacted_at")) is None:
        raise AdviceIntegrityError("audit checkpoint timestamp is invalid")
    for field in ("generation", "prior_record_count", "superseded_record_count"):
        value = checkpoint.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AdviceIntegrityError(f"audit checkpoint {field} is invalid")
    retained = checkpoint.get("retained_transition_count")
    if not isinstance(retained, int) or isinstance(retained, bool) or retained < 0:
        raise AdviceIntegrityError("audit checkpoint retained_transition_count is invalid")
    if checkpoint.get("superseded_record_count") != checkpoint.get("prior_record_count"):
        raise AdviceIntegrityError("audit checkpoint superseded count mismatch")
    for field in (
        "prior_chain_head_sha256",
        "previous_history_commitment_sha256",
        "checkpoint_state_sha256",
        "history_commitment_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(checkpoint.get(field) or "")):
            raise AdviceIntegrityError(f"audit checkpoint {field} is invalid")
    if checkpoint.get("checkpoint_state_sha256") != record.get("resulting_state_sha256"):
        raise AdviceIntegrityError("audit checkpoint state binding mismatch")
    if checkpoint.get("history_commitment_sha256") != _checkpoint_history_hash(checkpoint):
        raise AdviceIntegrityError("audit checkpoint history commitment mismatch")


def _validate_audit_record(
    record: Mapping[str, Any],
    index: int,
    *,
    previous_hash: str | None,
    previous_state_hash: str | None,
) -> tuple[str, str]:
    """Validate one record while preserving the full-ledger chain contract."""

    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("payload_type") != AUDIT_PAYLOAD_TYPE
    ):
        raise AdviceIntegrityError(f"audit record {index} schema mismatch")
    if record.get("sequence") != index:
        raise AdviceIntegrityError(f"audit sequence mismatch at {index}")
    if record.get("previous_record_hash_sha256") != previous_hash:
        raise AdviceIntegrityError(f"audit chain mismatch at {index}")
    if record.get("previous_state_sha256") != previous_state_hash:
        raise AdviceIntegrityError(f"audit state chain mismatch at {index}")
    if record.get("record_hash_sha256") != _audit_hash(record):
        raise AdviceIntegrityError(f"audit record hash mismatch at {index}")
    _validate_checkpoint(record, index)
    compressed_positions = record.get("changed_positions_compressed") is not None
    for row in _audit_changed_positions(record):
        # The enclosing record safety walk below covers this row as well.  Keep
        # plain row safety validation here without traversing it twice.  Rows in
        # a compressed envelope must be checked after decoding because the
        # enclosing base64 text cannot reveal forbidden or credential fields.
        _validate_position_record(
            row, check_safe_output=compressed_positions
        )
    _assert_safe_output(record)
    return (
        str(record["record_hash_sha256"]),
        str(record.get("resulting_state_sha256") or ""),
    )


def _validate_audit_records(records: Sequence[Mapping[str, Any]]) -> None:
    """Verify the complete current epoch, including an optional compacted head."""

    previous_hash: str | None = None
    previous_state_hash: str | None = None
    for index, record in enumerate(records, 1):
        previous_hash, previous_state_hash = _validate_audit_record(
            record,
            index,
            previous_hash=previous_hash,
            previous_state_hash=previous_state_hash,
        )


def read_audit_ledger(path: Path) -> list[dict[str, Any]]:
    """Read and fully verify the append-only hash chain."""

    if not path.exists():
        return []
    if path.is_symlink():
        raise AdviceIntegrityError("advice audit ledger must not be a symbolic link")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise AdviceIntegrityError(f"audit line {line_number} is incomplete")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdviceIntegrityError(f"audit line {line_number} is invalid") from exc
            if not isinstance(value, dict):
                raise AdviceIntegrityError(f"audit line {line_number} must be an object")
            records.append(value)
    _validate_audit_records(records)
    return records


def _read_advice_state_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise AdviceIntegrityError("advice state must not be a symbolic link")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AdviceIntegrityError("advice state root must be an object")
    validate_advice_state(value)
    return value


def _apply_audit_record(
    previous_core: Mapping[str, Any] | None, record: Mapping[str, Any]
) -> dict[str, Any]:
    by_symbol = {
        str(row["thscode"]): copy.deepcopy(dict(row))
        for row in (previous_core or {}).get("positions") or []
        if isinstance(row, Mapping)
    }
    for symbol in record.get("removed_symbols") or []:
        by_symbol.pop(str(symbol), None)
    for row in _audit_changed_positions(record):
        by_symbol[str(row["thscode"])] = copy.deepcopy(dict(row))
    metadata = record.get("state_metadata") if isinstance(record.get("state_metadata"), Mapping) else {}
    core = {
        "schema_version": metadata.get("schema_version"),
        "payload_type": metadata.get("payload_type"),
        "portfolio_id": metadata.get("portfolio_id"),
        "portfolio_as_of": metadata.get("portfolio_as_of"),
        "portfolio_fingerprint_sha256": metadata.get(
            "portfolio_fingerprint_sha256"
        ),
        "generated_at": metadata.get("generated_at"),
        "positions": [by_symbol[symbol] for symbol in sorted(by_symbol)],
    }
    if _canonical_hash(core) != record.get("resulting_state_sha256"):
        raise AdviceIntegrityError("audit replay state hash mismatch")
    return core


def _replay_ledger(records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    core: dict[str, Any] | None = None
    for record in records:
        if (record.get("previous_state_sha256") or None) != (
            _canonical_hash(core) if core is not None else None
        ):
            raise AdviceIntegrityError("audit replay previous state mismatch")
        core = _apply_audit_record(core, record)
    return core


def _advance_audit_core(
    core: Mapping[str, Any] | None, record: Mapping[str, Any]
) -> dict[str, Any]:
    expected_previous = _canonical_hash(core) if core is not None else None
    if (record.get("previous_state_sha256") or None) != expected_previous:
        raise AdviceIntegrityError("audit replay previous state mismatch")
    return _apply_audit_record(core, record)


def _scan_audit_ledger_for_compaction(
    path: Path, *, keep_recent_transitions: int
) -> dict[str, Any]:
    """Verify and replay a ledger once while retaining only its recent tail.

    The former compactor materialized the complete JSONL ledger twice and then
    replayed the complete history several more times.  This scanner validates
    every source record and resulting-state hash, but keeps at most the desired
    transition tail plus one replay state in memory.
    """

    if not path.exists():
        return {
            "before_bytes": 0,
            "record_count": 0,
            "checkpoint_core": None,
            "checkpoint_recorded_at": None,
            "recent_records": [],
            "final_core": None,
            "tail_sha256": None,
            "previous_history_commitment_sha256": "0" * 64,
            "previous_checkpoint_generation": 0,
        }
    if path.is_symlink():
        raise AdviceIntegrityError("advice audit ledger must not be a symbolic link")

    keep_recent_transitions = max(0, int(keep_recent_transitions))
    recent: deque[dict[str, Any]] = deque()
    checkpoint_core: dict[str, Any] | None = None
    checkpoint_recorded_at: str | None = None
    previous_hash: str | None = None
    previous_state_hash: str | None = None
    previous_history_commitment = "0" * 64
    previous_checkpoint_generation = 0
    count = 0

    with path.open("r", encoding="utf-8") as handle:
        before_bytes = int(os.fstat(handle.fileno()).st_size)
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise AdviceIntegrityError(f"audit line {line_number} is incomplete")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdviceIntegrityError(
                    f"audit line {line_number} is invalid"
                ) from exc
            if not isinstance(value, dict):
                raise AdviceIntegrityError(
                    f"audit line {line_number} must be an object"
                )
            previous_hash, previous_state_hash = _validate_audit_record(
                value,
                line_number,
                previous_hash=previous_hash,
                previous_state_hash=previous_state_hash,
            )
            if line_number == 1 and isinstance(value.get("checkpoint"), Mapping):
                previous_history_commitment = str(
                    value["checkpoint"].get("history_commitment_sha256")
                    or "0" * 64
                )
                previous_checkpoint_generation = int(
                    value["checkpoint"].get("generation") or 0
                )
            recent.append(value)
            if len(recent) > keep_recent_transitions:
                evicted = recent.popleft()
                checkpoint_core = _advance_audit_core(checkpoint_core, evicted)
                checkpoint_recorded_at = str(evicted.get("recorded_at") or "")
            count = line_number

    # A checkpoint must absorb at least the first source record.  This matters
    # only for a short ledger where the configured tail is longer than history.
    retained = min(keep_recent_transitions, max(0, count - 1))
    while len(recent) > retained:
        evicted = recent.popleft()
        checkpoint_core = _advance_audit_core(checkpoint_core, evicted)
        checkpoint_recorded_at = str(evicted.get("recorded_at") or "")

    final_core = copy.deepcopy(checkpoint_core)
    for record in recent:
        final_core = _advance_audit_core(final_core, record)
    if count and final_core is None:
        raise AdviceIntegrityError("audit replay produced no advice state")

    return {
        "before_bytes": before_bytes,
        "record_count": count,
        "checkpoint_core": checkpoint_core,
        "checkpoint_recorded_at": checkpoint_recorded_at,
        "recent_records": list(recent),
        "final_core": final_core,
        "tail_sha256": previous_hash,
        "previous_history_commitment_sha256": previous_history_commitment,
        "previous_checkpoint_generation": previous_checkpoint_generation,
    }


def _audit_records_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def _atomic_write_audit_records(
    path: Path, records: Sequence[Mapping[str, Any]]
) -> None:
    """Replace a verified audit epoch without ever exposing a partial file."""

    _validate_audit_records(records)
    encoded = _audit_records_bytes(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AdviceIntegrityError("advice audit ledger must not be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".retention.tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _history_checkpoint_from_summary(
    checkpoint_core: Mapping[str, Any],
    *,
    generation: int,
    prior_chain_head_sha256: str,
    prior_record_count: int,
    previous_history_commitment_sha256: str,
    retained_transition_count: int,
    compacted_at: datetime,
) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "payload_type": AUDIT_CHECKPOINT_PAYLOAD_TYPE,
        "compacted_at": _iso(compacted_at),
        "generation": int(generation),
        "prior_chain_head_sha256": str(prior_chain_head_sha256),
        "prior_record_count": int(prior_record_count),
        "superseded_record_count": int(prior_record_count),
        "retained_transition_count": int(retained_transition_count),
        "previous_history_commitment_sha256": str(
            previous_history_commitment_sha256
        ),
        "checkpoint_state_sha256": _canonical_hash(checkpoint_core),
    }
    checkpoint["history_commitment_sha256"] = _checkpoint_history_hash(checkpoint)
    return checkpoint


def _history_checkpoint(
    records: Sequence[Mapping[str, Any]],
    checkpoint_core: Mapping[str, Any],
    *,
    retained_transition_count: int,
    compacted_at: datetime,
) -> dict[str, Any]:
    previous_checkpoint = (
        records[0].get("checkpoint")
        if records and isinstance(records[0].get("checkpoint"), Mapping)
        else {}
    )
    checkpoint = _history_checkpoint_from_summary(
        checkpoint_core,
        generation=int(previous_checkpoint.get("generation") or 0) + 1,
        prior_chain_head_sha256=str(records[-1]["record_hash_sha256"]),
        prior_record_count=len(records),
        previous_history_commitment_sha256=str(
            previous_checkpoint.get("history_commitment_sha256") or "0" * 64
        ),
        retained_transition_count=retained_transition_count,
        compacted_at=compacted_at,
    )
    return checkpoint


def _rebased_audit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    keep_recent_transitions: int,
    compacted_at: datetime,
) -> list[dict[str, Any]]:
    """Create a new epoch headed by a full-state checkpoint.

    The checkpoint commits to the prior chain head and an accumulated history
    commitment.  Recent transitions are then replayed with fresh contiguous
    sequence numbers and hashes, so ordinary load/replay validation is unchanged.
    """

    if not records:
        return []
    retained = min(max(0, int(keep_recent_transitions)), max(0, len(records) - 1))
    boundary = len(records) - retained
    checkpoint_core = _replay_ledger(records[:boundary])
    if checkpoint_core is None:
        raise AdviceIntegrityError("cannot create an empty advice audit checkpoint")
    checkpoint_metadata = _history_checkpoint(
        records,
        checkpoint_core,
        retained_transition_count=retained,
        compacted_at=compacted_at,
    )
    checkpoint_record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": AUDIT_PAYLOAD_TYPE,
        "sequence": 1,
        "recorded_at": str(records[boundary - 1].get("recorded_at") or _iso(compacted_at)),
        "previous_record_hash_sha256": None,
        "previous_state_sha256": None,
        "resulting_state_sha256": _canonical_hash(checkpoint_core),
        "state_metadata": {
            key: copy.deepcopy(checkpoint_core[key])
            for key in (
                "schema_version",
                "payload_type",
                "portfolio_id",
                "portfolio_as_of",
                "portfolio_fingerprint_sha256",
                "generated_at",
            )
        },
        "changed_symbols": [
            str(row["thscode"]) for row in checkpoint_core.get("positions") or []
        ],
        "removed_symbols": [],
        "changed_positions": copy.deepcopy(checkpoint_core.get("positions") or []),
        "checkpoint": checkpoint_metadata,
    }
    checkpoint_record["record_hash_sha256"] = _audit_hash(checkpoint_record)
    rebased: list[dict[str, Any]] = [checkpoint_record]
    previous_hash = str(checkpoint_record["record_hash_sha256"])
    previous_state_hash = str(checkpoint_record["resulting_state_sha256"])
    for original in records[boundary:]:
        rewritten = {
            key: copy.deepcopy(original.get(key))
            for key in (
                "schema_version",
                "payload_type",
                "recorded_at",
                "resulting_state_sha256",
                "state_metadata",
                "changed_symbols",
                "removed_symbols",
            )
        }
        rewritten["changed_positions_compressed"] = _compress_changed_positions(
            _audit_changed_positions(original)
        )
        rewritten["sequence"] = len(rebased) + 1
        rewritten["previous_record_hash_sha256"] = previous_hash
        rewritten["previous_state_sha256"] = previous_state_hash
        rewritten["record_hash_sha256"] = _audit_hash(rewritten)
        rebased.append(rewritten)
        previous_hash = str(rewritten["record_hash_sha256"])
        previous_state_hash = str(rewritten["resulting_state_sha256"])
    _validate_audit_records(rebased)
    final_core = _replay_ledger(rebased)
    original_core = _replay_ledger(records)
    if final_core is None or _canonical_hash(final_core) != _canonical_hash(original_core):
        raise AdviceIntegrityError("audit checkpoint changed the resulting advice state")
    return rebased


def _rebased_audit_records_from_scan(
    scan: Mapping[str, Any],
    *,
    keep_recent_transitions: int,
    compacted_at: datetime,
) -> list[dict[str, Any]]:
    """Build a compact epoch from a verified streaming scan."""

    record_count = int(scan.get("record_count") or 0)
    if record_count <= 0:
        return []
    recent = list(scan.get("recent_records") or [])
    retained = min(
        max(0, int(keep_recent_transitions)),
        len(recent),
        max(0, record_count - 1),
    )
    checkpoint_core = copy.deepcopy(scan.get("checkpoint_core"))
    checkpoint_recorded_at = str(scan.get("checkpoint_recorded_at") or "")
    while len(recent) > retained:
        superseded = recent.pop(0)
        checkpoint_core = _advance_audit_core(checkpoint_core, superseded)
        checkpoint_recorded_at = str(superseded.get("recorded_at") or "")
    if not isinstance(checkpoint_core, Mapping):
        raise AdviceIntegrityError("cannot create an empty advice audit checkpoint")

    checkpoint_metadata = _history_checkpoint_from_summary(
        checkpoint_core,
        generation=int(scan.get("previous_checkpoint_generation") or 0) + 1,
        prior_chain_head_sha256=str(scan.get("tail_sha256") or ""),
        prior_record_count=record_count,
        previous_history_commitment_sha256=str(
            scan.get("previous_history_commitment_sha256") or "0" * 64
        ),
        retained_transition_count=retained,
        compacted_at=compacted_at,
    )
    checkpoint_record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": AUDIT_PAYLOAD_TYPE,
        "sequence": 1,
        "recorded_at": checkpoint_recorded_at or _iso(compacted_at),
        "previous_record_hash_sha256": None,
        "previous_state_sha256": None,
        "resulting_state_sha256": _canonical_hash(checkpoint_core),
        "state_metadata": {
            key: copy.deepcopy(checkpoint_core[key])
            for key in (
                "schema_version",
                "payload_type",
                "portfolio_id",
                "portfolio_as_of",
                "portfolio_fingerprint_sha256",
                "generated_at",
            )
        },
        "changed_symbols": [
            str(row["thscode"]) for row in checkpoint_core.get("positions") or []
        ],
        "removed_symbols": [],
        "changed_positions": copy.deepcopy(checkpoint_core.get("positions") or []),
        "checkpoint": checkpoint_metadata,
    }
    checkpoint_record["record_hash_sha256"] = _audit_hash(checkpoint_record)
    rebased: list[dict[str, Any]] = [checkpoint_record]
    previous_hash = str(checkpoint_record["record_hash_sha256"])
    previous_state_hash = str(checkpoint_record["resulting_state_sha256"])
    for original in recent:
        rewritten = {
            key: copy.deepcopy(original.get(key))
            for key in (
                "schema_version",
                "payload_type",
                "recorded_at",
                "resulting_state_sha256",
                "state_metadata",
                "changed_symbols",
                "removed_symbols",
            )
        }
        rewritten["changed_positions_compressed"] = _compress_changed_positions(
            _audit_changed_positions(original)
        )
        rewritten["sequence"] = len(rebased) + 1
        rewritten["previous_record_hash_sha256"] = previous_hash
        rewritten["previous_state_sha256"] = previous_state_hash
        rewritten["record_hash_sha256"] = _audit_hash(rewritten)
        rebased.append(rewritten)
        previous_hash = str(rewritten["record_hash_sha256"])
        previous_state_hash = str(rewritten["resulting_state_sha256"])

    _validate_audit_records(rebased)
    final_core = _replay_ledger(rebased)
    source_final_core = scan.get("final_core")
    if (
        final_core is None
        or not isinstance(source_final_core, Mapping)
        or _canonical_hash(final_core) != _canonical_hash(source_final_core)
    ):
        raise AdviceIntegrityError("audit checkpoint changed the resulting advice state")
    return rebased


def load_advice_state(
    state_path: Path = DEFAULT_STATE_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    *,
    recover: bool = False,
) -> dict[str, Any] | None:
    """Load state and verify it against the ledger; optionally recover a torn commit."""

    state = _read_advice_state_file(state_path)
    scan = _scan_audit_ledger_for_compaction(
        audit_path,
        keep_recent_transitions=0,
    )
    record_count = int(scan.get("record_count") or 0)
    if record_count == 0:
        if state is not None and int((state.get("audit") or {}).get("sequence") or 0) != 0:
            raise AdviceIntegrityError("state refers to a missing audit ledger")
        return state

    ledger_core = scan.get("final_core")
    if not isinstance(ledger_core, Mapping):
        raise AdviceIntegrityError("audit replay produced no advice state")
    ledger_state = _state_from_core(
        ledger_core,
        audit_sequence=record_count,
        audit_tail_sha256=str(scan.get("tail_sha256") or ""),
    )
    state_matches = bool(
        state
        and state.get("content_sha256") == ledger_state.get("content_sha256")
        and state.get("audit") == ledger_state.get("audit")
    )
    if state_matches:
        return state
    if not recover:
        raise AdviceIntegrityError("state and audit ledger do not match")
    _atomic_write_json(state_path, ledger_state)
    return ledger_state


@contextlib.contextmanager
def _store_lock(state_path: Path) -> Iterable[None]:
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _compact_position_advice_store_locked(
    state_path: Path,
    audit_path: Path,
    *,
    max_bytes: int,
    keep_recent_transitions: int,
    dry_run: bool,
    now: datetime,
) -> dict[str, Any]:
    if isinstance(max_bytes, bool) or int(max_bytes) < 64 * 1024:
        raise ValueError("max_bytes must be at least 64 KiB")
    if (
        isinstance(keep_recent_transitions, bool)
        or int(keep_recent_transitions) < 0
        or int(keep_recent_transitions) > 10_000
    ):
        raise ValueError("keep_recent_transitions must be between 0 and 10000")
    max_bytes = int(max_bytes)
    keep_recent_transitions = int(keep_recent_transitions)
    persisted_state = _read_advice_state_file(state_path)
    scan = _scan_audit_ledger_for_compaction(
        audit_path,
        keep_recent_transitions=keep_recent_transitions,
    )
    before_bytes = int(scan["before_bytes"])
    before_records = int(scan["record_count"])
    state_needs_recovery = False
    if before_records == 0:
        if (
            persisted_state is not None
            and int((persisted_state.get("audit") or {}).get("sequence") or 0) != 0
        ):
            raise AdviceIntegrityError("state refers to a missing audit ledger")
        state = persisted_state
    else:
        final_core = scan.get("final_core")
        if not isinstance(final_core, Mapping):
            raise AdviceIntegrityError("audit replay produced no advice state")
        ledger_state = _state_from_core(
            final_core,
            audit_sequence=before_records,
            audit_tail_sha256=str(scan.get("tail_sha256") or ""),
        )
        state_matches = bool(
            persisted_state
            and persisted_state.get("content_sha256")
            == ledger_state.get("content_sha256")
            and persisted_state.get("audit") == ledger_state.get("audit")
        )
        if not state_matches and dry_run:
            raise AdviceIntegrityError("state and audit ledger do not match")
        state_needs_recovery = not state_matches
        state = persisted_state if state_matches else ledger_state
    base = {
        "ok": True,
        "dry_run": bool(dry_run),
        "compacted": False,
        "before_bytes": before_bytes,
        "after_bytes": before_bytes,
        "bytes_reclaimed": 0,
        "before_records": before_records,
        "after_records": before_records,
        "max_bytes": max_bytes,
        "keep_recent_transitions": keep_recent_transitions,
        "checkpoint": {},
        "state": copy.deepcopy(state),
    }
    if before_bytes <= max_bytes or before_records == 0:
        if state_needs_recovery and not dry_run and state is not None:
            _atomic_write_json(state_path, state)
        return base

    retained = min(keep_recent_transitions, max(0, before_records - 1))
    candidate: list[dict[str, Any]] = []
    encoded = b""
    while retained >= 0:
        candidate = _rebased_audit_records_from_scan(
            scan,
            keep_recent_transitions=retained,
            compacted_at=now,
        )
        encoded = _audit_records_bytes(candidate)
        if len(encoded) <= max_bytes:
            break
        retained -= 1
    if retained < 0 or not candidate or len(encoded) > max_bytes:
        raise AdviceIntegrityError(
            "advice audit checkpoint alone exceeds configured maximum size"
        )

    final_core = _replay_ledger(candidate)
    assert final_core is not None
    tail = candidate[-1]
    compacted_state = _state_from_core(
        final_core,
        audit_sequence=int(tail["sequence"]),
        audit_tail_sha256=str(tail["record_hash_sha256"]),
    )
    validate_advice_state(compacted_state)
    result = {
        **base,
        "compacted": True,
        "after_bytes": len(encoded),
        "bytes_reclaimed": max(0, before_bytes - len(encoded)),
        "after_records": len(candidate),
        "keep_recent_transitions": retained,
        "checkpoint": copy.deepcopy(candidate[0].get("checkpoint") or {}),
        "state": compacted_state,
    }
    if dry_run:
        return result

    # The ledger is committed first.  A crash between these two atomic replaces
    # is safely recoverable from the checkpoint by load_advice_state(recover=True).
    _atomic_write_audit_records(audit_path, candidate)
    _atomic_write_json(state_path, compacted_state)
    verified = load_advice_state(state_path, audit_path, recover=False)
    if verified is None or verified.get("content_sha256") != compacted_state.get("content_sha256"):
        raise AdviceIntegrityError("compacted advice state did not verify")
    result["state"] = verified
    return result


def compact_position_advice_store(
    state_path: Path = DEFAULT_STATE_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    *,
    max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
    keep_recent_transitions: int = DEFAULT_AUDIT_KEEP_RECENT_TRANSITIONS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bound the audit ledger while retaining a verifiable state checkpoint.

    The same inter-process lock used by advice updates serializes compaction.
    A dry run performs full chain/replay validation and returns exact projected
    byte counts without replacing either file.
    """

    state_path = Path(state_path)
    audit_path = Path(audit_path)
    observed = (now or _now()).astimezone(CN_TZ)
    with _store_lock(state_path):
        return _compact_position_advice_store_locked(
            state_path,
            audit_path,
            max_bytes=max_bytes,
            keep_recent_transitions=keep_recent_transitions,
            dry_run=dry_run,
            now=observed,
        )


def update_position_advice_store(
    portfolio: Mapping[str, Any],
    *,
    quotes: Any,
    position_news: Any,
    market_news: Any,
    market_state: Any,
    risk: Any,
    rebalance: Any,
    state_path: Path = DEFAULT_STATE_PATH,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    input_errors: Sequence[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate, append a hash-chained audit record, and atomically replace state.

    The audit line is fsynced before the state rename.  If a process stops in
    between, the next call replays the ledger and recovers the state before it
    evaluates new evidence.  Identical evidence performs no writes unless the
    legacy audit ledger is already above its configured size cap, in which case
    it is compacted without changing the advice content.
    """

    state_path = Path(state_path)
    audit_path = Path(audit_path)
    with _store_lock(state_path):
        compaction: dict[str, Any] | None = None
        try:
            audit_bytes = audit_path.stat().st_size
        except OSError:
            audit_bytes = 0
        # Repair an oversized legacy epoch before evaluating the next refresh.
        # Otherwise the refresh would replay the large epoch once to load it and
        # then a second time to compact it while every other updater waits.
        if audit_bytes > DEFAULT_AUDIT_MAX_BYTES:
            compaction = _compact_position_advice_store_locked(
                state_path,
                audit_path,
                max_bytes=DEFAULT_AUDIT_MAX_BYTES,
                keep_recent_transitions=DEFAULT_AUDIT_KEEP_RECENT_TRANSITIONS,
                dry_run=False,
                now=(now or _now()).astimezone(CN_TZ),
            )
            previous = copy.deepcopy(compaction["state"])
        else:
            previous = load_advice_state(state_path, audit_path, recover=True)
        evaluation = evaluate_position_advice(
            portfolio,
            quotes=quotes,
            position_news=position_news,
            market_news=market_news,
            market_state=market_state,
            risk=risk,
            rebalance=rebalance,
            previous_state=previous,
            input_errors=input_errors,
            now=now,
        )
        if not evaluation["changed"]:
            state = copy.deepcopy(previous)
            return {
                **evaluation,
                "state": state,
                **({"audit_compaction": compaction} if compaction is not None else {}),
            }

        core = evaluation["state_core"]
        previous_core = _state_core(previous) if previous else None
        previous_hash = _canonical_hash(previous_core) if previous_core else None
        resulting_hash = _canonical_hash(core)
        previous_audit = (previous or {}).get("audit") or {}
        sequence = int(previous_audit.get("sequence") or 0) + 1
        previous_tail = previous_audit.get("tail_sha256")
        changed_set = set(evaluation["changed_symbols"])
        changed_positions = [
            copy.deepcopy(row)
            for row in core["positions"]
            if row.get("thscode") in changed_set
        ]
        audit_record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "payload_type": AUDIT_PAYLOAD_TYPE,
            "sequence": sequence,
            "recorded_at": evaluation["evaluated_at"],
            "previous_record_hash_sha256": previous_tail,
            "previous_state_sha256": previous_hash,
            "resulting_state_sha256": resulting_hash,
            "state_metadata": {
                key: copy.deepcopy(core[key])
                for key in (
                    "schema_version",
                    "payload_type",
                    "portfolio_id",
                    "portfolio_as_of",
                    "portfolio_fingerprint_sha256",
                    "generated_at",
                )
            },
            "changed_symbols": sorted(changed_set),
            "removed_symbols": list(evaluation["removed_symbols"]),
            "changed_positions": changed_positions,
        }
        audit_record = _sanitize(audit_record)
        audit_record["record_hash_sha256"] = _audit_hash(audit_record)
        _assert_safe_output(audit_record)
        _append_audit(audit_path, audit_record)
        state = _state_from_core(
            core,
            audit_sequence=sequence,
            audit_tail_sha256=str(audit_record["record_hash_sha256"]),
        )
        _atomic_write_json(state_path, state)
        validate_advice_state(state)
        try:
            audit_bytes = audit_path.stat().st_size
        except OSError:
            audit_bytes = 0
        if audit_bytes > DEFAULT_AUDIT_MAX_BYTES:
            post_compaction = _compact_position_advice_store_locked(
                state_path,
                audit_path,
                max_bytes=DEFAULT_AUDIT_MAX_BYTES,
                keep_recent_transitions=DEFAULT_AUDIT_KEEP_RECENT_TRANSITIONS,
                dry_run=False,
                now=(now or _now()).astimezone(CN_TZ),
            )
            state = copy.deepcopy(post_compaction["state"])
            compaction = post_compaction
        return {
            **evaluation,
            "state": state,
            **({"audit_compaction": compaction} if compaction is not None else {}),
        }


def public_position_advice(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the small dashboard/API contract without internal evidence cursors."""

    if not state:
        return {
            "schema_version": SCHEMA_VERSION,
            "payload_type": PAYLOAD_TYPE,
            "ok": False,
            "positions": [],
        }
    validate_advice_state(state)
    rows = [
        {
            key: copy.deepcopy(row.get(key))
            for key in (
                "thscode",
                "name",
                "current_advice",
                "advice_time",
                "evidence_since",
                "evidence_through",
                "evidence_count",
                "evidence_digest_sha256",
                "trigger_evidence_summary",
                "rationale",
                "decision_mode",
                "data_verified",
                "increase_allowed",
                "gate_reason_codes",
                "advice_hash_sha256",
            )
        }
        for row in state.get("positions") or []
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": PAYLOAD_TYPE,
        "ok": bool(rows),
        "portfolio_id": state.get("portfolio_id"),
        "portfolio_as_of": state.get("portfolio_as_of"),
        "portfolio_fingerprint_sha256": state.get(
            "portfolio_fingerprint_sha256"
        ),
        "generated_at": state.get("generated_at"),
        "positions": rows,
        "integrity": {
            "content_sha256": state.get("content_sha256"),
            "audit_sequence": (state.get("audit") or {}).get("sequence"),
            "audit_tail_sha256": (state.get("audit") or {}).get("tail_sha256"),
        },
        "note": "建议仅供人工复核；不连接券商、不自动下单、不承诺收益。",
    }
    _assert_safe_output(payload)
    return payload
