#!/usr/bin/env python3
"""Fail-closed portfolio rebalancing drafts for the local A-share portfolio.

The engine consumes the *output* of ``portfolio_risk_engine``.  It deliberately
does not fetch quotes or infer whether a broker snapshot is current.  Exact
share counts are emitted only after the upstream snapshot explicitly proves
that holdings, cash, debt, and quotes are current.  Even then, output is an
indicative, staged draft: this module never submits an order.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTFOLIO = ROOT / "config" / "portfolio.json"
DEFAULT_POLICY = ROOT / "config" / "risk-policy.json"
DEFAULT_RISK = ROOT / "data" / "risk-snapshot.json"
CN_TZ = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1
UNKNOWN_AGGREGATE_RISK_SCALE = 0.50
DEFAULT_EVENT_PAYLOAD_MAX_AGE_SECONDS = 900.0
DEFAULT_LIQUIDITY_PAYLOAD_MAX_AGE_SECONDS = 900.0
LIQUIDITY_PAYLOAD_TYPE = "portfolio-liquidity-adv.v1"
LIQUIDITY_LOOKBACK_SESSIONS = 20
EPSILON = 1e-9

SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
EVENT_CAP_FACTOR = {"none": 1.0, "low": 1.0, "medium": 0.85, "high": 0.60, "critical": 0.25}
BLOCKING_EVENT_TERMS = (
    "停牌",
    "暂停交易",
    "suspend",
    "suspension",
    "halt",
    "退市",
    "delist",
)
SECURITY_EVENT_SCOPES = frozenset(
    {"position", "security", "company", "symbol", "stock", "instrument", "issuer"}
)


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _finite(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _round(value: Any, digits: int = 4) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def _nonnegative(value: Any, default: float = 0.0) -> float:
    number = _finite(value, default)
    return max(0.0, float(number if number is not None else default))


def _integer(value: Any, default: int = 0) -> int:
    number = _finite(value)
    if number is None:
        return default
    return int(math.floor(max(0.0, number) + EPSILON))


def _required_nonnegative(value: Any, label: str, *, integer: bool = False) -> float:
    number = _finite(value)
    if number is None or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    if integer and abs(number - round(number)) > EPSILON:
        raise ValueError(f"{label} must be an integer")
    return float(number)


def _normalise_thscode(value: Any) -> str:
    raw = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", raw)
    if match:
        return raw
    prefix = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if prefix:
        return f"{prefix.group(2)}.{prefix.group(1)}"
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith("92") or raw.startswith(("43", "83", "87")):
            market = "BJ"
        elif raw.startswith(("5", "6", "9")):
            market = "SH"
        else:
            market = "SZ"
        return f"{raw}.{market}"
    return raw


def _position_key(account_id: Any, thscode: Any) -> str:
    return f"{str(account_id or '').strip()}::{_normalise_thscode(thscode)}"


def _portfolio_records(portfolio: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    accounts: dict[str, dict[str, Any]] = {}
    raw_accounts = portfolio.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("portfolio accounts must be a non-empty list")
    for account in raw_accounts:
        if not isinstance(account, dict):
            raise ValueError("portfolio account rows must be objects")
        account_id = str(account.get("account_id") or "").strip()
        if not account_id or account_id in accounts:
            raise ValueError("portfolio accounts require unique, non-empty account_id values")
        cash = _required_nonnegative(account.get("cash"), f"{account_id}.cash")
        debt = _required_nonnegative(account.get("debt"), f"{account_id}.debt")
        accounts[account_id] = {
            "account_id": account_id,
            "account_type": str(account.get("account_type") or ""),
            "cash_cny": cash,
            "debt_cny": debt,
        }
        raw_positions = account.get("positions")
        if not isinstance(raw_positions, list):
            raise ValueError(f"{account_id}.positions must be a list")
        for item in raw_positions:
            if not isinstance(item, dict):
                raise ValueError(f"{account_id}.positions rows must be objects")
            thscode = _normalise_thscode(item.get("thscode"))
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", thscode):
                raise ValueError(f"invalid thscode in portfolio: {item.get('thscode')!r}")
            quantity = _required_nonnegative(
                item.get("quantity"), f"{account_id}.{thscode}.quantity", integer=True
            )
            available = _required_nonnegative(
                item.get("available"), f"{account_id}.{thscode}.available", integer=True
            )
            if available > quantity + EPSILON:
                raise ValueError(f"{account_id}.{thscode}.available exceeds quantity")
            record = copy.deepcopy(item)
            record.update(
                {
                    "account_id": account_id,
                    "account_type": accounts[account_id]["account_type"],
                    "thscode": thscode,
                    "position_key": _position_key(account_id, thscode),
                    "quantity": int(quantity),
                    "available": int(available),
                }
            )
            records.append(record)
    if not records:
        raise ValueError("portfolio contains no positions")
    keys = [row["position_key"] for row in records]
    if len(keys) != len(set(keys)):
        raise ValueError("portfolio contains duplicate account/symbol positions")
    return records, accounts


def _risk_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in snapshot.get("positions") or []:
        if not isinstance(item, dict):
            continue
        record = copy.deepcopy(item)
        record["account_id"] = str(record.get("account_id") or "").strip()
        record["thscode"] = _normalise_thscode(record.get("thscode") or record.get("symbol"))
        record["position_key"] = _position_key(record["account_id"], record["thscode"])
        records.append(record)
    return records


def _policy_contract_errors(policy: dict[str, Any]) -> list[str]:
    limits = policy.get("portfolio_limits") if isinstance(policy.get("portfolio_limits"), dict) else {}
    execution = policy.get("execution") if isinstance(policy.get("execution"), dict) else {}
    quality = policy.get("data_quality") if isinstance(policy.get("data_quality"), dict) else {}
    errors: list[str] = []

    bounded_percentages = (
        "target_equity_exposure_pct",
        "max_gross_exposure_pct",
        "minimum_cash_pct",
        "maximum_debt_pct",
        "maximum_single_position_pct",
        "maximum_top5_pct",
        "maximum_theme_pct",
    )
    positive_limits = (
        "maximum_annualized_volatility_pct",
        "maximum_daily_es_97_5_pct",
        "maximum_5d_stress_loss_pct",
        "maximum_drawdown_pct",
        "single_trade_loss_budget_pct",
    )
    for key in bounded_percentages:
        value = _finite(limits.get(key))
        if value is None or value < 0 or value > 100:
            errors.append(f"portfolio_limits.{key}")
    for key in positive_limits:
        value = _finite(limits.get(key))
        if value is None or value <= 0:
            errors.append(f"portfolio_limits.{key}")

    for key in ("minimum_rebalance_gap_pct_points", "minimum_relative_gap_pct"):
        value = _finite(execution.get(key))
        if value is None or value < 0:
            errors.append(f"execution.{key}")
    for key in (
        "initial_order_fraction_of_gap",
        "existing_position_rebalance_fraction_of_gap",
    ):
        value = _finite(execution.get(key))
        if value is None or value <= 0 or value > 1:
            errors.append(f"execution.{key}")
    confirmation = _finite(execution.get("normal_signal_confirmation_closes"))
    if confirmation is None or confirmation < 1 or abs(confirmation - round(confirmation)) > EPSILON:
        errors.append("execution.normal_signal_confirmation_closes")
    participation = _finite(execution.get("maximum_adv_participation_pct_per_day"))
    if participation is None or participation <= 0 or participation > 100:
        errors.append("execution.maximum_adv_participation_pct_per_day")
    horizon = _finite(execution.get("liquidity_horizon_days"))
    if horizon is None or horizon <= 0:
        errors.append("execution.liquidity_horizon_days")
    lot_size = _finite(execution.get("a_share_lot_size"))
    if lot_size is None or lot_size < 1 or abs(lot_size - round(lot_size)) > EPSILON:
        errors.append("execution.a_share_lot_size")
    if not isinstance(execution.get("t_plus_one"), bool):
        errors.append("execution.t_plus_one")

    live_ttl = _finite(quality.get("live_quote_ttl_seconds"))
    hard_ttl = _finite(quality.get("live_quote_hard_stale_seconds"))
    if live_ttl is None or live_ttl <= 0:
        errors.append("data_quality.live_quote_ttl_seconds")
    if hard_ttl is None or hard_ttl <= 0 or (live_ttl is not None and hard_ttl < live_ttl):
        errors.append("data_quality.live_quote_hard_stale_seconds")
    return sorted(set(errors))


def _verification_gate(
    portfolio: dict[str, Any],
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Return an explicit proof gate; never infer freshness from HTTP success."""
    reasons: list[dict[str, str]] = []
    quality = snapshot.get("data_quality") if isinstance(snapshot.get("data_quality"), dict) else {}
    account_quality = quality.get("account_snapshot") if isinstance(quality.get("account_snapshot"), dict) else {}
    quotes = quality.get("quotes") if isinstance(quality.get("quotes"), dict) else {}
    cache = snapshot.get("cache") if isinstance(snapshot.get("cache"), dict) else {}
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    quality_policy = policy.get("data_quality") if isinstance(policy.get("data_quality"), dict) else {}

    account_verified = (
        account_quality.get("verified") is True
        and account_quality.get("current_verified") is True
        and account_quality.get("stale") is False
    )
    if not account_verified:
        reasons.append(
            {
                "code": "ACCOUNT_SNAPSHOT_NOT_CURRENT",
                "message": "账户持仓、可用数量、现金和负债尚未被核验为当前交易日状态",
            }
        )

    configured_as_of = str(portfolio.get("as_of") or "")
    quality_as_of = str(account_quality.get("as_of") or "")
    as_of_matches = bool(configured_as_of and quality_as_of and configured_as_of == quality_as_of)
    if not as_of_matches:
        reasons.append(
            {
                "code": "SNAPSHOT_DATE_MISMATCH",
                "message": "portfolio.json 与风险快照的账户日期不一致",
            }
        )

    state = str(snapshot.get("state") or "")
    valid_risk_state = state in {"NORMAL", "RISK_OFF", "REDUCE_ONLY_CRITICAL"}
    if not valid_risk_state:
        reasons.append(
            {
                "code": "UPSTREAM_DATA_UNVERIFIED",
                "message": "上游风险状态未通过数据质量门禁",
            }
        )

    critical_failures = quality.get("critical_failures") or []
    if critical_failures:
        reasons.append(
            {
                "code": "UPSTREAM_CRITICAL_FAILURE",
                "message": "上游仍存在关键数据失败",
            }
        )

    coverage = _integer(quotes.get("coverage_count"), -1)
    position_count = _integer(quotes.get("position_count"), -1)
    missing = list(quotes.get("missing_positions") or [])
    stale = list(quotes.get("stale_positions") or [])
    quotes_verified = position_count > 0 and coverage == position_count and not missing and not stale
    if not quotes_verified:
        reasons.append(
            {
                "code": "QUOTES_INCOMPLETE_OR_STALE",
                "message": "持仓行情未达到全覆盖且无陈旧报价的要求",
            }
        )

    if cache.get("stale") is True:
        reasons.append(
            {
                "code": "RISK_CACHE_STALE",
                "message": "风险快照来自陈旧缓存",
            }
        )

    if cache.get("fresh") is not True:
        reasons.append(
            {
                "code": "RISK_CACHE_NOT_PROVEN_FRESH",
                "message": "风险快照缓存未显式证明为新鲜状态",
            }
        )
    input_fingerprint = str(cache.get("input_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", input_fingerprint):
        reasons.append(
            {
                "code": "RISK_INPUT_FINGERPRINT_MISSING",
                "message": "风险快照没有有效的上游输入指纹，无法排除旧进程或旧载荷",
            }
        )

    explicit_schema = snapshot.get("schema_version")
    if _integer(explicit_schema, -1) != 1:
        reasons.append(
            {
                "code": "RISK_SCHEMA_MISMATCH",
                "message": "风险快照 schema_version 与本引擎支持的契约不一致",
            }
        )
    explicit_policy = snapshot.get("policy_version")
    if not explicit_policy or str(explicit_policy) != str(policy.get("policy_version")):
        reasons.append(
            {
                "code": "RISK_POLICY_MISMATCH",
                "message": "风险快照与当前 risk-policy.json 版本不一致",
            }
        )
    explicit_portfolio = snapshot.get("portfolio_id")
    if not explicit_portfolio or str(explicit_portfolio) != str(portfolio.get("portfolio_id")):
        reasons.append(
            {
                "code": "RISK_PORTFOLIO_MISMATCH",
                "message": "风险快照与当前 portfolio_id 不一致",
            }
        )
    if _integer(portfolio.get("schema_version"), -1) != 1:
        reasons.append(
            {
                "code": "PORTFOLIO_SCHEMA_MISMATCH",
                "message": "portfolio.json 缺失或不支持 schema_version=1",
            }
        )
    if _integer(policy.get("schema_version"), -1) != 1 or not policy.get("policy_version"):
        reasons.append(
            {
                "code": "POLICY_SCHEMA_OR_VERSION_MISSING",
                "message": "risk-policy.json 必须包含 schema_version=1 和 policy_version",
            }
        )
    policy_errors = _policy_contract_errors(policy)
    if policy_errors:
        reasons.append(
            {
                "code": "POLICY_CONTRACT_INVALID",
                "message": "risk-policy.json 缺失或非法字段：" + ", ".join(policy_errors),
            }
        )

    hard_stale_seconds = _nonnegative(
        quality_policy.get("live_quote_hard_stale_seconds"), 300.0
    )
    market_phase = str(quotes.get("market_phase") or "open").lower()
    lunch_stale_seconds = _nonnegative(
        quality_policy.get("lunch_quote_max_age_seconds"), 7_200.0
    )
    closed_stale_seconds = _nonnegative(
        quality_policy.get("closed_quote_max_age_seconds"), 259_200.0
    )
    phase_stale_seconds = (
        hard_stale_seconds
        if market_phase in {"open", "auction"}
        else lunch_stale_seconds
        if market_phase == "lunch"
        else closed_stale_seconds
    )
    quote_age = _finite(quotes.get("quote_age_seconds"))
    if (
        quotes.get("feed_hard_stale") is not False
        or quote_age is None
        or quote_age < 0
        or quote_age > phase_stale_seconds
    ):
        reasons.append(
            {
                "code": "QUOTE_AGE_CONTRACT_FAILED",
                "message": "行情年龄或 feed_hard_stale 标志未通过当前交易时段阈值复核",
            }
        )

    generated = _parse_datetime(snapshot.get("generated_at"))
    risk_age = (now - generated).total_seconds() if generated else None
    maximum_risk_age = max(
        hard_stale_seconds,
        _nonnegative(cache.get("ttl_seconds"), 15.0) * 4,
    )
    if risk_age is None or risk_age < -60 or risk_age > maximum_risk_age:
        reasons.append(
            {
                "code": "RISK_SNAPSHOT_AGE_FAILED",
                "message": "风险快照生成时间已超出本地可执行时效",
            }
        )

    try:
        configured, _ = _portfolio_records(portfolio)
    except Exception as exc:
        configured = []
        reasons.append({"code": "PORTFOLIO_INVALID", "message": str(exc)})
    live = _risk_records(snapshot)
    configured_map = {row["position_key"]: row for row in configured}
    live_map = {row["position_key"]: row for row in live}
    positions_match = bool(configured_map) and configured_map.keys() == live_map.keys()
    if positions_match:
        for key, configured_row in configured_map.items():
            live_row = live_map[key]
            quantity = _finite(live_row.get("quantity"))
            configured_quantity = _finite(configured_row.get("quantity"))
            configured_available = _finite(configured_row.get("available"))
            price = _finite(live_row.get("price"))
            available = _finite(live_row.get("available"))
            if (
                quantity is None
                or configured_quantity is None
                or abs(quantity - configured_quantity) > EPSILON
                or configured_available is None
                or available is None
                or available < 0
                or abs(available - configured_available) > EPSILON
                or price is None
                or price <= 0
                or str(live_row.get("quote_status") or "").lower() != "live"
            ):
                positions_match = False
                break
            market_value = _finite(live_row.get("market_value_cny"))
            implied_value = quantity * price
            if market_value is None or abs(market_value - implied_value) > max(1.0, implied_value * 0.001):
                positions_match = False
                break
    if not positions_match:
        reasons.append(
            {
                "code": "POSITION_RECONCILIATION_FAILED",
                "message": "风险快照与账户配置的标的、数量、可用量或实时价格无法逐项勾稽",
            }
        )

    configured_cash = sum(_nonnegative(row.get("cash")) for row in portfolio.get("accounts") or [] if isinstance(row, dict))
    configured_debt = sum(_nonnegative(row.get("debt")) for row in portfolio.get("accounts") or [] if isinstance(row, dict))
    risk_cash = _finite(metrics.get("cash_cny"))
    risk_debt = _finite(metrics.get("debt_cny"))
    risk_market_value = _finite(metrics.get("gross_market_value_cny"))
    risk_nav = _finite(metrics.get("nav_cny"))
    live_market_value = sum(_nonnegative(row.get("market_value_cny")) for row in live)
    financials_match = all(
        (
            risk_cash is not None,
            risk_debt is not None,
            risk_market_value is not None,
            risk_nav is not None,
        )
    )
    if financials_match:
        tolerance = max(1.0, abs(float(risk_nav)) * 0.00001)
        financials_match = bool(
            abs(float(risk_cash) - configured_cash) <= tolerance
            and abs(float(risk_debt) - configured_debt) <= tolerance
            and abs(float(risk_market_value) - live_market_value) <= tolerance
            and abs(float(risk_nav) - (live_market_value + configured_cash - configured_debt)) <= tolerance
        )
    if not financials_match:
        reasons.append(
            {
                "code": "ACCOUNT_FINANCIAL_RECONCILIATION_FAILED",
                "message": "风险快照的现金、负债、市值与净资产无法和 portfolio.json 勾稽",
            }
        )

    verified = not reasons
    return {
        "verified": verified,
        "account_snapshot_verified": account_verified,
        "snapshot_dates_match": as_of_matches,
        "quotes_verified": quotes_verified,
        "positions_reconciled": positions_match,
        "account_financials_reconciled": financials_match,
        "risk_cache_current": cache.get("stale") is not True,
        "risk_input_fingerprint_verified": bool(
            re.fullmatch(r"[0-9a-f]{64}", input_fingerprint)
        ),
        "quote_age_locally_verified": bool(
            quotes.get("feed_hard_stale") is False
            and quote_age is not None
            and 0 <= quote_age <= phase_stale_seconds
        ),
        "quote_market_phase": market_phase,
        "quote_age_limit_seconds": _round(phase_stale_seconds, 2),
        "risk_snapshot_age_seconds": _round(risk_age, 2),
        "upstream_state": state or "UNKNOWN",
        "upstream_risk_state": str(snapshot.get("risk_state") or "UNKNOWN"),
        "reasons": reasons,
        "required_actions": (
            []
            if verified
            else [
                "重新导出两个账户的当前持仓、可用数量、现金和负债",
                "刷新全部持仓行情并重新生成风险快照",
                "确认门禁全部通过后再计算股数和订单分步",
            ]
        ),
    }


def _buy_data_gate(
    snapshot: dict[str, Any],
    aggregate_missing: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Buying is stricter than risk-reducing sells."""
    reasons: list[dict[str, str]] = []
    quality = snapshot.get("data_quality") if isinstance(snapshot.get("data_quality"), dict) else {}
    history_quality = quality.get("history") if isinstance(quality.get("history"), dict) else {}
    history = snapshot.get("history") if isinstance(snapshot.get("history"), dict) else {}
    windows = history.get("windows") if isinstance(history.get("windows"), dict) else {}
    quality_policy = policy.get("data_quality") if isinstance(policy.get("data_quality"), dict) else {}

    if quality.get("status") != "VERIFIED":
        reasons.append(
            {
                "code": "DATA_QUALITY_NOT_VERIFIED",
                "message": "新增买入要求 data_quality.status=VERIFIED",
            }
        )
    if history_quality.get("status") != "FULL":
        reasons.append(
            {
                "code": "HISTORY_LAYER_NOT_FULL",
                "message": "新增买入要求历史风险层状态为 FULL",
            }
        )
    required_windows = (
        "volatility",
        "es_97_5",
        "drawdown",
        "correlation_and_risk_contribution",
    )
    incomplete_windows: list[str] = []
    for name in required_windows:
        value = windows.get(name) if isinstance(windows.get(name), dict) else {}
        configured = _integer(value.get("configured_sessions"), -1)
        actual = _integer(value.get("actual_sessions"), -1)
        if value.get("sufficient") is not True or configured <= 0 or actual < configured:
            incomplete_windows.append(name)
    if incomplete_windows:
        reasons.append(
            {
                "code": "HISTORY_WINDOWS_INCOMPLETE",
                "message": "历史窗口不足：" + ", ".join(incomplete_windows),
            }
        )
    if aggregate_missing:
        reasons.append(
            {
                "code": "AGGREGATE_RISK_METRICS_MISSING",
                "message": "组合风险指标缺失：" + ", ".join(aggregate_missing),
            }
        )
    quote_age = _finite((quality.get("quotes") or {}).get("quote_age_seconds"))
    live_ttl = _nonnegative(quality_policy.get("live_quote_ttl_seconds"), 60.0)
    if quote_age is None or quote_age < 0 or quote_age > live_ttl:
        reasons.append(
            {
                "code": "QUOTE_NOT_LIVE_FOR_BUY",
                "message": "新增买入要求行情年龄不超过 live_quote_ttl_seconds",
            }
        )
    return {"verified": not reasons, "reasons": reasons}


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _input_binding(
    portfolio: dict[str, Any],
    policy: dict[str, Any],
    snapshot: dict[str, Any],
    liquidity_payload: dict[str, Any] | None,
    event_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "rebalance_schema_version": SCHEMA_VERSION,
        "risk_contract": "portfolio-risk-snapshot.v1-required-fields",
        "policy_version": policy.get("policy_version"),
        "portfolio_id": portfolio.get("portfolio_id"),
        "portfolio_as_of": portfolio.get("as_of"),
        "risk_generated_at": snapshot.get("generated_at"),
        "risk_input_fingerprint": (snapshot.get("cache") or {}).get("input_fingerprint"),
        "portfolio_sha256": _canonical_hash(portfolio),
        "policy_sha256": _canonical_hash(policy),
        "risk_snapshot_sha256": _canonical_hash(snapshot),
        "liquidity_sha256": _canonical_hash(liquidity_payload) if liquidity_payload is not None else None,
        "events_sha256": _canonical_hash(event_payload) if event_payload is not None else None,
    }


def _severity(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number >= 4:
            return "critical"
        if number >= 3:
            return "high"
        if number >= 2:
            return "medium"
        if number > 0:
            return "low"
        return "none"
    text = str(value or "none").strip().lower()
    aliases = {"严重": "critical", "高": "high", "中": "medium", "低": "low"}
    text = aliases.get(text, text)
    return text if text in SEVERITY_RANK else "medium"


def _payload_stale(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return True
    if payload.get("stale") is True:
        return True
    status = str(payload.get("status") or "").lower()
    return status in {"stale", "expired", "error", "failed"}


def _event_payload_gate(
    payload: dict[str, Any] | None,
    portfolio: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Validate the event-monitor envelope before treating silence as evidence.

    An empty or old payload is not evidence that no event exists.  The event
    monitor has its own input-quality checks, so its binding fields and an
    explicit, empty ``input_errors`` list are part of this contract.
    """
    reasons: list[dict[str, str]] = []
    payload = payload if isinstance(payload, dict) else None
    if not payload:
        reasons.append(
            {
                "code": "EVENT_PAYLOAD_MISSING",
                "message": "事件监控载荷缺失或为空，不能据此认定无事件风险",
            }
        )
        return {
            "verified": False,
            "age_seconds": None,
            "reasons": reasons,
        }

    if _integer(payload.get("schema_version"), -1) != 1:
        reasons.append(
            {
                "code": "EVENT_SCHEMA_MISMATCH",
                "message": "事件监控载荷缺失或不支持 schema_version=1",
            }
        )
    portfolio_id = str(portfolio.get("portfolio_id") or "")
    payload_portfolio_id = str(payload.get("portfolio_id") or "")
    if not portfolio_id or payload_portfolio_id != portfolio_id:
        reasons.append(
            {
                "code": "EVENT_PORTFOLIO_MISMATCH",
                "message": "事件监控载荷未绑定到当前 portfolio_id",
            }
        )
    policy_version = str(policy.get("policy_version") or "")
    payload_policy_version = str(payload.get("policy_version") or "")
    if not policy_version or payload_policy_version != policy_version:
        reasons.append(
            {
                "code": "EVENT_POLICY_MISMATCH",
                "message": "事件监控载荷未绑定到当前 policy_version",
            }
        )
    if not str(payload.get("risk_state") or "").strip():
        reasons.append(
            {
                "code": "EVENT_RISK_STATE_MISSING",
                "message": "事件监控载荷缺少非空 risk_state",
            }
        )

    input_errors = payload.get("input_errors")
    if not isinstance(input_errors, list):
        reasons.append(
            {
                "code": "EVENT_INPUT_ERRORS_UNPROVEN",
                "message": "事件监控载荷未显式证明输入无错误",
            }
        )
    elif input_errors:
        reasons.append(
            {
                "code": "EVENT_INPUT_ERRORS_PRESENT",
                "message": "事件监控仍有输入错误，事件静默不可采信",
            }
        )

    has_event_evidence = any(
        isinstance(payload.get(key), list) for key in ("alerts", "events", "items")
    ) or isinstance(payload.get("by_symbol"), dict)
    if not has_event_evidence:
        reasons.append(
            {
                "code": "EVENT_EVIDENCE_CONTAINER_MISSING",
                "message": "事件监控载荷没有 alerts/events/items/by_symbol 证据容器",
            }
        )

    generated = _parse_datetime(payload.get("generated_at"))
    age_seconds = (now - generated).total_seconds() if generated else None
    quality_policy = policy.get("data_quality") if isinstance(policy.get("data_quality"), dict) else {}
    maximum_age = _finite(quality_policy.get("event_payload_max_age_seconds"))
    if maximum_age is None or maximum_age <= 0:
        maximum_age = DEFAULT_EVENT_PAYLOAD_MAX_AGE_SECONDS
    if (
        _payload_stale(payload)
        or age_seconds is None
        or age_seconds < -60
        or age_seconds > maximum_age
    ):
        reasons.append(
            {
                "code": "EVENT_PAYLOAD_STALE",
                "message": "事件监控载荷缺少有效生成时间、来自未来或已超过时效",
            }
        )

    return {
        "verified": not reasons,
        "age_seconds": _round(age_seconds, 2),
        "maximum_age_seconds": _round(maximum_age, 2),
        "risk_state": str(payload.get("risk_state") or "UNKNOWN"),
        "reasons": reasons,
    }


def _liquidity_payload_gate(
    payload: dict[str, Any] | None,
    portfolio: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Require the complete builder contract before trusting any ADV value.

    ADV directly controls executable share counts, so a merely fresh-looking
    envelope is insufficient.  Recompute every hash link locally and reconcile
    the exact unique-symbol universe and twenty-session window to the current
    portfolio.  Any ambiguity invalidates the entire payload.
    """
    reasons: list[dict[str, str]] = []
    payload = payload if isinstance(payload, dict) else None
    if not payload:
        reasons.append(
            {
                "code": "LIQUIDITY_PAYLOAD_MISSING",
                "message": "成交容量载荷缺失或为空，不能计算可执行股数",
            }
        )
        return {"verified": False, "age_seconds": None, "reasons": reasons}

    def reject(code: str, message: str) -> None:
        reasons.append({"code": code, "message": message})

    def exact_integer(value: Any, expected: int) -> bool:
        number = _finite(value)
        return bool(
            not isinstance(value, bool)
            and number is not None
            and abs(number - expected) <= EPSILON
        )

    if not exact_integer(payload.get("schema_version"), 1):
        reasons.append(
            {
                "code": "LIQUIDITY_SCHEMA_MISMATCH",
                "message": "成交容量载荷缺失或不支持 schema_version=1",
            }
        )
    if payload.get("payload_type") != LIQUIDITY_PAYLOAD_TYPE:
        reject(
            "LIQUIDITY_PAYLOAD_TYPE_MISMATCH",
            f"成交容量载荷必须声明 payload_type={LIQUIDITY_PAYLOAD_TYPE}",
        )
    if payload.get("ok") is not True:
        reject("LIQUIDITY_NOT_OK", "成交容量载荷必须显式报告 ok=true")
    if payload.get("status") != "fresh":
        reject("LIQUIDITY_STATUS_NOT_FRESH", "成交容量载荷 status 必须严格为 fresh")
    if payload.get("stale") is not False:
        reject("LIQUIDITY_STALE_FLAG_INVALID", "成交容量载荷必须显式报告 stale=false")

    portfolio_id = str(portfolio.get("portfolio_id") or "").strip()
    policy_version = str(policy.get("policy_version") or "").strip()
    portfolio_as_of = str(portfolio.get("as_of") or "").strip()
    try:
        date.fromisoformat(portfolio_as_of)
        portfolio_date_valid = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", portfolio_as_of))
    except ValueError:
        portfolio_date_valid = False
    if not portfolio_id or str(payload.get("portfolio_id") or "") != portfolio_id:
        reject(
            "LIQUIDITY_PORTFOLIO_MISMATCH",
            "成交容量载荷未绑定到当前 portfolio_id",
        )
    if not policy_version or str(payload.get("policy_version") or "") != policy_version:
        reject(
            "LIQUIDITY_POLICY_MISMATCH",
            "成交容量载荷未绑定到当前 policy_version",
        )
    if (
        not portfolio_date_valid
        or payload.get("portfolio_as_of") != portfolio_as_of
        or payload.get("data_as_of") != portfolio_as_of
    ):
        reject(
            "LIQUIDITY_AS_OF_MISMATCH",
            "portfolio_as_of、data_as_of 必须同时等于当前 portfolio.as_of",
        )

    input_errors = payload.get("input_errors")
    if not isinstance(input_errors, list) or input_errors:
        reject(
            "LIQUIDITY_INPUT_ERRORS",
            "成交容量载荷未显式证明输入无错误",
        )

    # Reconcile the builder's unique-symbol universe without requiring account
    # quantities; the standalone builder accepts identity-only portfolios too.
    expected_symbols: set[str] = set()
    expected_instruments: dict[str, str] = {}
    portfolio_position_count = 0
    inventory_valid = True
    accounts = portfolio.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        inventory_valid = False
    else:
        for account in accounts:
            raw_positions = account.get("positions") if isinstance(account, dict) else None
            if not isinstance(raw_positions, list):
                inventory_valid = False
                continue
            for position in raw_positions:
                portfolio_position_count += 1
                if not isinstance(position, dict):
                    inventory_valid = False
                    continue
                raw_symbol = str(position.get("thscode") or "").strip().upper()
                symbol = _normalise_thscode(raw_symbol)
                instrument = str(position.get("instrument_type") or "").strip().lower()
                if (
                    raw_symbol != symbol
                    or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol)
                    or instrument not in {"stock", "etf"}
                    or (
                        symbol in expected_instruments
                        and expected_instruments[symbol] != instrument
                    )
                ):
                    inventory_valid = False
                    continue
                expected_symbols.add(symbol)
                expected_instruments[symbol] = instrument
    if not inventory_valid or not expected_symbols:
        reject(
            "LIQUIDITY_PORTFOLIO_UNIVERSE_INVALID",
            "当前 portfolio 无法形成唯一且有效的股票代码全集",
        )

    binding = payload.get("input_binding")
    binding_valid = isinstance(binding, dict)
    if not binding_valid:
        reject("LIQUIDITY_INPUT_BINDING_MISSING", "成交容量载荷缺少 input_binding")
        binding = {}
    expected_portfolio_hash = _canonical_hash(portfolio)
    expected_policy_hash = _canonical_hash(policy)
    if binding.get("portfolio_sha256") != expected_portfolio_hash:
        reject(
            "LIQUIDITY_PORTFOLIO_HASH_MISMATCH",
            "input_binding.portfolio_sha256 与当前 portfolio 不一致",
        )
    if binding.get("policy_sha256") != expected_policy_hash:
        reject(
            "LIQUIDITY_POLICY_HASH_MISMATCH",
            "input_binding.policy_sha256 与当前 policy 不一致",
        )
    if (
        binding.get("portfolio_id") != portfolio_id
        or binding.get("policy_version") != policy_version
        or binding.get("portfolio_as_of") != portfolio_as_of
        or binding.get("data_as_of") != portfolio_as_of
        or binding.get("generated_at") != payload.get("generated_at")
    ):
        reject(
            "LIQUIDITY_BINDING_FIELDS_MISMATCH",
            "input_binding 的身份、日期或生成时间与载荷/当前输入不一致",
        )
    supplied_binding_hash = binding.get("binding_sha256")
    binding_material = copy.deepcopy(binding)
    binding_material.pop("binding_sha256", None)
    if (
        not isinstance(supplied_binding_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_binding_hash)
        or supplied_binding_hash != _canonical_hash(binding_material)
    ):
        reject(
            "LIQUIDITY_BINDING_HASH_MISMATCH",
            "input_binding.binding_sha256 未通过本地重算",
        )

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        reject("LIQUIDITY_SOURCE_EVIDENCE_MISSING", "成交容量载荷缺少 source evidence")
        evidence = {}
    supplied_evidence_hash = evidence.get("evidence_sha256")
    evidence_material = copy.deepcopy(evidence)
    evidence_material.pop("evidence_sha256", None)
    evidence_hash_valid = bool(
        isinstance(supplied_evidence_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", supplied_evidence_hash)
        and supplied_evidence_hash == _canonical_hash(evidence_material)
    )
    if not evidence_hash_valid:
        reject(
            "LIQUIDITY_SOURCE_EVIDENCE_HASH_MISMATCH",
            "evidence_sha256 未通过本地重算",
        )
    if binding.get("source_evidence_sha256") != supplied_evidence_hash:
        reject(
            "LIQUIDITY_SOURCE_EVIDENCE_BINDING_MISMATCH",
            "input_binding 未绑定到当前 source evidence 哈希",
        )

    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        reject("LIQUIDITY_COVERAGE_MISSING", "成交容量载荷缺少 coverage")
        coverage = {}
    unique_count = len(expected_symbols)
    coverage_ratio = _finite(coverage.get("coverage_ratio"))
    coverage_valid = bool(
        coverage.get("complete") is True
        and exact_integer(coverage.get("portfolio_position_count"), portfolio_position_count)
        and exact_integer(coverage.get("required_symbol_count"), unique_count)
        and exact_integer(coverage.get("valid_symbol_count"), unique_count)
        and coverage_ratio is not None
        and abs(coverage_ratio - 1.0) <= EPSILON
        and coverage.get("missing_or_invalid_symbols") == []
        and exact_integer(
            coverage.get("required_session_count"), LIQUIDITY_LOOKBACK_SESSIONS
        )
        and exact_integer(
            coverage.get("common_session_count"), LIQUIDITY_LOOKBACK_SESSIONS
        )
    )
    if not coverage_valid:
        reject(
            "LIQUIDITY_COVERAGE_INCOMPLETE",
            "coverage 未证明当前持仓 unique symbol 全覆盖且 20 日证据完整",
        )

    data_window = payload.get("data_window")
    if not isinstance(data_window, dict):
        reject("LIQUIDITY_WINDOW_MISSING", "成交容量载荷缺少 data_window")
        data_window = {}
    sessions = data_window.get("sessions")
    session_dates: list[date] = []
    sessions_valid = isinstance(sessions, list) and len(sessions) == LIQUIDITY_LOOKBACK_SESSIONS
    if sessions_valid:
        for session in sessions:
            try:
                parsed_session = date.fromisoformat(str(session))
            except ValueError:
                sessions_valid = False
                break
            if str(session) != parsed_session.isoformat():
                sessions_valid = False
                break
            session_dates.append(parsed_session)
        sessions_valid = bool(
            sessions_valid
            and len(set(session_dates)) == LIQUIDITY_LOOKBACK_SESSIONS
            and session_dates == sorted(session_dates)
            and all(
                earlier < later
                for earlier, later in zip(session_dates, session_dates[1:])
            )
        )
    window_start = sessions[0] if sessions_valid else None
    window_end = sessions[-1] if sessions_valid else None
    window_valid = bool(
        exact_integer(payload.get("lookback_sessions"), LIQUIDITY_LOOKBACK_SESSIONS)
        and exact_integer(data_window.get("session_count"), LIQUIDITY_LOOKBACK_SESSIONS)
        and data_window.get("start") == window_start
        and data_window.get("end") == window_end == portfolio_as_of
        and data_window.get("cutoff_inclusive") is True
        and data_window.get("future_data_used") is False
        and binding.get("window_start") == window_start
        and binding.get("window_end") == window_end
        and exact_integer(binding.get("lookback_sessions"), LIQUIDITY_LOOKBACK_SESSIONS)
        and binding.get("session_calendar_sha256")
        == (_canonical_hash(sessions) if sessions_valid else None)
    )
    if not sessions_valid or not window_valid:
        reject(
            "LIQUIDITY_WINDOW_INVALID",
            "成交容量载荷未证明截至 portfolio.as_of 的同一组完整 20 个交易日",
        )

    records = payload.get("positions")
    records_valid = isinstance(records, list) and len(records) == unique_count
    observed_symbols: set[str] = set()
    if isinstance(records, list):
        for row in records:
            if not isinstance(row, dict):
                records_valid = False
                continue
            raw_symbol = str(row.get("thscode") or "").strip().upper()
            symbol = _normalise_thscode(raw_symbol)
            if raw_symbol != symbol or symbol in observed_symbols:
                records_valid = False
            observed_symbols.add(symbol)
            adv_values = [
                _finite(row.get(key))
                for key in ("adv_cny", "average_daily_turnover_cny", "adv20_cny")
            ]
            positive_consistent_adv = bool(
                all(value is not None and value > 0 for value in adv_values)
                and max(float(value) for value in adv_values if value is not None)
                - min(float(value) for value in adv_values if value is not None)
                <= max(0.01, float(adv_values[0] or 0.0) * 1e-9)
            )
            if (
                symbol not in expected_symbols
                or row.get("instrument_type") != expected_instruments.get(symbol)
                or not positive_consistent_adv
                or not exact_integer(
                    row.get("window_session_count"), LIQUIDITY_LOOKBACK_SESSIONS
                )
                or not exact_integer(
                    row.get("observed_session_count"), LIQUIDITY_LOOKBACK_SESSIONS
                )
                or row.get("window_start") != window_start
                or row.get("window_end") != window_end
                or row.get("calculation")
                != "arithmetic_mean_of_exact_common_20_sessions"
                or not isinstance(row.get("source"), dict)
                or not row.get("source")
            ):
                records_valid = False
    if not records_valid or observed_symbols != expected_symbols:
        reject(
            "LIQUIDITY_POSITION_EVIDENCE_INVALID",
            "positions 必须与持仓 unique symbol 全集一一对应，并各自提供唯一正 ADV 与完整 20 日窗口",
        )

    generated = _parse_datetime(payload.get("generated_at"))
    age_seconds = (now - generated).total_seconds() if generated else None
    quality_policy = policy.get("data_quality") if isinstance(policy.get("data_quality"), dict) else {}
    maximum_age = _finite(quality_policy.get("liquidity_payload_max_age_seconds"))
    if maximum_age is None or maximum_age <= 0:
        maximum_age = DEFAULT_LIQUIDITY_PAYLOAD_MAX_AGE_SECONDS
    if (
        age_seconds is None
        or age_seconds < -60
        or age_seconds > maximum_age
    ):
        reasons.append(
            {
                "code": "LIQUIDITY_PAYLOAD_STALE",
                "message": "成交容量载荷缺少有效生成时间、来自未来或已超过时效",
            }
        )
    return {
        "verified": not reasons,
        "age_seconds": _round(age_seconds, 2),
        "maximum_age_seconds": _round(maximum_age, 2),
        "reasons": reasons,
    }


def _event_assessments(
    positions: Iterable[dict[str, Any]],
    payload: dict[str, Any] | None,
    payload_verified: bool,
) -> dict[str, dict[str, Any]]:
    known = bool(payload_verified and payload is not None)
    assessments: dict[str, dict[str, Any]] = {
        row["position_key"]: {
            "known": known,
            "severity": "none" if known else "unknown",
            "categories": [],
            "blocking": False,
            "event_ids": [],
        }
        for row in positions
    }
    if not known or payload is None:
        return assessments

    records: list[dict[str, Any]] = []
    for key in ("alerts", "events", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            records.extend(row for row in value if isinstance(row, dict))
    by_symbol = payload.get("by_symbol")
    if isinstance(by_symbol, dict):
        for symbol, value in by_symbol.items():
            if isinstance(value, dict):
                # A by-symbol container is itself explicit symbol-level scope.
                records.append({"scope": "symbol", "thscode": symbol, **value})

    by_code: dict[str, list[str]] = {}
    for position in positions:
        by_code.setdefault(position["thscode"], []).append(position["position_key"])

    for event in records:
        scope = str(event.get("scope") or "").strip().lower()
        has_direct_symbol = any(
            str(event.get(key) or "").strip()
            for key in ("thscode", "symbol", "code")
        )
        # Account, portfolio, macro and data-quality alerts may intentionally
        # name every holding so humans can see their impact.  That does not make
        # them issuer events, and projecting them onto every security would
        # falsely label each company as having HIGH_EVENT_RISK.  An absent scope
        # is retained only for legacy records carrying a direct symbol field;
        # affected_symbols alone is not proof of security-level semantics.
        if (scope and scope not in SECURITY_EVENT_SCOPES) or (
            not scope and not has_direct_symbol
        ):
            continue
        raw_symbols: list[Any] = [
            event.get("thscode") or event.get("symbol") or event.get("code")
        ]
        affected = event.get("affected_symbols")
        if isinstance(affected, (list, tuple, set)):
            raw_symbols.extend(affected)
        symbols = {
            _normalise_thscode(value)
            for value in raw_symbols
            if str(value or "").strip()
        }
        keys = sorted(
            {
                position_key
                for symbol in symbols
                for position_key in by_code.get(symbol, [])
            }
        )
        if not keys:
            continue
        level = _severity(event.get("severity", event.get("level")))
        category = str(event.get("category") or event.get("type") or "").strip()
        title = str(event.get("title") or event.get("message") or "")
        blocking = bool(event.get("trading_blocked")) or any(
            term in f"{category} {title}".lower() for term in BLOCKING_EVENT_TERMS
        )
        for key in keys:
            target = assessments[key]
            current_level = target["severity"]
            if current_level == "unknown" or SEVERITY_RANK[level] > SEVERITY_RANK[current_level]:
                target["severity"] = level
            if category and category not in target["categories"]:
                target["categories"].append(category)
            target["blocking"] = target["blocking"] or blocking
            identifier = str(event.get("id") or event.get("event_id") or "")
            if identifier and identifier not in target["event_ids"]:
                target["event_ids"].append(identifier)
    return assessments


def _liquidity_assessments(
    positions: Iterable[dict[str, Any]],
    payload: dict[str, Any] | None,
    participation_pct: float,
    horizon_days: float,
    payload_verified: bool,
) -> dict[str, dict[str, Any]]:
    known_payload = bool(payload_verified and payload is not None)
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    if known_payload and payload is not None:
        by_symbol = payload.get("by_symbol")
        if isinstance(by_symbol, dict):
            for symbol, value in by_symbol.items():
                if isinstance(value, dict):
                    raw_by_symbol[_normalise_thscode(symbol)] = value
        for key in ("positions", "items", "liquidity"):
            value = payload.get(key)
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        symbol = _normalise_thscode(
                            row.get("thscode") or row.get("symbol") or row.get("code")
                        )
                        raw_by_symbol[symbol] = row

    result: dict[str, dict[str, Any]] = {}
    for position in positions:
        row = raw_by_symbol.get(position["thscode"], {})
        adv = None
        for key in (
            "adv_cny",
            "average_daily_turnover_cny",
            "average_amount_cny",
            "adv20_cny",
            "turnover_cny",
        ):
            adv = _finite(row.get(key))
            if adv is not None and adv > 0:
                break
        if (adv is None or adv <= 0) and _finite(row.get("adv_yi")) is not None:
            adv = float(_finite(row.get("adv_yi"), 0.0) or 0.0) * 100_000_000
        known = known_payload and adv is not None and adv > 0
        daily_cap_cny = float(adv) * participation_pct / 100 if known else None
        market_value = _nonnegative(position.get("market_value_cny"))
        exit_days = market_value / daily_cap_cny if daily_cap_cny and daily_cap_cny > 0 else None
        result[position["position_key"]] = {
            "known": known,
            "adv_cny": _round(adv, 2),
            "daily_cap_cny": _round(daily_cap_cny, 2),
            "exit_days_at_limit": _round(exit_days, 3),
            "within_horizon": bool(known and exit_days is not None and exit_days <= horizon_days),
        }
    return result


def _aggregate_risk_scale(
    metrics: dict[str, Any], limits: dict[str, Any]
) -> tuple[float, list[dict[str, Any]], list[str]]:
    checks = (
        ("annualized_volatility_pct", "maximum_annualized_volatility_pct", "年化波动率"),
        ("daily_es_97_5_pct", "maximum_daily_es_97_5_pct", "日度 ES97.5"),
        ("worst_5d_stress_loss_pct", "maximum_5d_stress_loss_pct", "五日压力损失"),
        ("static_weight_max_drawdown_pct", "maximum_drawdown_pct", "最大回撤代理"),
    )
    scale = 1.0
    evidence: list[dict[str, Any]] = []
    missing: list[str] = []
    for metric_key, limit_key, label in checks:
        actual = _finite(metrics.get(metric_key))
        limit = _finite(limits.get(limit_key))
        if limit is None or limit <= 0:
            missing.append(limit_key)
            evidence.append(
                {"metric": metric_key, "label": label, "actual": _round(actual), "limit": None, "scale": None}
            )
            scale = min(scale, UNKNOWN_AGGREGATE_RISK_SCALE)
            continue
        if actual is None:
            missing.append(metric_key)
            evidence.append(
                {"metric": metric_key, "label": label, "actual": None, "limit": limit, "scale": None}
            )
            scale = min(scale, UNKNOWN_AGGREGATE_RISK_SCALE)
            continue
        ratio = 1.0 if actual <= limit or actual <= 0 else max(0.0, min(1.0, limit / actual))
        scale = min(scale, ratio)
        evidence.append(
            {
                "metric": metric_key,
                "label": label,
                "actual": _round(actual),
                "limit": _round(limit),
                "scale": _round(ratio),
            }
        )
    return max(0.0, min(1.0, scale)), evidence, missing


def _structural_position_cap(limits: dict[str, Any], count: int) -> float:
    single = _nonnegative(limits.get("maximum_single_position_pct"), 8.0)
    top5 = _nonnegative(limits.get("maximum_top5_pct"), 35.0)
    divisor = max(1, min(5, count))
    return min(single, top5 / divisor)


def _priority_and_models(
    positions: list[dict[str, Any]],
    accounts: dict[str, dict[str, Any]],
    limits: dict[str, Any],
    liquidity: dict[str, dict[str, Any]],
    events: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    symbol_totals: dict[str, float] = {}
    for row in positions:
        symbol_totals[row["thscode"]] = symbol_totals.get(row["thscode"], 0.0) + _nonnegative(
            row.get("weight_pct")
        )
    structural_cap = _structural_position_cap(limits, len(symbol_totals))
    max_vol = _nonnegative(limits.get("maximum_annualized_volatility_pct"), 18.0)
    max_theme = _nonnegative(limits.get("maximum_theme_pct"), 50.0)
    theme_totals: dict[str, float] = {}
    for row in positions:
        theme_totals[str(row.get("theme") or "unknown")] = theme_totals.get(
            str(row.get("theme") or "unknown"), 0.0
        ) + _nonnegative(row.get("weight_pct"))

    models: list[dict[str, Any]] = []
    for row in positions:
        key = row["position_key"]
        weight = _nonnegative(row.get("weight_pct"))
        aggregate_symbol_weight = symbol_totals.get(row["thscode"], weight)
        volatility = _finite(row.get("annualized_volatility_pct"))
        contribution = _finite(row.get("risk_contribution_pct"))
        theme = str(row.get("theme") or "unknown")
        event = events[key]
        liquid = liquidity[key]
        flags: list[str] = []
        reasons: list[str] = []

        score = aggregate_symbol_weight / max(structural_cap, EPSILON)
        if aggregate_symbol_weight > structural_cap + EPSILON:
            flags.append("POSITION_OR_TOP5_CAP")
            reasons.append("单票或前五大集中度约束要求优先复核")
            score += 1.5
        if theme_totals.get(theme, 0.0) > max_theme + EPSILON:
            flags.append("THEME_CONCENTRATION")
            reasons.append("所属主题超过组合上限")
            score += min(3.0, theme_totals[theme] / max(max_theme, EPSILON))

        if volatility is None:
            flags.append("VOLATILITY_UNKNOWN")
            reasons.append("波动率缺失，按未知高风险处理")
            vol_ratio = 1.75
            score += 1.75
        else:
            vol_ratio = max(0.25, volatility / max(max_vol, EPSILON))
            if volatility > max_vol:
                flags.append("VOLATILITY_LIMIT")
                reasons.append("历史波动率超过策略上限")
                score += min(4.0, vol_ratio)

        if contribution is None:
            flags.append("RISK_CONTRIBUTION_UNKNOWN")
            reasons.append("风险贡献缺失，不能按零风险处理")
            contribution_ratio = 1.5
            score += 1.25
        else:
            contribution_ratio = contribution / max(weight, 0.25)
            if contribution_ratio > 1.25:
                flags.append("OUTSIZED_RISK_CONTRIBUTION")
                reasons.append("风险贡献高于持仓权重")
                score += min(3.0, contribution_ratio)

        if not liquid["known"]:
            flags.append("LIQUIDITY_UNKNOWN")
            reasons.append("成交容量缺失，不能生成该标的可执行股数")
            liquidity_ratio = 1.5
            score += 1.0
        else:
            exit_days = _finite(liquid.get("exit_days_at_limit"), 0.0) or 0.0
            liquidity_ratio = max(0.5, exit_days / 3.0)
            if not liquid["within_horizon"]:
                flags.append("LIQUIDITY_HORIZON")
                reasons.append("按成交额参与上限退出所需天数过长")
                score += min(3.0, liquidity_ratio)

        event_level = str(event.get("severity") or "unknown")
        if not event["known"]:
            flags.append("EVENT_DATA_UNKNOWN")
            reasons.append("事件风险数据缺失或陈旧")
            event_ratio = 1.5
            score += 1.25
        else:
            event_ratio = 1.0 + 0.35 * SEVERITY_RANK.get(event_level, 2)
            if SEVERITY_RANK.get(event_level, 0) >= SEVERITY_RANK["high"]:
                flags.append("HIGH_EVENT_RISK")
                reasons.append("存在高等级公司事件或监管风险")
                score += SEVERITY_RANK[event_level]
            if event.get("blocking"):
                flags.append("TRADING_BLOCKED_EVENT")
                reasons.append("事件可能阻断交易，必须人工核验交易状态")
                score += 5.0

        account = accounts.get(row["account_id"], {})
        if _nonnegative(account.get("debt_cny")) > 0:
            flags.append("DEBT_ACCOUNT")
            reasons.append("该账户有负债，降低敞口所得应优先还债")
            score += 1.0

        cap = structural_cap
        cap *= max(0.40, min(1.0, 1.0 / max(vol_ratio, 1.0)))
        cap *= EVENT_CAP_FACTOR.get(event_level, 0.60) if event["known"] else 0.60
        if not liquid["known"]:
            cap *= 0.75
        elif not liquid["within_horizon"]:
            exit_days = _finite(liquid.get("exit_days_at_limit"), 999.0) or 999.0
            cap *= max(0.50, min(1.0, 3.0 / max(exit_days, EPSILON)))
        if contribution_ratio > 1.5:
            cap *= max(0.65, min(1.0, 1.5 / contribution_ratio))

        risk_factor = max(0.35, vol_ratio * contribution_ratio * liquidity_ratio * event_ratio)
        desirability = math.sqrt(max(weight, 0.25)) / risk_factor
        models.append(
            {
                **row,
                "aggregate_symbol_weight_pct": aggregate_symbol_weight,
                "structural_cap_pct": structural_cap,
                "risk_adjusted_cap_pct": max(0.0, min(structural_cap, cap)),
                "risk_factor": risk_factor,
                "desirability": max(desirability, 0.0001),
                "risk_score": score,
                "risk_flags": flags,
                "reasons": reasons or ["未触发额外风险加分"],
                "liquidity": liquid,
                "event": event,
                "data_complete_for_increase": (
                    volatility is not None
                    and contribution is not None
                    and liquid["known"]
                    and event["known"]
                    and SEVERITY_RANK.get(event_level, 2) < SEVERITY_RANK["high"]
                    and not event.get("blocking")
                ),
            }
        )

    ordered = sorted(models, key=lambda row: (-row["risk_score"], row["thscode"], row["account_id"]))
    priority = [
        {
            "rank": rank,
            "account_id": row["account_id"],
            "thscode": row["thscode"],
            "name": str(row.get("name") or row["thscode"]),
            "risk_score": _round(row["risk_score"], 4),
            "risk_flags": row["risk_flags"],
            "reasons": row["reasons"],
        }
        for rank, row in enumerate(ordered, 1)
    ]
    return priority, models


def _allocate_targets(
    models: list[dict[str, Any]], target_total_pct: float, theme_cap_pct: float
) -> tuple[dict[str, float], float]:
    weights = {row["position_key"]: 0.0 for row in models}
    symbol_weights: dict[str, float] = {}
    theme_weights: dict[str, float] = {}
    remaining = max(0.0, target_total_pct)

    for _ in range(200):
        if remaining <= 1e-7:
            break
        eligible = []
        for row in models:
            key = row["position_key"]
            theme = str(row.get("theme") or "unknown")
            position_room = row["target_cap_pct"] - weights[key]
            symbol_room = row["symbol_target_cap_pct"] - symbol_weights.get(row["thscode"], 0.0)
            theme_room = theme_cap_pct - theme_weights.get(theme, 0.0)
            if position_room > 1e-8 and symbol_room > 1e-8 and theme_room > 1e-8:
                eligible.append(row)
        if not eligible:
            break
        score_total = sum(row["desirability"] for row in eligible)
        proposals: dict[str, float] = {}
        for row in eligible:
            key = row["position_key"]
            proposals[key] = min(
                row["target_cap_pct"] - weights[key],
                remaining * row["desirability"] / max(score_total, EPSILON),
            )

        by_theme: dict[str, list[dict[str, Any]]] = {}
        for row in eligible:
            by_theme.setdefault(str(row.get("theme") or "unknown"), []).append(row)
        for theme, rows in by_theme.items():
            proposed = sum(proposals[row["position_key"]] for row in rows)
            room = max(0.0, theme_cap_pct - theme_weights.get(theme, 0.0))
            if proposed > room + EPSILON:
                factor = room / proposed if proposed > 0 else 0.0
                for row in rows:
                    proposals[row["position_key"]] *= factor

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in eligible:
            by_symbol.setdefault(row["thscode"], []).append(row)
        for symbol, rows in by_symbol.items():
            proposed = sum(proposals[row["position_key"]] for row in rows)
            symbol_cap = min(row["symbol_target_cap_pct"] for row in rows)
            room = max(0.0, symbol_cap - symbol_weights.get(symbol, 0.0))
            if proposed > room + EPSILON:
                factor = room / proposed if proposed > 0 else 0.0
                for row in rows:
                    proposals[row["position_key"]] *= factor

        applied = sum(proposals.values())
        if applied <= 1e-9:
            break
        for row in eligible:
            key = row["position_key"]
            amount = proposals[key]
            weights[key] += amount
            symbol_weights[row["thscode"]] = symbol_weights.get(row["thscode"], 0.0) + amount
            theme = str(row.get("theme") or "unknown")
            theme_weights[theme] = theme_weights.get(theme, 0.0) + amount
        remaining -= applied
    return weights, max(0.0, remaining)


def _whole_lots(shares: float, lot_size: int) -> int:
    if lot_size <= 0:
        raise ValueError("lot size must be positive")
    return max(0, int(math.floor(max(0.0, shares) / lot_size + EPSILON)) * lot_size)


def _outside_rebalance_band(
    adjustment_cny: float,
    nav: float,
    current_value: float,
    execution_policy: dict[str, Any],
) -> bool:
    gap_points = abs(adjustment_cny) / max(nav, EPSILON) * 100
    relative_gap = abs(adjustment_cny) / max(current_value, EPSILON) * 100
    minimum_points = _nonnegative(execution_policy.get("minimum_rebalance_gap_pct_points"), 1.0)
    minimum_relative = _nonnegative(execution_policy.get("minimum_relative_gap_pct"), 20.0)
    # Both policy fields are minimums: ignore a gap unless it is material both
    # to the whole portfolio and to the position being changed.
    return gap_points >= minimum_points and relative_gap >= minimum_relative


def _post_stage_one_estimate(
    models: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    nav: float,
    accounts: dict[str, dict[str, Any]],
    limits: dict[str, Any],
) -> dict[str, Any]:
    """Estimate state after only the generated stage-one sells have filled."""
    values = {
        row["position_key"]: _nonnegative(row.get("market_value_cny")) for row in models
    }
    cash = {
        account_id: _nonnegative(account.get("cash_cny"))
        for account_id, account in accounts.items()
    }
    debt = {
        account_id: _nonnegative(account.get("debt_cny"))
        for account_id, account in accounts.items()
    }
    stage_notional = 0.0
    for order in orders:
        if order.get("stage") != 1 or order.get("side") != "SELL":
            continue
        account_id = str(order.get("account_id") or "")
        key = _position_key(account_id, order.get("thscode"))
        proceeds = min(
            values.get(key, 0.0),
            _nonnegative(order.get("estimated_notional_cny")),
        )
        values[key] = max(0.0, values.get(key, 0.0) - proceeds)
        stage_notional += proceeds
        repayment = min(debt.get(account_id, 0.0), proceeds)
        debt[account_id] = max(0.0, debt.get(account_id, 0.0) - repayment)
        cash[account_id] = cash.get(account_id, 0.0) + proceeds - repayment

    securities: dict[str, float] = {}
    themes: dict[str, float] = {}
    model_by_key = {row["position_key"]: row for row in models}
    for key, value in values.items():
        row = model_by_key[key]
        securities[row["thscode"]] = securities.get(row["thscode"], 0.0) + value
        theme = str(row.get("theme") or "unknown")
        themes[theme] = themes.get(theme, 0.0) + value
    gross = sum(values.values())
    cash_total = sum(cash.values())
    debt_total = sum(debt.values())
    security_weights = sorted((value / nav * 100 for value in securities.values()), reverse=True)
    theme_weights = {theme: value / nav * 100 for theme, value in themes.items()}
    metrics = {
        "gross_exposure_pct": gross / nav * 100,
        "cash_pct": cash_total / nav * 100,
        "debt_pct": debt_total / nav * 100,
        "largest_position_pct": security_weights[0] if security_weights else 0.0,
        "top5_pct": sum(security_weights[:5]),
        "largest_theme_pct": max(theme_weights.values(), default=0.0),
    }
    checks = (
        ("GROSS_EXPOSURE", metrics["gross_exposure_pct"], _nonnegative(limits.get("max_gross_exposure_pct")), "maximum"),
        ("CASH_BUFFER", metrics["cash_pct"], _nonnegative(limits.get("minimum_cash_pct")), "minimum"),
        ("DEBT", metrics["debt_pct"], _nonnegative(limits.get("maximum_debt_pct")), "maximum"),
        ("SINGLE_POSITION", metrics["largest_position_pct"], _nonnegative(limits.get("maximum_single_position_pct")), "maximum"),
        ("TOP5", metrics["top5_pct"], _nonnegative(limits.get("maximum_top5_pct")), "maximum"),
        ("THEME", metrics["largest_theme_pct"], _nonnegative(limits.get("maximum_theme_pct")), "maximum"),
    )
    breaches: list[dict[str, Any]] = []
    for code, actual, limit, direction in checks:
        violated = actual + EPSILON < limit if direction == "minimum" else actual > limit + EPSILON
        if violated:
            breaches.append(
                {
                    "code": code,
                    "actual_pct": _round(actual),
                    "limit_pct": _round(limit),
                    "rule": direction,
                }
            )
    return {
        "basis": "STAGE_1_SELLS_ONLY_AT_REFERENCE_PRICE_BEFORE_FEES",
        "stage_1_sell_notional_cny": _round(stage_notional, 2),
        "gross_market_value_cny": _round(gross, 2),
        "cash_cny": _round(cash_total, 2),
        "debt_cny": _round(debt_total, 2),
        **{key: _round(value) for key, value in metrics.items()},
        "residual_breaches": breaches,
        "constraints_satisfied": not breaches,
        "assumption": "卖出所得仅在本账户内先还债，余款计入现金；成交后必须用券商数据重算。",
    }


def _build_execution(
    models: list[dict[str, Any]],
    target_weights: dict[str, float],
    nav: float,
    accounts: dict[str, dict[str, Any]],
    limits: dict[str, Any],
    execution_policy: dict[str, Any],
    buys_globally_allowed: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lot_size = max(1, _integer(execution_policy.get("a_share_lot_size"), 100))
    initial_fraction = _finite(execution_policy.get("initial_order_fraction_of_gap"), 1 / 3) or 1 / 3
    initial_fraction = max(0.0, min(1.0, initial_fraction))
    existing_fraction = _finite(
        execution_policy.get("existing_position_rebalance_fraction_of_gap"), 0.5
    ) or 0.5
    existing_fraction = max(0.0, min(1.0, existing_fraction))
    confirmation_closes = max(
        1, _integer(execution_policy.get("normal_signal_confirmation_closes"), 2)
    )
    participation = _nonnegative(execution_policy.get("maximum_adv_participation_pct_per_day"), 10.0)
    t_plus_one = execution_policy.get("t_plus_one", True) is not False

    adjustments: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in models:
        current = _nonnegative(row.get("market_value_cny"))
        target_weight = target_weights[row["position_key"]]
        target = nav * target_weight / 100
        gap = target - current
        outside = _outside_rebalance_band(gap, nav, current, execution_policy)
        direction = "HOLD"
        if outside and gap < -EPSILON:
            direction = "REDUCE"
        elif outside and gap > EPSILON:
            direction = "ADD"
        adjustment = {
            "account_id": row["account_id"],
            "thscode": row["thscode"],
            "name": str(row.get("name") or row["thscode"]),
            "theme": str(row.get("theme") or "unknown"),
            "current_weight_pct": _round(row.get("weight_pct")),
            "target_weight_pct": _round(target_weight),
            "target_value_cny": _round(target, 2),
            "adjustment_cny": _round(gap, 2),
            "direction": direction,
            "inside_rebalance_band": not outside,
            "reason_codes": row["risk_flags"],
        }
        adjustments.append(adjustment)
        if direction != "HOLD":
            candidates.append({**row, "target_weight_pct": target_weight, "adjustment_cny": gap})

    account_positions: dict[str, float] = {account_id: 0.0 for account_id in accounts}
    for row in models:
        account_positions[row["account_id"]] = account_positions.get(row["account_id"], 0.0) + _nonnegative(
            row.get("market_value_cny")
        )

    account_buy_budget: dict[str, float] = {}
    for account_id, account in accounts.items():
        account_nav = (
            account_positions.get(account_id, 0.0)
            + _nonnegative(account.get("cash_cny"))
            - _nonnegative(account.get("debt_cny"))
        )
        minimum_cash = max(0.0, account_nav) * _nonnegative(limits.get("minimum_cash_pct"), 10.0) / 100
        account_buy_budget[account_id] = max(
            0.0,
            _nonnegative(account.get("cash_cny"))
            - _nonnegative(account.get("debt_cny"))
            - minimum_cash,
        )

    orders: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    allocated_buy_budget: dict[str, float] = {account_id: 0.0 for account_id in accounts}

    for row in sorted(candidates, key=lambda item: (item["adjustment_cny"] > 0, -item["risk_score"])):
        side = "SELL" if row["adjustment_cny"] < 0 else "BUY"
        price = _finite(row.get("price"))
        if price is None or price <= 0:
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "实时价格缺失，未计算股数",
                }
            )
            continue
        if row["event"].get("blocking"):
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "存在停牌、退市或交易阻断事件，未计算股数",
                }
            )
            continue
        if not row["liquidity"].get("known"):
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "成交容量未知，未计算股数",
                }
            )
            continue
        if not row["event"].get("known"):
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "事件数据未知或陈旧，未计算股数",
                }
            )
            continue
        if side == "BUY" and (not buys_globally_allowed or not row["data_complete_for_increase"]):
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "新增买入门禁或该标的风险数据未通过，未计算股数",
                }
            )
            continue

        desired_notional = abs(float(row["adjustment_cny"]))
        if side == "BUY":
            account_id = row["account_id"]
            budget_left = max(
                0.0,
                account_buy_budget.get(account_id, 0.0) - allocated_buy_budget.get(account_id, 0.0),
            )
            desired_notional = min(desired_notional, budget_left)
            loss_budget_pct = _finite(limits.get("single_trade_loss_budget_pct"))
            adverse_move_pct = _finite(row.get("daily_limit_pct"))
            if (
                loss_budget_pct is None
                or loss_budget_pct <= 0
                or adverse_move_pct is None
                or adverse_move_pct <= 0
            ):
                manual_review.append(
                    {
                        "account_id": row["account_id"],
                        "thscode": row["thscode"],
                        "side": side,
                        "reason": "单笔损失预算或不利价格幅度缺失，未计算买入股数",
                    }
                )
                continue
            loss_budget_cny = nav * loss_budget_pct / 100
            maximum_notional_by_loss = loss_budget_cny / (adverse_move_pct / 100)
            desired_notional = min(desired_notional, maximum_notional_by_loss)
        else:
            loss_budget_pct = None
            adverse_move_pct = None
            loss_budget_cny = None
            maximum_notional_by_loss = None
        full_shares = _whole_lots(desired_notional / price, lot_size)
        odd_lot_full_exit = False
        if side == "SELL":
            available = _integer(row.get("available"))
            quantity = _integer(row.get("quantity"))
            full_liquidation = (
                row.get("target_weight_pct", 0.0) <= EPSILON
                and desired_notional >= _nonnegative(row.get("market_value_cny")) - max(1.0, price)
                and available == quantity
                and quantity > 0
            )
            if full_liquidation:
                # A-share sell rules permit disposing of the residual odd lot
                # only as the complete remaining position.
                full_shares = quantity
                odd_lot_full_exit = quantity % lot_size != 0
            else:
                full_shares = min(full_shares, _whole_lots(available, lot_size))
        daily_cap_cny = _finite(row["liquidity"].get("daily_cap_cny"), 0.0) or 0.0
        daily_cap_shares = _whole_lots(daily_cap_cny / price, lot_size)
        stage_fraction = initial_fraction if side == "SELL" else existing_fraction
        stage_shares = _whole_lots(full_shares * stage_fraction, lot_size)
        if stage_shares == 0 and full_shares >= lot_size:
            stage_shares = lot_size
        if odd_lot_full_exit and full_shares < lot_size and daily_cap_cny / price >= full_shares:
            stage_shares = full_shares
            daily_cap_shares = full_shares
        elif odd_lot_full_exit and stage_fraction >= 1 and daily_cap_cny / price >= full_shares:
            stage_shares = full_shares
            daily_cap_shares = max(daily_cap_shares, full_shares)
        stage_shares = min(stage_shares, daily_cap_shares, full_shares)
        valid_odd_lot_exit = bool(
            odd_lot_full_exit and stage_shares == full_shares and full_shares > 0
        )
        if (full_shares < lot_size or stage_shares < lot_size) and not valid_odd_lot_exit:
            manual_review.append(
                {
                    "account_id": row["account_id"],
                    "thscode": row["thscode"],
                    "side": side,
                    "reason": "调整缺口或单日成交容量不足一手，未生成订单",
                }
            )
            continue

        estimated = stage_shares * price
        if side == "BUY":
            allocated_buy_budget[row["account_id"]] += estimated
        order = {
                "stage": 1 if side == "SELL" else 3,
                "account_id": row["account_id"],
                "thscode": row["thscode"],
                "name": str(row.get("name") or row["thscode"]),
                "side": side,
                "quantity": stage_shares,
                "lot_size": lot_size,
                "odd_lot_full_exit": valid_odd_lot_exit,
                "reference_price_cny": _round(price, 4),
                "estimated_notional_cny": _round(estimated, 2),
                "full_gap_quantity": full_shares,
                "remaining_after_stage_quantity": max(0, full_shares - stage_shares),
                "daily_liquidity_cap_quantity": daily_cap_shares,
                "maximum_adv_participation_pct": _round(participation),
                "stage_fraction_of_gap": _round(stage_fraction),
                "confirmation_closes_required": confirmation_closes if side == "BUY" else 0,
                "confirmation_evidence_verified": side != "BUY",
                "authorization_status": (
                    "PENDING_CLOSE_CONFIRMATION" if side == "BUY" else "READY_FOR_HUMAN_REVIEW"
                ),
                "single_trade_loss_budget_pct": _round(loss_budget_pct),
                "single_trade_loss_budget_cny": _round(loss_budget_cny, 2),
                "assumed_adverse_move_pct": _round(adverse_move_pct),
                "maximum_notional_by_loss_budget_cny": _round(maximum_notional_by_loss, 2),
                "available_quantity_checked": side != "SELL" or stage_shares <= _integer(row.get("available")),
                "t_plus_one_guard": bool(t_plus_one),
                "reason_codes": row["risk_flags"],
                "submission_enabled": False,
            }
        orders.append(order)

    account_plans: list[dict[str, Any]] = []
    for account_id, account in accounts.items():
        account_orders = [row for row in orders if row["account_id"] == account_id]
        debt = _nonnegative(account.get("debt_cny"))
        account_plans.append(
            {
                "account_id": account_id,
                "account_type": account.get("account_type"),
                "steps": [
                    {
                        "stage": 1,
                        "action": "RISK_REDUCTION_SELLS",
                        "order_count": sum(1 for row in account_orders if row["side"] == "SELL"),
                    },
                    {
                        "stage": 2,
                        "action": "REPAY_DEBT_AND_REVERIFY_ACCOUNT",
                        "target_debt_cny": 0.0,
                        "current_debt_cny": _round(debt, 2),
                        "rule": "卖出所得先清偿负债；重新核对券商可用资金",
                    },
                    {
                        "stage": 3,
                        "action": "CONDITIONAL_BUYS",
                        "order_count": sum(1 for row in account_orders if row["side"] == "BUY"),
                        "condition": (
                            "仅在债务为零、现金缓冲达标、风险与事件数据仍有效，且信号连续"
                            f"确认 {confirmation_closes} 个收盘后执行"
                        ),
                    },
                    {
                        "stage": 4,
                        "action": "RECOMPUTE_BEFORE_NEXT_LOT",
                        "rule": "每一阶段成交后重新生成风险与再平衡快照",
                    },
                ],
            }
        )

    conditional_buys = [row for row in orders if row["side"] == "BUY"]
    post_stage_one = _post_stage_one_estimate(models, orders, nav, accounts, limits)
    execution = {
        "authorized": bool(orders) and not manual_review and not conditional_buys,
        "order_submission_enabled": False,
        "mode": "INDICATIVE_STAGED_DRAFT",
        "lot_size": lot_size,
        "t_plus_one": bool(t_plus_one),
        "normal_signal_confirmation_closes": confirmation_closes,
        "orders": orders,
        "conditional_order_count": len(conditional_buys),
        "manual_review": manual_review,
        "post_stage_1_estimate": post_stage_one,
        "residual_breaches": post_stage_one["residual_breaches"],
        "account_plans": account_plans,
        "safety_note": "股数按参考价和公开成交容量取整，仅供人工复核；本工具不会下单。",
    }
    return adjustments, execution


def _constraint_check(
    targets: list[dict[str, Any]], limits: dict[str, Any]
) -> dict[str, Any]:
    securities: dict[str, float] = {}
    themes: dict[str, float] = {}
    for row in targets:
        symbol = _normalise_thscode(row.get("thscode"))
        securities[symbol] = securities.get(symbol, 0.0) + _nonnegative(
            row.get("target_weight_pct")
        )
        theme = str(row.get("theme") or "unknown")
        themes[theme] = themes.get(theme, 0.0) + _nonnegative(row.get("target_weight_pct"))
    weights = sorted(securities.values(), reverse=True)
    largest = weights[0] if weights else 0.0
    top5 = sum(weights[:5])
    largest_theme = max(themes.values(), default=0.0)
    single_limit = _nonnegative(limits.get("maximum_single_position_pct"), 8.0)
    top5_limit = _nonnegative(limits.get("maximum_top5_pct"), 35.0)
    theme_limit = _nonnegative(limits.get("maximum_theme_pct"), 50.0)
    return {
        "basis": "FINAL_CONTINUOUS_TARGET_BEFORE_LOT_ROUNDING_OR_STAGE_EXECUTION",
        "compliant": (
            largest <= single_limit + 1e-4
            and top5 <= top5_limit + 1e-4
            and largest_theme <= theme_limit + 1e-4
        ),
        "largest_position_pct": _round(largest),
        "largest_position_limit_pct": _round(single_limit),
        "top5_pct": _round(top5),
        "top5_limit_pct": _round(top5_limit),
        "largest_theme_pct": _round(largest_theme),
        "theme_limit_pct": _round(theme_limit),
        "securities": [
            {"thscode": symbol, "target_weight_pct": _round(weight)}
            for symbol, weight in sorted(
                securities.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "themes": [
            {"theme": theme, "target_weight_pct": _round(weight)}
            for theme, weight in sorted(themes.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def get_rebalance_plan(
    risk_snapshot: dict[str, Any],
    portfolio: dict[str, Any],
    policy: dict[str, Any],
    liquidity_payload: dict[str, Any] | None = None,
    event_payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a fail-closed, explainable rebalance plan.

    Unverified account state returns only ``gate`` and ``priority_draft`` (plus
    metadata/warnings); it intentionally omits targets, amounts and share
    counts.  Verified state may return indicative whole-lot quantities.
    """
    if not all(isinstance(value, dict) for value in (risk_snapshot, portfolio, policy)):
        raise TypeError("risk_snapshot, portfolio and policy must be dictionaries")
    generated_at = (now or _now()).astimezone(CN_TZ).isoformat(timespec="seconds")
    limits = policy.get("portfolio_limits") if isinstance(policy.get("portfolio_limits"), dict) else {}
    execution_policy = policy.get("execution") if isinstance(policy.get("execution"), dict) else {}

    effective_now = (now or _now()).astimezone(CN_TZ)
    gate = _verification_gate(portfolio, risk_snapshot, policy, effective_now)
    binding = _input_binding(
        portfolio, policy, risk_snapshot, liquidity_payload, event_payload
    )
    try:
        configured, accounts = _portfolio_records(portfolio)
    except ValueError:
        configured, accounts = [], {}
    live = _risk_records(risk_snapshot)

    # Use risk rows when available; otherwise retain only non-monetary configured
    # identity for a fail-closed priority list.
    if live:
        configured_map = {row["position_key"]: row for row in configured}
        positions = [
            {**configured_map.get(row["position_key"], {}), **row}
            for row in live
        ]
    else:
        positions = configured
    participation = _nonnegative(execution_policy.get("maximum_adv_participation_pct_per_day"), 10.0)
    horizon = _nonnegative(execution_policy.get("liquidity_horizon_days"), 3.0)
    liquidity_gate = _liquidity_payload_gate(
        liquidity_payload, portfolio, policy, effective_now
    )
    liquidity = _liquidity_assessments(
        positions,
        liquidity_payload,
        participation,
        horizon,
        liquidity_gate["verified"],
    )
    event_gate = _event_payload_gate(event_payload, portfolio, policy, effective_now)
    events = _event_assessments(positions, event_payload, event_gate["verified"])
    priority, models = _priority_and_models(positions, accounts, limits, liquidity, events)

    warnings: list[str] = []
    if not liquidity_gate["verified"]:
        warnings.append("成交容量数据缺失或陈旧；任何相关标的均不得把流动性风险记为零")
    if not event_gate["verified"]:
        warnings.append("事件风险数据缺失或陈旧；任何相关标的均不得被视为无事件风险")

    if not gate["verified"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "status": "DATA_UNVERIFIED",
            "executable": False,
            "input_binding": binding,
            "gate": gate,
            "liquidity_gate": liquidity_gate,
            "event_gate": event_gate,
            "priority_draft": priority,
            "warnings": warnings,
            "note": "账户门禁未通过：未生成目标权重、调整金额或买卖股数。",
        }

    metrics = risk_snapshot.get("metrics") if isinstance(risk_snapshot.get("metrics"), dict) else {}
    nav = _finite(metrics.get("nav_cny"))
    if nav is None or nav <= 0:
        closed_gate = copy.deepcopy(gate)
        closed_gate["verified"] = False
        closed_gate["reasons"].append({"code": "NAV_INVALID", "message": "净资产缺失或非正数"})
        closed_gate["required_actions"] = ["重新核对账户净资产后再计算"]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "status": "DATA_UNVERIFIED",
            "executable": False,
            "input_binding": binding,
            "gate": closed_gate,
            "liquidity_gate": liquidity_gate,
            "event_gate": event_gate,
            "priority_draft": priority,
            "warnings": warnings,
            "note": "账户门禁未通过：未生成目标权重、调整金额或买卖股数。",
        }

    current_market_value = sum(_nonnegative(row.get("market_value_cny")) for row in models)
    current_exposure_pct = current_market_value / nav * 100
    minimum_cash_pct = _nonnegative(limits.get("minimum_cash_pct"), 10.0)
    maximum_gross_pct = _nonnegative(limits.get("max_gross_exposure_pct"), 100.0)
    policy_target_pct = _nonnegative(limits.get("target_equity_exposure_pct"), 88.0)
    structural_target_pct = min(policy_target_pct, maximum_gross_pct, max(0.0, 100.0 - minimum_cash_pct))
    risk_scale, scale_evidence, aggregate_missing = _aggregate_risk_scale(metrics, limits)
    buy_gate = _buy_data_gate(risk_snapshot, aggregate_missing, policy)
    buy_policy_reasons: list[dict[str, str]] = []
    if risk_scale < 1.0 - EPSILON:
        buy_policy_reasons.append(
            {
                "code": "CURRENT_AGGREGATE_RISK_OVER_LIMIT",
                "message": "当前组合波动、ES、压力损失或回撤仍超过当前策略上限",
            }
        )
    if _nonnegative(metrics.get("debt_pct")) > 0:
        buy_policy_reasons.append(
            {"code": "DEBT_NOT_ZERO", "message": "清偿全部负债前禁止新增买入"}
        )
    if _nonnegative(metrics.get("cash_pct")) + EPSILON < minimum_cash_pct:
        buy_policy_reasons.append(
            {"code": "CASH_BUFFER_BELOW_LIMIT", "message": "现金缓冲达到下限前禁止新增买入"}
        )
    if _nonnegative(metrics.get("gross_exposure_pct")) > maximum_gross_pct + EPSILON:
        buy_policy_reasons.append(
            {"code": "GROSS_EXPOSURE_OVER_LIMIT", "message": "总敞口回到上限内前禁止新增买入"}
        )
    buy_gate["reasons"].extend(buy_policy_reasons)
    buy_gate["verified"] = not buy_gate["reasons"]
    desired_exposure_pct = structural_target_pct * risk_scale

    upstream_risk_state = str(risk_snapshot.get("risk_state") or risk_snapshot.get("state") or "UNKNOWN")
    buys_globally_allowed = (
        risk_snapshot.get("new_buy_allowed") is True
        and upstream_risk_state == "NORMAL"
        and not aggregate_missing
        and buy_gate["verified"]
    )
    symbol_risk_caps: dict[str, float] = {}
    symbol_current_weights: dict[str, float] = {}
    for row in models:
        symbol = row["thscode"]
        risk_cap = _nonnegative(row["risk_adjusted_cap_pct"])
        symbol_risk_caps[symbol] = min(symbol_risk_caps.get(symbol, risk_cap), risk_cap)
        symbol_current_weights[symbol] = symbol_current_weights.get(symbol, 0.0) + _nonnegative(
            row.get("weight_pct")
        )

    for row in models:
        symbol = row["thscode"]
        cap = row["risk_adjusted_cap_pct"]
        if not buys_globally_allowed or not row["data_complete_for_increase"]:
            cap = min(cap, _nonnegative(row.get("weight_pct")))
        row["target_cap_pct"] = max(0.0, cap)
        aggregate_cap = symbol_risk_caps[symbol]
        if not buys_globally_allowed:
            aggregate_cap = min(aggregate_cap, symbol_current_weights[symbol])
        row["symbol_target_cap_pct"] = max(0.0, aggregate_cap)

    theme_cap = _nonnegative(limits.get("maximum_theme_pct"), 50.0)
    target_weights, unallocated_pct = _allocate_targets(models, desired_exposure_pct, theme_cap)
    targets: list[dict[str, Any]] = []
    for row in models:
        target_weight = target_weights[row["position_key"]]
        targets.append(
            {
                "account_id": row["account_id"],
                "thscode": row["thscode"],
                "name": str(row.get("name") or row["thscode"]),
                "theme": str(row.get("theme") or "unknown"),
                "current_weight_pct": _round(row.get("weight_pct")),
                "target_weight_pct": _round(target_weight),
                "target_value_cny": _round(nav * target_weight / 100, 2),
                "risk_adjusted_cap_pct": _round(row["target_cap_pct"]),
                "aggregate_symbol_cap_pct": _round(row["symbol_target_cap_pct"]),
                "risk_score": _round(row["risk_score"]),
                "increase_allowed": bool(buys_globally_allowed and row["data_complete_for_increase"]),
                "risk_flags": row["risk_flags"],
                "explanation": row["reasons"],
            }
        )

    adjustments, execution = _build_execution(
        models,
        target_weights,
        nav,
        accounts,
        limits,
        execution_policy,
        buys_globally_allowed,
    )
    target_exposure_pct = sum(target_weights.values())
    target_cash_pct = max(0.0, 100.0 - target_exposure_pct)
    target_market_value = nav * target_exposure_pct / 100
    total_reduction = sum(max(0.0, -float(row["adjustment_cny"] or 0.0)) for row in adjustments)
    total_addition = sum(max(0.0, float(row["adjustment_cny"] or 0.0)) for row in adjustments)
    constraint_check = _constraint_check(targets, limits)

    if aggregate_missing:
        warnings.append("组合波动、ES、压力损失或回撤数据缺失或不完整；按保守敞口折扣且禁止新增买入")
    if unallocated_pct > 1e-4:
        warnings.append("现有标的在集中度与风险上限下无法承载全部目标敞口；差额保留为现金而非臆造新标的")
    if execution["manual_review"]:
        warnings.append("部分调整缺少成交容量、事件状态或整手条件，已进入人工复核且未生成股数")

    if execution["authorized"]:
        status = "READY"
    elif execution.get("conditional_order_count"):
        status = "CONDITIONAL_PLAN"
    else:
        status = "READY_WITH_REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "executable": execution["authorized"],
        "input_binding": binding,
        "gate": gate,
        "liquidity_gate": liquidity_gate,
        "event_gate": event_gate,
        "buy_gate": buy_gate,
        "priority_draft": priority,
        "policy_basis": {
            "policy_version": policy.get("policy_version"),
            "target_equity_exposure_pct": _round(policy_target_pct),
            "maximum_gross_exposure_pct": _round(maximum_gross_pct),
            "minimum_cash_pct": _round(minimum_cash_pct),
            "target_debt_pct": 0.0,
            "maximum_single_position_pct": _round(limits.get("maximum_single_position_pct")),
            "maximum_top5_pct": _round(limits.get("maximum_top5_pct")),
            "maximum_theme_pct": _round(theme_cap),
        },
        "portfolio_transition": {
            "nav_cny": _round(nav, 2),
            "current_market_value_cny": _round(current_market_value, 2),
            "current_gross_exposure_pct": _round(current_exposure_pct),
            "structural_target_exposure_pct": _round(structural_target_pct),
            "aggregate_risk_scale": _round(risk_scale),
            "aggregate_risk_evidence": scale_evidence,
            "target_market_value_cny": _round(target_market_value, 2),
            "target_gross_exposure_pct": _round(target_exposure_pct),
            "target_cash_pct": _round(target_cash_pct),
            "target_debt_pct": 0.0,
            "full_reduction_gap_cny": _round(total_reduction, 2),
            "full_addition_gap_cny": _round(total_addition, 2),
            "unallocated_due_to_constraints_pct": _round(unallocated_pct),
            "risk_projection_method": "当前组合风险按目标/当前总敞口线性缩放的保守近似；每阶段后必须重算",
        },
        "constraint_check": constraint_check,
        "targets": targets,
        "adjustments": adjustments,
        "execution": execution,
        "warnings": warnings,
        "note": "本结果是可解释的本地风控草案，不承诺收益，不连接券商，不自动下单。",
    }


def _atomic_write(path: Path, payload: dict[str, Any], compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":") if compact else None,
                indent=None if compact else 2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a fail-closed A-share rebalance draft")
    parser.add_argument("--risk-file", type=Path, default=DEFAULT_RISK)
    parser.add_argument("--portfolio-file", type=Path, default=DEFAULT_PORTFOLIO)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--liquidity-file", type=Path)
    parser.add_argument("--events-file", type=Path)
    parser.add_argument("--output", type=Path, help="Write JSON atomically; omit to print stdout")
    parser.add_argument("--compact", action="store_true")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = get_rebalance_plan(
            risk_snapshot=_read_json(args.risk_file),
            portfolio=_read_json(args.portfolio_file),
            policy=_read_json(args.policy_file),
            liquidity_payload=_read_json(args.liquidity_file) if args.liquidity_file else None,
            event_payload=_read_json(args.events_file) if args.events_file else None,
        )
    except Exception as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now().isoformat(timespec="seconds"),
            "status": "ERROR",
            "executable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 2
    else:
        exit_code = 0

    if args.output:
        _atomic_write(args.output, payload, args.compact)
    else:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":") if args.compact else None,
                indent=None if args.compact else 2,
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_main())
