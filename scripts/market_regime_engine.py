#!/usr/bin/env python3
"""Explainable, fail-closed market-regime classification for the local dashboard.

The engine is intentionally pure: it does not fetch data and it never places or
suggests an order.  It combines a live quote payload, a completed-session daily
snapshot, and an optional official monthly macro snapshot.  Missing or stale
core market evidence produces ``UNVERIFIED`` rather than a guessed signal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "market-regime-policy.json"
DEFAULT_DAILY_PATH = ROOT / "data" / "market-snapshot.json"
DEFAULT_MACRO_PATH = ROOT / "data" / "macro-snapshot.json"
DEFAULT_OUTPUT_PATH = ROOT / "data" / "market-regime-snapshot.json"

CN_TZ = timezone(timedelta(hours=8))
ENGINE_VERSION = "2026-08-31.1"

INDEX_WEIGHTS = {
    "上证综指": 0.10,
    "沪深300": 0.20,
    "中证500": 0.15,
    "中证1000": 0.15,
    "深证成指": 0.10,
    "创业板指": 0.15,
    "科创50": 0.15,
}

INDEX_ALIASES = {
    "上证指数": "上证综指",
    "上证综指": "上证综指",
    "深证成指": "深证成指",
    "创业板指": "创业板指",
    "科创50": "科创50",
    "沪深300": "沪深300",
    "中证500": "中证500",
    "中证1000": "中证1000",
}

GLOBAL_WEIGHTS = {
    "hf_ES": 0.24,
    "hf_NQ": 0.24,
    "hkHSI": 0.18,
    "b_NKY": 0.09,
    "b_KOSPI": 0.09,
    "fx_susdcnh": 0.10,
    "hf_CL": 0.06,
}

REGIME_LABELS = {
    "RISK_OFF": "风险收缩",
    "CAUTIOUS": "谨慎",
    "NEUTRAL": "中性",
    "RISK_ON": "风险偏好改善",
    "UNVERIFIED": "数据待核验",
}


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw, pattern)
                    break
                except ValueError:
                    continue
            else:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _month_index(value: str | None) -> int | None:
    raw = str(value or "")[:7]
    try:
        year, month = (int(part) for part in raw.split("-", 1))
    except (ValueError, TypeError):
        return None
    if not 1 <= month <= 12:
        return None
    return year * 12 + month


def _canonical_indices(quotes: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    markets = quotes.get("markets") if isinstance(quotes.get("markets"), Mapping) else {}
    raw = markets.get("indices") if isinstance(markets, Mapping) else []
    output: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        name = INDEX_ALIASES.get(str(item.get("name") or item.get("source_name") or ""))
        if name:
            output[name] = dict(item)
    return output


def _daily_indices(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = snapshot.get("indices")
    if not isinstance(raw, Mapping):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        name = INDEX_ALIASES.get(str(key)) or INDEX_ALIASES.get(str(value.get("name") or ""))
        if name:
            output[name] = dict(value)
    return output


def _quote_freshness(
    quotes: Mapping[str, Any], policy: Mapping[str, Any], observed_at: datetime
) -> dict[str, Any]:
    quality = policy.get("data_quality") if isinstance(policy.get("data_quality"), Mapping) else {}
    market_status = quotes.get("market_status") if isinstance(quotes.get("market_status"), Mapping) else {}
    phase = str(market_status.get("state") or "unknown").lower()
    quote_time = _parse_datetime(market_status.get("quote_time"))
    if quote_time is None:
        candidates = [
            _parse_datetime(item.get("quote_time"))
            for item in _canonical_indices(quotes).values()
        ]
        quote_time = max((value for value in candidates if value is not None), default=None)
    future_tolerance = float(quality.get("future_tolerance_seconds") or 120)
    if phase in {"open", "auction"}:
        maximum_age = float(quality.get("open_quote_max_age_seconds") or 180)
    elif phase == "lunch":
        maximum_age = float(quality.get("lunch_quote_max_age_seconds") or 7200)
    else:
        maximum_age = float(quality.get("closed_quote_max_age_seconds") or 259200)
    age = (observed_at - quote_time).total_seconds() if quote_time else None
    usable = bool(
        quote_time
        and age is not None
        and age >= -future_tolerance
        and age <= maximum_age
        and not market_status.get("feed_stale")
        and not (quotes.get("cache") or {}).get("expired")
    )
    return {
        "phase": phase,
        "quote_time": quote_time.isoformat(timespec="seconds") if quote_time else None,
        "age_seconds": _round(max(age, 0.0), 1) if age is not None else None,
        "maximum_age_seconds": maximum_age,
        "usable": usable,
        "reason": "" if usable else "实时市场证据缺失、超时或被行情源标记为延迟",
    }


def _daily_freshness(
    snapshot: Mapping[str, Any],
    policy: Mapping[str, Any],
    observed_at: datetime,
    quotes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    quality = policy.get("data_quality") if isinstance(policy.get("data_quality"), Mapping) else {}
    data_date = _parse_date(snapshot.get("data_date"))
    maximum_lag = int(quality.get("daily_snapshot_max_calendar_day_lag") or 4)
    lag = (observed_at.date() - data_date).days if data_date else None
    future = bool(data_date and data_date > observed_at.date())
    validation = snapshot.get("validation") if isinstance(snapshot.get("validation"), Mapping) else {}
    validated = validation.get("passed") is not False
    market_status = (
        quotes.get("market_status")
        if isinstance(quotes, Mapping) and isinstance(quotes.get("market_status"), Mapping)
        else {}
    )
    phase = str(market_status.get("state") or "unknown").lower()
    quote_time = _parse_datetime(market_status.get("quote_time"))
    quote_session = quote_time.date() if quote_time else None
    require_closed_match = bool(
        quality.get("daily_snapshot_must_match_closed_quote_session", True)
    )
    closed_session_match = bool(
        not require_closed_match
        or phase != "closed"
        or quote_session is None
        or data_date == quote_session
    )
    usable = bool(
        data_date
        and not future
        and lag is not None
        and lag <= maximum_lag
        and validated
        and closed_session_match
    )
    if not closed_session_match:
        reason = "收盘行情已进入新完成交易日，但日线快照尚未同步到同一交易日"
    else:
        reason = "完成交易日快照缺失、未来日期、过旧或交叉验证未通过"
    return {
        "data_date": data_date.isoformat() if data_date else None,
        "calendar_day_lag": lag,
        "maximum_calendar_day_lag": maximum_lag,
        "cross_source_validation_passed": validated,
        "market_phase": phase,
        "quote_session": quote_session.isoformat() if quote_session else None,
        "closed_session_match_required": require_closed_match,
        "closed_session_match": closed_session_match,
        "calendar_basis": (
            "closed markets must match the independently timestamped quote session; "
            "calendar lag remains a pre-close holiday/weekend guard"
        ),
        "usable": usable,
        "reason": "" if usable else reason,
    }


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float | None:
    valid = [(value, weight) for value, weight in values if math.isfinite(value) and weight > 0]
    total = sum(weight for _, weight in valid)
    return sum(value * weight for value, weight in valid) / total if total else None


def _trend_component(
    live: Mapping[str, Mapping[str, Any]], daily: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    scores: list[tuple[float, float]] = []
    for name, weight in INDEX_WEIGHTS.items():
        quote = live.get(name)
        history = daily.get(name)
        if not quote or not history:
            continue
        price = _number(quote.get("price")) or _number(history.get("close"))
        averages = history.get("moving_averages") if isinstance(history.get("moving_averages"), Mapping) else {}
        returns = history.get("returns_pct") if isinstance(history.get("returns_pct"), Mapping) else {}
        ma20, ma60, ma120 = (_number(averages.get(key)) for key in ("20", "60", "120"))
        return20 = _number(returns.get("20"))
        day_change = _number(quote.get("change_pct"))
        if not price or not ma20 or not ma60:
            continue
        parts = [
            (_clip((price / ma20 - 1.0) / 0.03), 0.30),
            (_clip((price / ma60 - 1.0) / 0.06), 0.25),
            (_clip((price / ma120 - 1.0) / 0.10), 0.15) if ma120 else (0.0, 0.0),
            (_clip((return20 or 0.0) / 10.0), 0.15) if return20 is not None else (0.0, 0.0),
            (_clip((day_change or 0.0) / 2.5), 0.15) if day_change is not None else (0.0, 0.0),
        ]
        score = _weighted_mean(parts)
        if score is None:
            continue
        scores.append((score, weight))
        details.append(
            {
                "name": name,
                "price": _round(price, 3),
                "change_pct": _round(day_change, 3),
                "return_20d_pct": _round(return20, 3),
                "distance_ma20_pct": _round((price / ma20 - 1.0) * 100, 3),
                "distance_ma60_pct": _round((price / ma60 - 1.0) * 100, 3),
                "distance_ma120_pct": _round((price / ma120 - 1.0) * 100, 3) if ma120 else None,
                "score": _round(score),
            }
        )
    score = _weighted_mean(scores)
    above20 = sum(1 for item in details if (_number(item.get("distance_ma20_pct")) or -999) > 0)
    above60 = sum(1 for item in details if (_number(item.get("distance_ma60_pct")) or -999) > 0)
    return {
        "available": score is not None,
        "score": _round(score),
        "label": "趋势改善" if score is not None and score >= 0.2 else "趋势承压" if score is not None and score <= -0.2 else "趋势分化",
        "indices_used": len(details),
        "above_ma20_count": above20,
        "above_ma60_count": above60,
        "details": details,
    }


def _breadth_component(quotes: Mapping[str, Any]) -> dict[str, Any]:
    markets = quotes.get("markets") if isinstance(quotes.get("markets"), Mapping) else {}
    breadth = markets.get("breadth") if isinstance(markets, Mapping) and isinstance(markets.get("breadth"), Mapping) else {}
    up, flat, down = (_number(breadth.get(key)) for key in ("up", "flat", "down"))
    directional = (up or 0.0) + (down or 0.0)
    total = _number(breadth.get("total")) or directional + (flat or 0.0)
    if up is None or down is None or directional <= 0:
        return {"available": False, "score": None, "label": "广度缺失"}
    balance = (up - down) / directional
    score = _clip(balance / 0.35)
    up_ratio = up / total * 100 if total else None
    return {
        "available": True,
        "score": _round(score),
        "label": "多数上涨" if score >= 0.2 else "多数下跌" if score <= -0.2 else "涨跌接近",
        "up": int(up),
        "flat": int(flat or 0),
        "down": int(down),
        "total": int(total or 0),
        "up_ratio_pct": _round(up_ratio, 2),
        "advance_decline_balance_pct": _round(balance * 100, 2),
    }


def _session_progress(phase: str, observed_at: datetime) -> float | None:
    minutes = observed_at.hour * 60 + observed_at.minute
    if phase in {"pre_open", "auction"}:
        return None
    if phase == "lunch":
        return 0.52
    if phase == "closed":
        return 1.0
    if phase != "open":
        return None
    if minutes <= 11 * 60 + 30:
        elapsed = max(0, minutes - (9 * 60 + 30))
        return max(0.06, min(0.52, 0.06 + elapsed / 120 * 0.46))
    elapsed = max(0, minutes - 13 * 60)
    return max(0.55, min(1.0, 0.55 + elapsed / 120 * 0.45))


def _liquidity_component(
    quotes: Mapping[str, Any], daily_snapshot: Mapping[str, Any], observed_at: datetime
) -> dict[str, Any]:
    markets = quotes.get("markets") if isinstance(quotes.get("markets"), Mapping) else {}
    breadth = markets.get("breadth") if isinstance(markets, Mapping) and isinstance(markets.get("breadth"), Mapping) else {}
    current = _number(breadth.get("amount_yi"))
    previous_root = daily_snapshot.get("breadth") if isinstance(daily_snapshot.get("breadth"), Mapping) else {}
    previous_combined = previous_root.get("沪深合计") if isinstance(previous_root.get("沪深合计"), Mapping) else {}
    previous = _number(previous_combined.get("amount_yi"))
    status = quotes.get("market_status") if isinstance(quotes.get("market_status"), Mapping) else {}
    phase = str(status.get("state") or "unknown")
    progress = _session_progress(phase, observed_at)
    if current is None or previous is None or previous <= 0 or progress is None or progress <= 0:
        return {"available": False, "score": None, "label": "成交比较暂不可用"}
    pace_ratio = current / (previous * progress)
    score = _clip((pace_ratio - 1.0) / 0.45)
    projected = current / progress
    return {
        "available": True,
        "score": _round(score),
        "label": "成交放大" if score >= 0.2 else "成交收缩" if score <= -0.2 else "成交接近前日",
        "current_amount_yi": _round(current, 2),
        "previous_full_day_amount_yi": _round(previous, 2),
        "session_progress_assumption": _round(progress, 3),
        "pace_ratio": _round(pace_ratio, 3),
        "projected_full_day_amount_yi": _round(projected, 2),
        "note": "盘中成交额按交易阶段估算，仅用于相对节奏，不替代收盘成交额。",
    }


def _volatility_component(
    live: Mapping[str, Mapping[str, Any]], daily: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    vols: list[float] = []
    day_changes: list[float] = []
    details: list[dict[str, Any]] = []
    for name in INDEX_WEIGHTS:
        history = daily.get(name)
        quote = live.get(name)
        if not history or not quote:
            continue
        vol = _number(history.get("volatility_20d_annualized_pct"))
        change = _number(quote.get("change_pct"))
        if vol is not None:
            vols.append(vol)
        if change is not None:
            day_changes.append(change)
        details.append({"name": name, "volatility_20d_pct": vol, "change_pct": change})
    if not vols:
        return {"available": False, "score": None, "label": "波动率缺失"}
    median_vol = median(vols)
    vol_score = _clip((22.0 - median_vol) / 18.0)
    downside = min(day_changes, default=0.0)
    downside_score = _clip(downside / 4.0) if downside < 0 else 0.0
    score = 0.75 * vol_score + 0.25 * downside_score
    return {
        "available": True,
        "score": _round(score),
        "label": "波动偏高" if score <= -0.2 else "波动可控" if score >= 0.2 else "波动中等",
        "median_20d_annualized_pct": _round(median_vol, 2),
        "worst_index_change_pct": _round(downside, 2),
        "details": details,
    }


def _global_component(quotes: Mapping[str, Any]) -> dict[str, Any]:
    markets = quotes.get("markets") if isinstance(quotes.get("markets"), Mapping) else {}
    raw = markets.get("global") if isinstance(markets, Mapping) else []
    rows = {str(item.get("symbol")): item for item in raw or [] if isinstance(item, Mapping)}
    values: list[tuple[float, float]] = []
    details: list[dict[str, Any]] = []
    for symbol, weight in GLOBAL_WEIGHTS.items():
        item = rows.get(symbol)
        if not item:
            continue
        change = _number(item.get("change_pct"))
        if change is None:
            continue
        if symbol == "fx_susdcnh":
            score = _clip(-change / 0.8)
        elif symbol == "hf_CL":
            score = _clip(-max(change - 1.0, 0.0) / 4.0 + max(-change - 5.0, 0.0) / 5.0)
        else:
            score = _clip(change / 2.0)
        values.append((score, weight))
        details.append(
            {
                "symbol": symbol,
                "name": item.get("name"),
                "change_pct": _round(change, 3),
                "quote_time": item.get("quote_time"),
                "source": item.get("source"),
                "score": _round(score),
            }
        )
    score = _weighted_mean(values)
    return {
        "available": score is not None,
        "score": _round(score),
        "label": "外部风险偏好偏弱" if score is not None and score <= -0.2 else "外部风险偏好偏强" if score is not None and score >= 0.2 else "外部市场中性",
        "observations_used": len(details),
        "details": details,
    }


def _macro_pmi(macro: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = macro.get("pmi")
    if not isinstance(raw, Mapping):
        return {}
    current = raw.get("current")
    return current if isinstance(current, Mapping) else raw


def _macro_component(macro: Mapping[str, Any] | None, observed_at: datetime, policy: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(macro, Mapping) or not macro:
        return {"available": False, "score": None, "label": "官方宏观快照缺失", "fresh": False}
    pmi = _macro_pmi(macro)
    period = str(pmi.get("period") or "")
    current_month = observed_at.year * 12 + observed_at.month
    evidence_month = _month_index(period)
    max_lag = int((policy.get("data_quality") or {}).get("macro_max_month_lag") or 2)
    month_lag = current_month - evidence_month if evidence_month is not None else None
    fresh = bool(month_lag is not None and 0 <= month_lag <= max_lag)
    manufacturing = _number(pmi.get("manufacturing_pmi"))
    non_manufacturing = _number(pmi.get("non_manufacturing_pmi"))
    composite = _number(pmi.get("composite_pmi"))
    pmi_values = [value for value in (manufacturing, non_manufacturing, composite) if value is not None]
    scores: list[tuple[float, float]] = []
    for value in pmi_values:
        scores.append((_clip((value - 50.0) / 2.0), 1.0))

    previous = (macro.get("pmi") or {}).get("previous") if isinstance(macro.get("pmi"), Mapping) else None
    if isinstance(previous, Mapping) and manufacturing is not None:
        previous_manufacturing = _number(previous.get("manufacturing_pmi"))
        if previous_manufacturing is not None:
            scores.append((_clip((manufacturing - previous_manufacturing) / 1.5), 0.75))

    social = macro.get("social_financing") if isinstance(macro.get("social_financing"), Mapping) else {}
    yoy = _number(social.get("year_over_year_pct"))
    ytd_yoy = _number(social.get("ytd_year_over_year_pct"))
    if yoy is not None:
        scores.append((_clip(yoy / 20.0), 0.5))
    if ytd_yoy is not None:
        scores.append((_clip(ytd_yoy / 15.0), 0.75))
    score = _weighted_mean(scores)
    available = bool(fresh and score is not None and len(pmi_values) == 3)
    return {
        "available": available,
        "score": _round(score) if available else None,
        "label": "宏观景气改善" if available and score >= 0.2 else "宏观景气偏弱" if available and score <= -0.2 else "宏观景气中性" if available else "官方宏观证据过旧或不完整",
        "fresh": fresh,
        "period": period or None,
        "month_lag": month_lag,
        "manufacturing_pmi": manufacturing,
        "non_manufacturing_pmi": non_manufacturing,
        "composite_pmi": composite,
        "social_financing_latest_month": social.get("latest_month"),
        "social_financing_yoy_pct": yoy,
        "social_financing_ytd_yoy_pct": ytd_yoy,
        "source_url": pmi.get("source_url"),
    }


def _style_state(
    live: Mapping[str, Mapping[str, Any]], daily: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    def average_change(names: Sequence[str], field: str, horizon: str | None = None) -> float | None:
        values: list[float] = []
        for name in names:
            if field == "live":
                value = _number((live.get(name) or {}).get("change_pct"))
            else:
                returns = (daily.get(name) or {}).get("returns_pct")
                value = _number(returns.get(horizon)) if isinstance(returns, Mapping) else None
            if value is not None:
                values.append(value)
        return sum(values) / len(values) if values else None

    groups = {
        "large": ["上证综指", "沪深300"],
        "growth": ["创业板指", "科创50"],
        "small_mid": ["中证500", "中证1000"],
    }
    live_change = {key: average_change(names, "live") for key, names in groups.items()}
    return20 = {key: average_change(names, "daily", "20") for key, names in groups.items()}
    combined = {
        key: (live_change[key] or 0.0) * 0.35 + (return20[key] or 0.0) * 0.65
        for key in groups
        if live_change[key] is not None or return20[key] is not None
    }
    if not combined:
        return {"available": False, "leader": None, "label": "风格证据缺失"}
    leader = max(combined, key=combined.get)
    laggard = min(combined, key=combined.get)
    labels = {"large": "大盘价值", "growth": "成长科技", "small_mid": "中小盘"}
    spread = combined[leader] - combined[laggard]
    return {
        "available": True,
        "leader": leader,
        "leader_label": labels[leader],
        "laggard": laggard,
        "laggard_label": labels[laggard],
        "lead_spread": _round(spread, 3),
        "label": f"{labels[leader]}相对占优" if spread >= 0.75 else "风格分化不强",
        "live_change_pct": {key: _round(value, 3) for key, value in live_change.items()},
        "return_20d_pct": {key: _round(value, 3) for key, value in return20.items()},
    }


def _classify(score: float, policy: Mapping[str, Any]) -> str:
    thresholds = policy.get("regime_thresholds") if isinstance(policy.get("regime_thresholds"), Mapping) else {}
    if score <= float(thresholds.get("risk_off_max") or -0.35):
        return "RISK_OFF"
    if score <= float(thresholds.get("cautious_max") or -0.12):
        return "CAUTIOUS"
    if score < float(thresholds.get("neutral_max") or 0.18):
        return "NEUTRAL"
    return "RISK_ON"


def _reason_codes(components: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    labels = {
        "trend": ("TREND_WEAK", "TREND_STRONG", "主要指数趋势偏弱", "主要指数趋势偏强"),
        "breadth": ("BREADTH_WEAK", "BREADTH_STRONG", "下跌家数占优", "上涨家数占优"),
        "liquidity": ("LIQUIDITY_CONTRACTING", "LIQUIDITY_EXPANDING", "成交节奏收缩", "成交节奏放大"),
        "volatility": ("VOLATILITY_HIGH", "VOLATILITY_LOW", "指数波动偏高", "指数波动较低"),
        "global_risk": ("GLOBAL_RISK_WEAK", "GLOBAL_RISK_STRONG", "海外风险资产偏弱", "海外风险资产偏强"),
        "macro": ("MACRO_WEAK", "MACRO_STRONG", "官方宏观景气偏弱", "官方宏观景气改善"),
    }
    reasons: list[dict[str, str]] = []
    ranked = sorted(
        ((name, _number(item.get("score"))) for name, item in components.items()),
        key=lambda pair: abs(pair[1] or 0.0),
        reverse=True,
    )
    for name, score in ranked:
        if score is None or abs(score) < 0.18 or name not in labels:
            continue
        neg_code, pos_code, neg_text, pos_text = labels[name]
        reasons.append({"code": pos_code if score > 0 else neg_code, "text": pos_text if score > 0 else neg_text})
    return reasons[:5]


def _guidance(regime: str, policy: Mapping[str, Any]) -> dict[str, Any]:
    root = policy.get("exposure_guidance") if isinstance(policy.get("exposure_guidance"), Mapping) else {}
    configured = root.get(regime) if isinstance(root.get(regime), Mapping) else {}
    messages = {
        "RISK_OFF": "只做风险收缩与证据核验；避免追涨和新增高贝塔敞口。",
        "CAUTIOUS": "控制总敞口，新增风险需等待收盘趋势与市场广度共同确认。",
        "NEUTRAL": "保持分散和风险预算，不以单日涨跌替代交易确认。",
        "RISK_ON": "风险偏好改善仍需日线收盘确认，避免突破当日一次性满仓。",
        "UNVERIFIED": "数据门禁未通过；停止依赖该状态新增风险敞口。",
    }
    return {
        "equity_exposure_floor_pct": configured.get("floor_pct"),
        "equity_exposure_ceiling_pct": configured.get("ceiling_pct"),
        "regime_allows_new_risk": bool(configured.get("allow_new_risk")),
        "message": messages[regime],
        "confirmation_conditions": [
            "以完成交易日收盘价确认主要指数相对20日与60日均线的位置",
            "市场上涨家数占比连续改善且并非仅少数权重股拉动",
            "海外股指期货、人民币与A股风格信号不再明显背离",
        ],
        "invalidation_conditions": [
            "主要指数跌破最近完成交易日低点且市场广度继续恶化",
            "高波动成长指数单日下跌扩大并伴随成交放大",
            "行情、账户或宏观证据过期时立即把状态降为待核验",
        ],
        "disclaimer": "区间是组合风险预算参考，不是收益预测、目标仓位命令或个股买卖建议。",
    }


def build_market_state(
    quotes: Mapping[str, Any],
    daily_snapshot: Mapping[str, Any],
    macro_snapshot: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one deterministic market-state snapshot from supplied evidence."""
    observed_at = (now or _now()).astimezone(CN_TZ)
    selected_policy = dict(policy or _read_json(DEFAULT_POLICY_PATH))
    live = _canonical_indices(quotes)
    daily = _daily_indices(daily_snapshot)
    quote_quality = _quote_freshness(quotes, selected_policy, observed_at)
    daily_quality = _daily_freshness(
        daily_snapshot, selected_policy, observed_at, quotes
    )
    minimum_indices = int((selected_policy.get("data_quality") or {}).get("minimum_core_indices") or 5)
    common_count = len(set(live) & set(daily))

    components: dict[str, dict[str, Any]] = {
        "trend": _trend_component(live, daily),
        "breadth": _breadth_component(quotes),
        "liquidity": _liquidity_component(quotes, daily_snapshot, observed_at),
        "volatility": _volatility_component(live, daily),
        "global_risk": _global_component(quotes),
        "macro": _macro_component(macro_snapshot, observed_at, selected_policy),
    }
    weights = selected_policy.get("component_weights") if isinstance(selected_policy.get("component_weights"), Mapping) else {}
    weighted: list[tuple[float, float]] = []
    available_weight = 0.0
    total_weight = sum(float(value) for value in weights.values()) or 1.0
    for name, component in components.items():
        score = _number(component.get("score")) if component.get("available") else None
        weight = float(weights.get(name) or 0.0)
        if score is not None and weight > 0:
            weighted.append((score, weight))
            available_weight += weight
    aggregate = _weighted_mean(weighted)
    coverage = available_weight / total_weight
    minimum_coverage = float((selected_policy.get("data_quality") or {}).get("minimum_component_coverage") or 0.65)

    critical_failures: list[str] = []
    warnings: list[str] = []
    if not quote_quality["usable"]:
        critical_failures.append(quote_quality["reason"])
    if not daily_quality["usable"]:
        critical_failures.append(daily_quality["reason"])
    if common_count < minimum_indices:
        critical_failures.append(f"核心指数覆盖不足：{common_count}/{minimum_indices}")
    if coverage < minimum_coverage:
        critical_failures.append(f"评分分项覆盖不足：{coverage:.0%}/{minimum_coverage:.0%}")
    if not components["macro"]["available"]:
        warnings.append("最新官方宏观快照缺失或过旧；总分已按可用市场分项重新归一化")
    if not components["global_risk"]["available"]:
        warnings.append("全球风险资产观测缺失")

    if critical_failures or aggregate is None:
        regime = "UNVERIFIED"
        aggregate_score = None
    else:
        aggregate_score = aggregate
        regime = _classify(aggregate, selected_policy)

    core_quality = 1.0
    core_quality *= min(1.0, common_count / max(minimum_indices, 1))
    if not quote_quality["usable"]:
        core_quality *= 0.25
    if not daily_quality["usable"]:
        core_quality *= 0.35
    confidence = _clip(coverage * core_quality, 0.0, 1.0)
    style = _style_state(live, daily)
    reasons = _reason_codes(components)

    evidence = {
        "quote_time": quote_quality.get("quote_time"),
        "completed_session": daily_quality.get("data_date"),
        "macro_period": components["macro"].get("period"),
        "quote_source": "腾讯财经/新浪财经公开行情",
        "daily_source": (daily_snapshot.get("sources") or {}).get("recipe") if isinstance(daily_snapshot.get("sources"), Mapping) else None,
        "macro_source": "国家统计局/中国人民银行" if macro_snapshot else None,
        "grade": "observation_grade",
    }
    fingerprint_body = json.dumps(
        {
            "quote_time": evidence["quote_time"],
            "completed_session": evidence["completed_session"],
            "macro_period": evidence["macro_period"],
            "policy_version": selected_policy.get("policy_version"),
            "scores": {key: value.get("score") for key, value in components.items()},
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    snapshot_id = hashlib.sha256(fingerprint_body.encode("utf-8")).hexdigest()[:20]

    return {
        "ok": regime != "UNVERIFIED",
        "generated_at": observed_at.isoformat(timespec="seconds"),
        "snapshot_id": snapshot_id,
        "engine_version": ENGINE_VERSION,
        "policy_version": selected_policy.get("policy_version"),
        "regime": regime,
        "label": REGIME_LABELS[regime],
        "score": _round(aggregate_score),
        "confidence": _round(confidence),
        "style": style,
        "components": components,
        "reason_codes": reasons,
        "guidance": _guidance(regime, selected_policy),
        "quality": {
            "status": "CRITICAL" if critical_failures else "DEGRADED" if warnings else "GOOD",
            "component_coverage_pct": _round(coverage * 100, 2),
            "common_index_count": common_count,
            "quote": quote_quality,
            "daily_snapshot": daily_quality,
            "critical_failures": critical_failures,
            "warnings": warnings,
        },
        "evidence": evidence,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an explainable local A-share market regime snapshot")
    parser.add_argument("--quotes", type=Path, required=True, help="JSON payload shaped like /api/quotes")
    parser.add_argument("--daily", type=Path, default=DEFAULT_DAILY_PATH)
    parser.add_argument("--macro", type=Path, default=DEFAULT_MACRO_PATH)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--now", help="optional ISO timestamp for reproducible runs")
    args = parser.parse_args(argv)
    macro = _read_json(args.macro) if args.macro.exists() else None
    observed_at = _parse_datetime(args.now) if args.now else None
    if args.now and observed_at is None:
        raise SystemExit("--now must be an ISO-8601 timestamp")
    snapshot = build_market_state(
        _read_json(args.quotes),
        _read_json(args.daily),
        macro,
        _read_json(args.policy),
        observed_at,
    )
    _atomic_write_json(args.output, snapshot)
    print(
        f"market regime: {snapshot['regime']} score={snapshot['score']} "
        f"confidence={snapshot['confidence']} -> {args.output}"
    )
    return 0 if snapshot["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
