#!/usr/bin/env python3
"""Fail-closed research radar for a configured A-share watchlist.

The radar classifies observable price states.  It never labels a security a
"buy" and it keeps ``eligible`` false unless account, portfolio-risk, market,
and completed-session confirmation gates all pass.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


CN_TZ = timezone(timedelta(hours=8))
ENGINE_VERSION = "2026-08-31.1"

STATE_LABELS = {
    "INVALIDATED": "逻辑失效/待复核",
    "RISK_REDUCTION": "进入风险复核区",
    "PULLBACK_ZONE": "进入回撤研究区",
    "BREAKOUT_PENDING": "突破待收盘确认",
    "CONFIRMED": "收盘与量能已确认",
    "WATCH": "继续观察",
    "DATA_UNVERIFIED": "数据待核验",
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _symbol_from_thscode(thscode: str) -> str:
    code, market = str(thscode).upper().split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market)
    if not prefix or len(code) != 6 or not code.isdigit():
        raise ValueError(f"unsupported thscode: {thscode!r}")
    return prefix + code


def _quote_map(quotes: Mapping[str, Any] | Sequence[Any]) -> dict[str, dict[str, Any]]:
    if isinstance(quotes, Mapping):
        collections = [quotes.get("watchlist"), quotes.get("positions"), quotes.get("items")]
        if all(value is None for value in collections):
            collections = [quotes]
    else:
        collections = [quotes]
    output: dict[str, dict[str, Any]] = {}
    for collection in collections:
        if isinstance(collection, Mapping):
            rows = collection.values()
        elif isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
            rows = collection
        else:
            continue
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            keys = {
                str(row.get("thscode") or "").upper(),
                str(row.get("symbol") or "").lower(),
                str(row.get("code") or ""),
            }
            for key in keys:
                if key:
                    output[key] = row
    return output


def _find_quote(mapping: Mapping[str, dict[str, Any]], thscode: str) -> dict[str, Any] | None:
    code = thscode.split(".", 1)[0]
    return mapping.get(thscode.upper()) or mapping.get(_symbol_from_thscode(thscode)) or mapping.get(code)


def _stock_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    stocks = snapshot.get("stocks")
    if not isinstance(stocks, Mapping):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for name, raw in stocks.items():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        quote = row.get("quote") if isinstance(row.get("quote"), Mapping) else {}
        symbol = str(row.get("symbol") or "").lower()
        code = str(quote.get("code") or symbol[-6:])
        output[str(name)] = row
        if code:
            output[code] = row
        if symbol:
            output[symbol] = row
    return output


def _find_daily(mapping: Mapping[str, dict[str, Any]], item: Mapping[str, Any]) -> dict[str, Any] | None:
    thscode = str(item.get("thscode") or "")
    code = thscode.split(".", 1)[0]
    return mapping.get(code) or mapping.get(str(item.get("name") or "")) or mapping.get(_symbol_from_thscode(thscode))


def _index_map(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = snapshot.get("indices")
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        symbol = str(row.get("symbol") or "").lower()
        output[str(name)] = row
        if symbol:
            output[symbol] = row
    return output


def _recent_closes(
    row: Mapping[str, Any], data_date: date | None
) -> list[dict[str, Any]]:
    raw = row.get("recent_closes")
    values: dict[date, float] = {}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            session = _parse_date(item.get("date"))
            close = _number(item.get("close"))
            if session and close is not None and close > 0 and (data_date is None or session <= data_date):
                values[session] = close
    latest_date = _parse_date(row.get("date")) or data_date
    latest_close = _number(row.get("close"))
    if latest_date and latest_close is not None and latest_close > 0:
        values[latest_date] = latest_close
    return [
        {"date": session.isoformat(), "close": values[session]}
        for session in sorted(values)
    ]


def _compare(value: float, operator: str, threshold: float) -> bool | None:
    if operator == "strictly_greater_than":
        return value > threshold
    if operator == "greater_than_or_equal":
        return value >= threshold
    if operator == "strictly_less_than":
        return value < threshold
    if operator == "less_than_or_equal":
        return value <= threshold
    return None


def _benchmark_condition(
    configured: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    thscode = str(configured.get("thscode") or "").upper()
    metric = str(configured.get("metric") or "")
    operator = str(configured.get("operator") or "")
    threshold = _number(configured.get("threshold"))
    try:
        sessions = int(configured.get("consecutive_sessions") or 1)
    except (TypeError, ValueError):
        sessions = 0
    try:
        symbol = _symbol_from_thscode(thscode)
    except (TypeError, ValueError):
        symbol = ""
    row = _index_map(snapshot).get(symbol)
    data_date = _parse_date(snapshot.get("data_date"))
    closes = _recent_closes(row or {}, data_date)
    selected = closes[-sessions:] if sessions > 0 else []
    complete = bool(
        metric == "close"
        and threshold is not None
        and sessions > 0
        and sessions <= 20
        and row
        and len(selected) == sessions
        and data_date
        and _parse_date(selected[-1].get("date")) == data_date
    )
    checks = (
        [_compare(float(item["close"]), operator, threshold) for item in selected]
        if complete and threshold is not None
        else []
    )
    if complete and any(value is None for value in checks):
        complete = False
    passed = bool(complete and checks and all(value is True for value in checks))
    return {
        "kind": "benchmark",
        "configured": dict(configured),
        "evidence_complete": complete,
        "passed": passed,
        "sessions": selected,
        "reason_code": (
            "BENCHMARK_CONDITION_PASSED"
            if passed
            else "BENCHMARK_CONDITION_NOT_MET"
            if complete
            else "BENCHMARK_CONDITION_EVIDENCE_UNVERIFIED"
        ),
    }


def _liquidity_condition(
    configured: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    expected_sessions: Sequence[str],
) -> dict[str, Any]:
    scope = str(configured.get("scope") or "")
    metric = str(configured.get("metric") or "")
    operator = str(configured.get("operator") or "")
    raw = snapshot.get("market_turnover_history")
    history: dict[str, float] = {}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            session = _parse_date(item.get("date"))
            amount = _number(item.get("amount_yi"))
            if session and amount is not None and amount > 0:
                history[session.isoformat()] = amount
    required = list(expected_sessions)[-2:]
    observations = [
        {"date": session, "amount_yi": history.get(session)} for session in required
    ]
    complete = bool(
        scope == "all_a_shares"
        and metric == "turnover_amount"
        and operator == "expanding"
        and len(required) == 2
        and all(item["amount_yi"] is not None for item in observations)
    )
    passed = bool(
        complete
        and float(observations[-1]["amount_yi"])
        > float(observations[-2]["amount_yi"])
    )
    return {
        "kind": "liquidity",
        "configured": dict(configured),
        "evidence_complete": complete,
        "passed": passed,
        "sessions": observations,
        "reason_code": (
            "LIQUIDITY_CONDITION_PASSED"
            if passed
            else "LIQUIDITY_CONDITION_NOT_MET"
            if complete
            else "LIQUIDITY_CONDITION_EVIDENCE_UNVERIFIED"
        ),
    }


def _market_condition_gate(
    item: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    configured = item.get("market_condition")
    if not isinstance(configured, Mapping) or not configured:
        return {
            "configured": False,
            "evidence_complete": True,
            "passed": True,
            "checks": [],
            "blocked_by": [],
        }
    checks: list[dict[str, Any]] = []
    benchmark = configured.get("benchmark")
    if isinstance(benchmark, Mapping):
        checks.append(_benchmark_condition(benchmark, snapshot))
    liquidity = configured.get("liquidity")
    if isinstance(liquidity, Mapping):
        expected_sessions: list[str] = []
        benchmark_check = next(
            (check for check in checks if check.get("kind") == "benchmark"), None
        )
        if benchmark_check:
            expected_sessions = [
                str(value.get("date") or "")
                for value in benchmark_check.get("sessions") or []
            ]
        if len(expected_sessions) < 2:
            data_date = _parse_date(snapshot.get("data_date"))
            for row in _index_map(snapshot).values():
                closes = _recent_closes(row, data_date)
                if len(closes) >= 2:
                    expected_sessions = [str(value["date"]) for value in closes[-2:]]
                    break
        checks.append(_liquidity_condition(liquidity, snapshot, expected_sessions))
    recognized = {check["kind"] for check in checks}
    configured_keys = {str(key) for key in configured}
    if not checks or configured_keys - recognized:
        checks.append(
            {
                "kind": "unsupported",
                "configured": {
                    key: value for key, value in configured.items() if str(key) not in recognized
                },
                "evidence_complete": False,
                "passed": False,
                "reason_code": "MARKET_CONDITION_UNSUPPORTED",
            }
        )
    complete = all(check.get("evidence_complete") is True for check in checks)
    passed = bool(complete and all(check.get("passed") is True for check in checks))
    blocked_by = (
        []
        if passed
        else [
            "MARKET_CONDITION_NOT_MET"
            if complete
            else "MARKET_CONDITION_EVIDENCE_UNVERIFIED"
        ]
    )
    return {
        "configured": True,
        "evidence_complete": complete,
        "passed": passed,
        "checks": checks,
        "blocked_by": blocked_by,
    }


def _risk_gate(risk: Mapping[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not risk.get("ok"):
        blockers.append("RISK_SNAPSHOT_UNVERIFIED")
    if not risk.get("new_buy_allowed"):
        blockers.append("PORTFOLIO_NEW_BUY_GATE_CLOSED")
    quality = risk.get("data_quality") if isinstance(risk.get("data_quality"), Mapping) else {}
    account = quality.get("account_snapshot") if isinstance(quality.get("account_snapshot"), Mapping) else {}
    if account.get("current_verified") is not True and account.get("verified") is not True:
        blockers.append("ACCOUNT_SNAPSHOT_NOT_CURRENT")
    return not blockers, blockers


def _market_gate(market: Mapping[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not market.get("ok") or market.get("regime") == "UNVERIFIED":
        blockers.append("MARKET_STATE_UNVERIFIED")
    guidance = market.get("guidance") if isinstance(market.get("guidance"), Mapping) else {}
    if guidance.get("regime_allows_new_risk") is not True:
        blockers.append(f"MARKET_REGIME_{market.get('regime') or 'UNKNOWN'}")
    return not blockers, blockers


def _price_state(item: Mapping[str, Any], price: float, daily: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    pullback_low = _number(item.get("pullback_low"))
    pullback_high = _number(item.get("pullback_high"))
    breakout = _number(item.get("breakout"))
    invalidation = _number(item.get("invalidation"))
    risk_reduction = _number(item.get("risk_reduction"))
    reasons: list[str] = []
    if invalidation is not None and price <= invalidation:
        return "INVALIDATED", ["PRICE_AT_OR_BELOW_INVALIDATION"]
    if risk_reduction is not None and price <= risk_reduction:
        return "RISK_REDUCTION", ["PRICE_AT_OR_BELOW_RISK_REDUCTION"]
    if pullback_low is not None and pullback_high is not None and pullback_low <= price <= pullback_high:
        return "PULLBACK_ZONE", ["PRICE_INSIDE_PULLBACK_RESEARCH_ZONE"]
    if breakout is not None and price >= breakout:
        technical = daily.get("technical") if isinstance(daily, Mapping) and isinstance(daily.get("technical"), Mapping) else {}
        completed_close = _number(technical.get("close"))
        volume_confirmation = _number(technical.get("volume_5d_vs_20d_pct"))
        if completed_close is not None and completed_close >= breakout and volume_confirmation is not None and volume_confirmation > 0:
            reasons.extend(["COMPLETED_CLOSE_ABOVE_BREAKOUT", "VOLUME_5D_ABOVE_20D"])
            return "CONFIRMED", reasons
        return "BREAKOUT_PENDING", ["LIVE_PRICE_ABOVE_BREAKOUT", "COMPLETED_CLOSE_OR_VOLUME_NOT_CONFIRMED"]
    return "WATCH", ["NO_CONFIGURED_PRICE_TRIGGER"]


def _proximity_score(item: Mapping[str, Any], price: float) -> float:
    triggers = [
        _number(item.get("pullback_low")),
        _number(item.get("pullback_high")),
        _number(item.get("breakout")),
    ]
    distances = [abs(price / trigger - 1.0) for trigger in triggers if trigger and trigger > 0]
    if not distances:
        return 0.0
    return max(0.0, 1.0 - min(distances) / 0.12)


def _score(item: Mapping[str, Any], price: float, daily: Mapping[str, Any] | None, market: Mapping[str, Any]) -> dict[str, float]:
    technical = daily.get("technical") if isinstance(daily, Mapping) and isinstance(daily.get("technical"), Mapping) else {}
    financial = daily.get("latest_financial") if isinstance(daily, Mapping) and isinstance(daily.get("latest_financial"), Mapping) else {}
    averages = technical.get("moving_averages") if isinstance(technical.get("moving_averages"), Mapping) else {}
    ma20, ma60 = _number(averages.get("20")), _number(averages.get("60"))
    trend_values = []
    if ma20:
        trend_values.append(max(-1.0, min(1.0, (price / ma20 - 1.0) / 0.08)))
    if ma60:
        trend_values.append(max(-1.0, min(1.0, (price / ma60 - 1.0) / 0.12)))
    trend = sum(trend_values) / len(trend_values) if trend_values else 0.0
    volume = _number(technical.get("volume_5d_vs_20d_pct"))
    volume_score = max(-1.0, min(1.0, (volume or 0.0) / 30.0)) if volume is not None else 0.0
    revenue = financial.get("revenue") if isinstance(financial.get("revenue"), Mapping) else {}
    profit = financial.get("net_profit") if isinstance(financial.get("net_profit"), Mapping) else {}
    growth_values = [value for value in (_number(revenue.get("yoy_pct")), _number(profit.get("yoy_pct"))) if value is not None]
    fundamental = max(-1.0, min(1.0, (sum(growth_values) / len(growth_values)) / 40.0)) if growth_values else 0.0
    market_score = _number(market.get("score")) or 0.0
    proximity = _proximity_score(item, price)
    total = 0.30 * proximity + 0.25 * trend + 0.15 * volume_score + 0.20 * fundamental + 0.10 * market_score
    return {
        "total": round(total, 4),
        "trigger_proximity": round(proximity, 4),
        "trend": round(trend, 4),
        "volume": round(volume_score, 4),
        "fundamental": round(fundamental, 4),
        "market": round(market_score, 4),
    }


def build_opportunity_radar(
    watchlist: Mapping[str, Any],
    quotes: Mapping[str, Any] | Sequence[Any],
    daily_snapshot: Mapping[str, Any],
    market_state: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed_at = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    quote_mapping = _quote_map(quotes)
    daily_mapping = _stock_map(daily_snapshot)
    risk_allowed, risk_blockers = _risk_gate(risk_snapshot)
    market_allowed, market_blockers = _market_gate(market_state)
    review_by = _parse_date(watchlist.get("review_by"))
    watchlist_stale = bool(review_by and observed_at.date() > review_by)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    condition_unverified: list[str] = []
    for raw in watchlist.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        thscode = str(item.get("thscode") or "")
        quote = _find_quote(quote_mapping, thscode)
        daily = _find_daily(daily_mapping, item)
        price = _number((quote or {}).get("price"))
        validation = (
            (quote or {}).get("official_validation")
            if isinstance((quote or {}).get("official_validation"), Mapping)
            else {}
        )
        validation_failed = bool(
            validation.get("required") and validation.get("passed") is not True
        )
        condition_gate = _market_condition_gate(item, daily_snapshot)
        blockers = [
            *risk_blockers,
            *market_blockers,
            *(condition_gate.get("blocked_by") or []),
        ]
        if condition_gate.get("evidence_complete") is not True:
            condition_unverified.append(thscode)
        if watchlist_stale:
            blockers.append("WATCHLIST_REVIEW_EXPIRED")
        quote_time = (quote or {}).get("quote_time")
        if quote is None or price is None or price <= 0 or quote.get("is_stale") or validation_failed:
            state = "DATA_UNVERIFIED"
            if validation_failed:
                state_reasons = ["OFFICIAL_QUOTE_VALIDATION_FAILED"]
                blockers.append("OFFICIAL_QUOTE_VALIDATION_FAILED")
            else:
                state_reasons = ["LIVE_QUOTE_MISSING_OR_STALE"]
                blockers.append("LIVE_QUOTE_MISSING_OR_STALE")
            missing.append(thscode)
            price = price or 0.0
        else:
            state, state_reasons = _price_state(item, price, daily)
        if state in {
            "INVALIDATED",
            "RISK_REDUCTION",
            "PULLBACK_ZONE",
            "BREAKOUT_PENDING",
            "WATCH",
            "DATA_UNVERIFIED",
        }:
            blockers.append(f"STATE_{state}")
        if state == "CONFIRMED" and not (risk_allowed and market_allowed):
            blockers.append("CONFIRMATION_BLOCKED_BY_PORTFOLIO_GATES")
        blockers = list(dict.fromkeys(blockers))
        scores = _score(item, price, daily, market_state) if price > 0 else {"total": None}
        breakout = _number(item.get("breakout"))
        invalidation = _number(item.get("invalidation"))
        pullback_low = _number(item.get("pullback_low"))
        pullback_high = _number(item.get("pullback_high"))
        rows.append(
            {
                "thscode": thscode,
                "name": item.get("name"),
                "theme": item.get("theme"),
                "state": state,
                "state_label": STATE_LABELS[state],
                "eligible": state == "CONFIRMED" and not blockers,
                "blocked_by": blockers,
                "reason_codes": state_reasons,
                "price": _round(price, 4) if price > 0 else None,
                "change_pct": _round(_number((quote or {}).get("change_pct")), 3),
                "quote_time": quote_time,
                "data_source": (quote or {}).get("data_source") or (quote or {}).get("source") or "",
                "official_validation": dict(validation),
                "pullback_zone": [pullback_low, pullback_high],
                "breakout": breakout,
                "invalidation": invalidation,
                "distance_to_breakout_pct": _round((breakout / price - 1.0) * 100, 2) if breakout and price else None,
                "distance_to_invalidation_pct": _round((price / invalidation - 1.0) * 100, 2) if invalidation and price else None,
                "score_components": scores,
                "fundamental_invalidation": item.get("fundamental_invalidation"),
                "market_condition": item.get("market_condition"),
                "market_condition_evaluation": condition_gate,
                "completed_session": daily_snapshot.get("data_date"),
            }
        )
    state_rank = {"INVALIDATED": 0, "RISK_REDUCTION": 1, "PULLBACK_ZONE": 2, "BREAKOUT_PENDING": 3, "CONFIRMED": 4, "WATCH": 5, "DATA_UNVERIFIED": 6}
    rows.sort(key=lambda row: (state_rank.get(str(row.get("state")), 99), -(_number((row.get("score_components") or {}).get("total")) or -99)))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "watchlist_as_of": watchlist.get("as_of"),
                "review_by": watchlist.get("review_by"),
                "market_snapshot": market_state.get("snapshot_id"),
                "risk_generated": risk_snapshot.get("generated_at"),
                "quotes": [(row["thscode"], row["price"], row["quote_time"]) for row in rows],
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    return {
        "ok": not missing and not condition_unverified,
        "generated_at": observed_at.isoformat(timespec="seconds"),
        "radar_id": fingerprint,
        "engine_version": ENGINE_VERSION,
        "watchlist_as_of": watchlist.get("as_of"),
        "review_by": watchlist.get("review_by"),
        "watchlist_stale": watchlist_stale,
        "market_regime": market_state.get("regime"),
        "new_buy_gate_open": bool(risk_allowed and market_allowed and not watchlist_stale),
        "items": rows,
        "counts_by_state": {state: sum(1 for row in rows if row["state"] == state) for state in STATE_LABELS},
        "missing_quotes": missing,
        "unverified_market_conditions": condition_unverified,
        "note": "仅显示研究触发与失效条件；eligible=false时不得把观察信号解释为买入建议。",
    }
