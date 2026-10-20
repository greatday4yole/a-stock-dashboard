#!/usr/bin/env python3
"""Portfolio risk snapshot engine for the local A-share dashboard.

The engine separates two clocks:

* current quotes determine NAV, exposure, concentration and scenario losses;
* completed, forward-adjusted daily closes determine volatility, ES, drawdown,
  correlations and risk contribution.

It is a risk-control aid, not a return forecast or a guarantee of profit.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = ROOT / "config" / "portfolio.json"
POLICY_PATH = ROOT / "config" / "risk-policy.json"
DATA_DIR = ROOT / "data"
RISK_CACHE_PATH = DATA_DIR / "risk-snapshot.json"

CN_TZ = timezone(timedelta(hours=8))
RISK_CACHE_TTL_SECONDS = 15.0
HISTORY_CACHE_TTL_SECONDS = 6 * 60 * 60.0

_CACHE_LOCK = threading.RLock()
_MEMORY_CACHE: dict[str, Any] = {
    "snapshot": None,
    "fingerprint": "",
    "monotonic": 0.0,
}
_HISTORY_CACHE: dict[str, dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _rounded(value: Any, digits: int = 4) -> float | None:
    number = _finite_float(value)
    return None if number is None else round(number, digits)


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
    except ValueError:
        pass
    for pattern in ("%Y%m%d%H%M%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def _parse_date(value: Any) -> date | None:
    parsed = _parse_datetime(value)
    return parsed.date() if parsed else None


def _business_day_lag(older: date | None, newer: date) -> int | None:
    """Weekday approximation used only as a conservative freshness guard.

    Exchange holidays can overstate the lag; allowing one session avoids treating
    Friday data as stale on Monday. A proper exchange calendar can replace this
    helper without changing the public contract.
    """

    if older is None or older > newer:
        return None
    cursor = older
    lag = 0
    while cursor < newer:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            lag += 1
    return lag


def _symbol_from_thscode(thscode: str) -> str:
    code, exchange = str(thscode).upper().split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if not prefix or len(code) != 6 or not code.isdigit():
        raise ValueError(f"Unsupported thscode: {thscode!r}")
    return prefix + code


def _flatten_positions(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in portfolio.get("accounts") or []:
        for configured in account.get("positions") or []:
            item = dict(configured)
            thscode = str(item.get("thscode") or "").upper()
            item.update(
                {
                    "thscode": thscode,
                    "symbol": _symbol_from_thscode(thscode),
                    "code": thscode.split(".", 1)[0],
                    "account_id": str(account.get("account_id") or ""),
                    "account_type": str(account.get("account_type") or ""),
                }
            )
            rows.append(item)
    if not rows:
        raise ValueError("portfolio.json contains no positions")
    return rows


def _history_file(thscode: str) -> Path | None:
    code, exchange = thscode.upper().split(".", 1)
    candidates = sorted(
        DATA_DIR.glob(f"portfolio-analysis-*/history-{code}-{exchange}.json"),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _frame_from_local_history(path: Path, thscode: str) -> pd.DataFrame:
    payload = _read_json(path)
    body = payload.get("data") or {}
    if not payload.get("ok"):
        raise ValueError(f"local history is not ok: {path.name}")
    if str(body.get("thscode") or "").upper() != thscode.upper():
        raise ValueError(f"history symbol mismatch in {path.name}")
    if body.get("interval") != "1d" or body.get("adjust") != "forward":
        raise ValueError(f"history must be 1d forward-adjusted: {path.name}")
    rows = body.get("item") or []
    records: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _finite_float(row.get("date_ms"))
        close = _finite_float(row.get("close_price"))
        if timestamp is None or close is None or close <= 0:
            continue
        session = datetime.fromtimestamp(timestamp / 1000.0, CN_TZ).date().isoformat()
        records.append({"date": session, "close": close})
    frame = pd.DataFrame(records, columns=["date", "close"])
    if frame.empty:
        raise ValueError(f"history has no valid closes: {path.name}")
    return frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def _fetch_tencent_history(symbol: str, count: int = 320) -> pd.DataFrame:
    try:
        from scripts.market_snapshot import fetch_kline
    except ImportError:  # direct `python scripts/portfolio_risk_engine.py`
        from market_snapshot import fetch_kline

    frame = fetch_kline(symbol, count=count)
    result = frame.loc[:, ["date", "close"]].copy()
    result["date"] = result["date"].astype(str).str[:10]
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    return (
        result.dropna()
        .loc[result["close"] > 0]
        .drop_duplicates("date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _load_history_for_position(
    position: dict[str, Any],
    minimum_sessions: int,
    tolerance_pct: float,
    force: bool = False,
    expected_session: str | None = None,
) -> dict[str, Any]:
    """Load reproducible local THS history, using Tencent qfq only to fill gaps."""

    thscode = position["thscode"]
    path = _history_file(thscode)
    local_mtime = path.stat().st_mtime if path and path.exists() else None
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        cached = _HISTORY_CACHE.get(thscode)
        if (
            cached
            and not force
            and now_mono - float(cached["monotonic"]) < HISTORY_CACHE_TTL_SECONDS
            and cached.get("local_mtime") == local_mtime
        ):
            result = copy.copy(cached["result"])
            result["frame"] = cached["result"]["frame"].copy()
            result["memory_cached"] = True
            return result

    local = pd.DataFrame(columns=["date", "close"])
    warnings: list[str] = []
    errors: list[str] = []
    local_errors: list[str] = []
    sources: list[str] = []
    if path:
        try:
            local = _frame_from_local_history(path, thscode)
            sources.append("hithink-local-forward")
        except Exception as exc:
            local_errors.append(f"{path.name}: {type(exc).__name__}: {exc}")

    combined = local
    local_last = str(local.iloc[-1]["date"]) if not local.empty else None
    local_behind = bool(expected_session and (not local_last or local_last < expected_session))
    # One extra close is needed to produce minimum_sessions daily returns. A
    # long local file can still be out of date, so freshness—not just length—
    # triggers a Tencent merge. A forced manual refresh bypasses the in-memory
    # history cache and revalidates this freshness decision.
    if len(local) < minimum_sessions + 1 or local_behind:
        try:
            remote = _fetch_tencent_history(position["symbol"], count=320)
            sources.append("tencent-qfq")
            if not local.empty and not remote.empty:
                overlap = local.merge(remote, on="date", suffixes=("_local", "_remote"))
                if not overlap.empty:
                    latest = overlap.iloc[-1]
                    denominator = abs(float(latest["close_local"]))
                    difference = (
                        abs(float(latest["close_remote"]) - float(latest["close_local"]))
                        / denominator
                        * 100
                        if denominator
                        else math.inf
                    )
                    if difference > tolerance_pct:
                        warnings.append(
                            f"{thscode} 腾讯/同花顺重叠收盘价差 {difference:.3f}% "
                            f"> {tolerance_pct:.3f}%，拒绝拼接"
                        )
                        remote = pd.DataFrame(columns=["date", "close"])
            if not remote.empty:
                parts = [remote] + ([] if local.empty else [local])
                # Local THS rows win on overlap; Tencent only supplies missing dates.
                combined = (
                    pd.concat(parts, ignore_index=True)
                    .drop_duplicates("date", keep="last")
                    .sort_values("date")
                    .reset_index(drop=True)
                )
        except Exception as exc:
            errors.append(f"Tencent qfq: {type(exc).__name__}: {exc}")

    if local_errors:
        # A successfully validated Tencent qfq replacement makes an unusable
        # optional local file a source warning, not a failed historical layer.
        (warnings if not combined.empty else errors).extend(local_errors)

    result = {
        "frame": combined,
        "source": "+".join(sources) if sources else "unavailable",
        "path": str(path) if path else "",
        "warnings": warnings,
        "errors": errors,
        "memory_cached": False,
    }
    with _CACHE_LOCK:
        _HISTORY_CACHE[thscode] = {
            "result": result,
            "local_mtime": local_mtime,
            "monotonic": now_mono,
        }
    copied = copy.copy(result)
    copied["frame"] = combined.copy()
    return copied


def _quote_keys(row: dict[str, Any]) -> list[str]:
    keys = []
    for field in ("thscode", "symbol", "code"):
        value = str(row.get(field) or "").strip()
        if value:
            keys.extend([value.upper(), value.lower()])
    return keys


def _quote_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in payload.get("positions") or []:
        if not isinstance(row, dict):
            continue
        for key in _quote_keys(row):
            result[key] = row
    return result


def _find_quote(mapping: dict[str, dict[str, Any]], position: dict[str, Any]) -> dict[str, Any] | None:
    for key in _quote_keys(position):
        if key in mapping:
            return mapping[key]
    return None


def _portfolio_input_fingerprint(
    portfolio: dict[str, Any], policy: dict[str, Any], quotes_payload: dict[str, Any]
) -> str:
    relevant_quotes = []
    for row in quotes_payload.get("positions") or []:
        relevant_quotes.append(
            {
                key: row.get(key)
                for key in (
                    "thscode",
                    "symbol",
                    "code",
                    "price",
                    "quote_time",
                    "is_stale",
                    "data_source",
                )
            }
        )
    value = {
        # Bind the cache to every account, holding, cash/debt and policy field;
        # identifiers alone cannot detect an edited snapshot whose version was
        # accidentally left unchanged.
        "portfolio": portfolio,
        "policy": policy,
        "quotes_generated_at": quotes_payload.get("generated_at"),
        "market_status": quotes_payload.get("market_status"),
        "quote_cache": quotes_payload.get("cache"),
        "quotes": relevant_quotes,
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _breach(
    code: str,
    severity: str,
    metric: str,
    actual: Any,
    limit: Any,
    unit: str,
    message: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "metric": metric,
        "actual": _rounded(actual, 4),
        "limit": _rounded(limit, 4),
        "unit": unit,
        "message": message,
    }


def _add_limit_breaches(
    metrics: dict[str, Any], positions: list[dict[str, Any]], limits: dict[str, Any]
) -> list[dict[str, Any]]:
    breaches: list[dict[str, Any]] = []

    def above(metric: str, limit_key: str, code: str, severity: str, message: str) -> None:
        actual = _finite_float(metrics.get(metric))
        limit = _finite_float(limits.get(limit_key))
        if actual is not None and limit is not None and actual > limit + 1e-9:
            breaches.append(_breach(code, severity, metric, actual, limit, "%", message))

    def below(metric: str, limit_key: str, code: str, severity: str, message: str) -> None:
        actual = _finite_float(metrics.get(metric))
        limit = _finite_float(limits.get(limit_key))
        if actual is not None and limit is not None and actual < limit - 1e-9:
            breaches.append(_breach(code, severity, metric, actual, limit, "%", message))

    above(
        "gross_exposure_pct",
        "max_gross_exposure_pct",
        "GROSS_EXPOSURE",
        "critical",
        "总股票敞口超过上限",
    )
    below("cash_pct", "minimum_cash_pct", "MINIMUM_CASH", "high", "现金缓冲低于下限")
    above("debt_pct", "maximum_debt_pct", "DEBT", "critical", "负债比例超过上限")
    above("top5_pct", "maximum_top5_pct", "TOP5_CONCENTRATION", "high", "前五大持仓集中度超限")
    above("largest_theme_pct", "maximum_theme_pct", "THEME_CONCENTRATION", "high", "最大主题集中度超限")
    above(
        "annualized_volatility_pct",
        "maximum_annualized_volatility_pct",
        "ANNUALIZED_VOLATILITY",
        "high",
        "组合历史年化波动率超限",
    )
    above(
        "daily_es_97_5_pct",
        "maximum_daily_es_97_5_pct",
        "DAILY_ES_97_5",
        "high",
        "组合日度 ES(97.5%) 超限",
    )
    above(
        "worst_5d_stress_loss_pct",
        "maximum_5d_stress_loss_pct",
        "STRESS_LOSS",
        "high",
        "最坏压力情景损失超限",
    )
    above(
        "static_weight_max_drawdown_pct",
        "maximum_drawdown_pct",
        "MAX_DRAWDOWN_PROXY",
        "high",
        "静态权重代理最大回撤超限",
    )

    single_limit = _finite_float(limits.get("maximum_single_position_pct"))
    if single_limit is not None:
        for row in positions:
            weight = _finite_float(row.get("weight_pct"))
            if weight is not None and weight > single_limit + 1e-9:
                breaches.append(
                    _breach(
                        "SINGLE_POSITION",
                        "high",
                        row["thscode"],
                        weight,
                        single_limit,
                        "%NAV",
                        f"{row['name']} 单票权重超限",
                    )
                )
    return breaches


def _scenario_metrics(
    positions: list[dict[str, Any]], nav: float, policy: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for scenario_id, scenario in (policy.get("stress_scenarios") or {}).items():
        shocks = scenario.get("theme_returns_pct") or {}
        pnl_pct = 0.0
        details: list[dict[str, Any]] = []
        for row in positions:
            shock = _finite_float(shocks.get(row.get("theme")), 0.0) or 0.0
            weight = _finite_float(row.get("weight_pct"), 0.0) or 0.0
            contribution = weight * shock / 100.0
            pnl_pct += contribution
            details.append(
                {
                    "thscode": row["thscode"],
                    "theme": row.get("theme", ""),
                    "shock_pct": round(shock, 4),
                    "nav_impact_pct": round(contribution, 4),
                }
            )
        loss_pct = max(0.0, -pnl_pct)
        results.append(
            {
                "scenario": scenario_id,
                "label": scenario.get("label") or scenario_id,
                "portfolio_return_pct": round(pnl_pct, 4),
                "loss_pct": round(loss_pct, 4),
                "loss_cny": round(nav * loss_pct / 100.0, 2),
                "position_impacts": details,
            }
        )
    return sorted(results, key=lambda row: row["loss_pct"], reverse=True)


def _historical_metrics(
    positions: list[dict[str, Any]],
    policy: dict[str, Any],
    market_open: bool,
    expected_session: str | None,
    now: datetime,
    force: bool,
    history_loader: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    limits = policy.get("portfolio_limits") or {}
    quality_policy = policy.get("data_quality") or {}
    minimum = int(_finite_float(limits.get("minimum_history_sessions"), 60) or 60)
    volatility_window = max(
        2, int(_finite_float(limits.get("volatility_lookback_sessions"), 120) or 120)
    )
    # Fewer than 250 observations produces an unstable 97.5% tail estimate.
    es_window = max(250, int(_finite_float(limits.get("es_lookback_sessions"), 250) or 250))
    drawdown_window = max(
        2, int(_finite_float(limits.get("drawdown_lookback_sessions"), 120) or 120)
    )
    max_lag = int(_finite_float(quality_policy.get("daily_bar_max_trading_day_lag"), 1) or 1)
    tolerance = _finite_float(quality_policy.get("price_cross_source_tolerance_pct"), 0.5) or 0.5
    today = now.date()

    frames: dict[str, pd.Series] = {}
    source_rows: list[dict[str, Any]] = []
    short: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    partial_excluded = False
    position_history: dict[str, dict[str, Any]] = {}

    for position in positions:
        thscode = position["thscode"]
        result = history_loader(
            position,
            minimum,
            tolerance,
            force=force,
            expected_session=expected_session,
        )
        frame = result.get("frame")
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame(columns=["date", "close"])
        frame = frame.loc[:, ["date", "close"]].copy() if not frame.empty else frame
        if not frame.empty:
            frame["date"] = frame["date"].astype(str).str[:10]
            frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
            frame = frame.dropna().loc[frame["close"] > 0].drop_duplicates("date", keep="last")
            if market_open:
                current_mask = frame["date"] == today.isoformat()
                if bool(current_mask.any()):
                    partial_excluded = True
                    frame = frame.loc[~current_mask]
            frame = frame.sort_values("date").reset_index(drop=True)

        sessions = len(frame)
        last_session = str(frame.iloc[-1]["date"]) if sessions else None
        lag = _business_day_lag(_parse_date(last_session), today)
        is_stale = lag is None or lag > max_lag
        if is_stale and sessions:
            stale.append(thscode)
        if sessions < minimum + 1:
            (short if sessions else missing).append(thscode)
        else:
            series = frame.set_index("date")["close"].astype(float)
            frames[thscode] = series
        warnings.extend(str(x) for x in result.get("warnings") or [])
        errors.extend(f"{thscode}: {x}" for x in result.get("errors") or [])
        position_history[thscode] = {
            "sessions": sessions,
            "last_session": last_session,
            "trading_day_lag": lag,
            "stale": is_stale,
            "source": result.get("source") or "unavailable",
            "modeled": thscode in frames,
        }
        source_rows.append(
            {
                "thscode": thscode,
                "source": result.get("source") or "unavailable",
                "path": result.get("path") or "",
                "sessions": sessions,
                "last_session": last_session,
                "memory_cached": bool(result.get("memory_cached")),
            }
        )

    total_market_value = sum(float(row["market_value_cny"]) for row in positions)
    modeled_market_value = sum(
        float(row["market_value_cny"]) for row in positions if row["thscode"] in frames
    )
    coverage_pct = modeled_market_value / total_market_value * 100 if total_market_value else 0.0

    metrics: dict[str, Any] = {
        "annualized_volatility_pct": None,
        "daily_var_97_5_pct": None,
        "daily_es_97_5_pct": None,
        "static_weight_max_drawdown_pct": None,
    }
    portfolio_sessions = 0
    as_of_session = None
    correlations: dict[str, dict[str, float | None]] = {}
    contributions: list[dict[str, Any]] = []
    window_samples = {
        "volatility": {"configured_sessions": volatility_window, "actual_sessions": 0, "sufficient": False},
        "es_97_5": {"configured_sessions": es_window, "actual_sessions": 0, "sufficient": False},
        "drawdown": {"configured_sessions": drawdown_window, "actual_sessions": 0, "sufficient": False},
        "correlation_and_risk_contribution": {
            "configured_sessions": volatility_window,
            "actual_sessions": 0,
            "sufficient": False,
        },
    }

    if frames:
        close_matrix = pd.concat(frames, axis=1).sort_index().ffill().dropna(how="any")
        returns = close_matrix / close_matrix.shift(1) - 1.0
        returns = returns.replace([np.inf, -np.inf], np.nan).dropna(how="any")
        # A valid forward-adjusted long-only A-share close cannot lose 100% in one session.
        returns = returns.loc[(returns > -1.0).all(axis=1)]
        portfolio_sessions = len(returns)
        as_of_session = str(returns.index[-1]) if portfolio_sessions else None
        weights = pd.Series(
            {
                row["thscode"]: float(row["weight_pct"]) / 100.0
                for row in positions
                if row["thscode"] in returns.columns
            }
        ).reindex(returns.columns)
        portfolio_returns = returns.mul(weights, axis=1).sum(axis=1)

        if portfolio_sessions >= 2:
            volatility_returns = portfolio_returns.tail(volatility_window)
            es_returns = portfolio_returns.tail(es_window)
            drawdown_returns = portfolio_returns.tail(drawdown_window)
            risk_returns = returns.tail(volatility_window)
            window_samples = {
                "volatility": {
                    "configured_sessions": volatility_window,
                    "actual_sessions": len(volatility_returns),
                    "sufficient": len(volatility_returns) >= volatility_window,
                },
                "es_97_5": {
                    "configured_sessions": es_window,
                    "actual_sessions": len(es_returns),
                    "sufficient": len(es_returns) >= es_window,
                },
                "drawdown": {
                    "configured_sessions": drawdown_window,
                    "actual_sessions": len(drawdown_returns),
                    "sufficient": len(drawdown_returns) >= drawdown_window,
                },
                "correlation_and_risk_contribution": {
                    "configured_sessions": volatility_window,
                    "actual_sessions": len(risk_returns),
                    "sufficient": len(risk_returns) >= volatility_window,
                },
            }
            daily_std = float(volatility_returns.std(ddof=1))
            metrics["annualized_volatility_pct"] = round(daily_std * math.sqrt(242) * 100, 4)
            if len(es_returns) >= minimum:
                quantile = float(es_returns.quantile(0.025))
                tail_count = max(1, int(math.ceil(len(es_returns) * 0.025)))
                tail = es_returns.nsmallest(tail_count)
                metrics["daily_var_97_5_pct"] = round(max(0.0, -quantile * 100), 4)
                metrics["daily_es_97_5_pct"] = round(
                    max(0.0, -float(tail.mean()) * 100), 4
                )
                if len(es_returns) < es_window:
                    warnings.append(
                        f"ES97.5 使用可用的 {len(es_returns)}/{es_window} 个收益样本；"
                        "结果已降级，不等同于完整窗口估计"
                    )
            else:
                warnings.append(
                    f"ES97.5 仅有 {len(es_returns)} 个收益样本，低于最低要求 {minimum}，"
                    "拒绝发布过短样本估计"
                )
            wealth = (1.0 + drawdown_returns).cumprod()
            drawdown = 1.0 - wealth / wealth.cummax()
            metrics["static_weight_max_drawdown_pct"] = round(
                max(0.0, float(drawdown.max()) * 100), 4
            )

            corr = risk_returns.corr()
            correlations = {
                symbol: {
                    other: _rounded(corr.loc[symbol, other], 4)
                    for other in corr.columns
                }
                for symbol in corr.index
            }
            covariance = risk_returns.cov().to_numpy(dtype=float)
            weight_vector = weights.to_numpy(dtype=float)
            portfolio_variance = float(weight_vector @ covariance @ weight_vector)
            marginal = covariance @ weight_vector
            component = weight_vector * marginal
            for index, symbol in enumerate(returns.columns):
                contribution_pct = (
                    float(component[index] / portfolio_variance * 100)
                    if portfolio_variance > 0
                    else None
                )
                individual_vol = float(risk_returns[symbol].std(ddof=1) * math.sqrt(242) * 100)
                contributions.append(
                    {
                        "thscode": symbol,
                        "annualized_volatility_pct": round(individual_vol, 4),
                        "risk_contribution_pct": _rounded(contribution_pct, 4),
                    }
                )
        else:
            warnings.append("组合共同历史收益少于 2 个交易日，无法计算统计风险")

    modeled_fresh_value = sum(
        float(row["market_value_cny"])
        for row in positions
        if row["thscode"] in frames and not position_history[row["thscode"]]["stale"]
    )
    fresh_coverage_pct = modeled_fresh_value / total_market_value * 100 if total_market_value else 0.0
    minimum_confidence = _finite_float(quality_policy.get("minimum_layer_confidence"), 0.7) or 0.7
    session_ratio = min(1.0, portfolio_sessions / minimum) if minimum else 1.0
    confidence = min(1.0, coverage_pct / 100.0) * session_ratio
    # A stale modeled layer is not considered verified even if it has many rows.
    confidence *= min(1.0, fresh_coverage_pct / max(coverage_pct, 1e-9))
    adequate = confidence >= minimum_confidence and portfolio_sessions >= minimum
    full = (
        adequate
        and len(frames) == len(positions)
        and not short
        and not missing
        and not stale
        and not errors
        and all(window["sufficient"] for window in window_samples.values())
    )
    history = {
        "status": "FULL" if full else ("DEGRADED" if adequate else "UNAVAILABLE"),
        "minimum_sessions": minimum,
        "portfolio_return_sessions": portfolio_sessions,
        "as_of_session": as_of_session,
        "partial_session_excluded": partial_excluded,
        "model_coverage_pct": round(coverage_pct, 4),
        "fresh_model_coverage_pct": round(fresh_coverage_pct, 4),
        "confidence": round(confidence, 4),
        "eligible_positions": sorted(frames),
        "short_history_positions": sorted(short),
        "missing_positions": sorted(missing),
        "stale_positions": sorted(stale),
        "sources": source_rows,
        "correlation_matrix": correlations,
        "risk_contributions": contributions,
        "windows": window_samples,
        "warnings": warnings,
        "errors": errors,
    }
    return metrics, history, position_history


def _data_quality(
    portfolio: dict[str, Any],
    quotes_payload: dict[str, Any],
    positions: list[dict[str, Any]],
    history: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    quality_policy = policy.get("data_quality") or {}
    hard_ttl = _finite_float(quality_policy.get("live_quote_hard_stale_seconds"), 300) or 300
    lunch_ttl = (
        _finite_float(quality_policy.get("lunch_quote_max_age_seconds"), 7_200)
        or 7_200
    )
    closed_ttl = (
        _finite_float(quality_policy.get("closed_quote_max_age_seconds"), 259_200)
        or 259_200
    )
    # Holdings/cash/debt are transaction state, not market bars. Once a new
    # trading session starts, yesterday's broker snapshot can no longer prove
    # the quantities are current. Keep this threshold independent from the
    # daily-bar freshness allowance (which normally permits one session).
    maximum_account_lag = max(
        0,
        int(
            _finite_float(
                quality_policy.get("account_snapshot_max_trading_day_lag"), 0
            )
            or 0
        ),
    )
    account_date = _parse_date(portfolio.get("as_of"))
    account_lag = _business_day_lag(account_date, now.date())
    account_stale = account_lag is None or account_lag > maximum_account_lag

    market_status = quotes_payload.get("market_status") or {}
    market_phase = str(market_status.get("state") or "unknown").lower()
    # Lunch is a scheduled halt: the morning close is the current observable
    # price until 13:00 and must not be judged by the continuous-auction TTL.
    # The quote layer still marks explicit source failures/expired caches.
    market_open = market_phase in {"open", "auction"}
    quote_age_limit = (
        hard_ttl
        if market_open
        else lunch_ttl
        if market_phase == "lunch"
        else closed_ttl
    )
    quote_age = _finite_float(market_status.get("quote_age_seconds"))
    if quote_age is None:
        generated = _parse_datetime(quotes_payload.get("generated_at"))
        quote_age = (now - generated).total_seconds() if generated else None
    payload_cache = quotes_payload.get("cache") or {}
    feed_hard_stale = bool(
        market_status.get("feed_stale")
        or payload_cache.get("expired")
        or quote_age is None
        or quote_age < 0
        or quote_age > quote_age_limit
    )
    missing_quotes = sorted(
        row["thscode"] for row in positions if row.get("quote_status") == "missing"
    )
    stale_quotes = sorted(
        row["thscode"] for row in positions if row.get("quote_status") == "stale"
    )
    live_quotes = len(positions) - len(missing_quotes) - len(stale_quotes)
    quote_score = max(0.0, live_quotes / len(positions)) if positions else 0.0
    if feed_hard_stale or not quotes_payload.get("ok", True):
        quote_score = 0.0
    history_score = _finite_float(history.get("confidence"), 0.0) or 0.0
    account_score = 0.0 if account_stale else 1.0
    score = 0.35 * account_score + 0.35 * quote_score + 0.30 * history_score

    critical: list[str] = []
    warnings: list[str] = list(history.get("warnings") or [])
    if account_stale:
        critical.append("账户持仓快照已跨交易日，无法确认当前数量/现金/负债")
    if missing_quotes:
        critical.append(f"缺少 {len(missing_quotes)} 个持仓的实时行情")
    if stale_quotes or feed_hard_stale:
        critical.append("持仓行情陈旧或实时行情源已超过硬过期阈值")
    if not quotes_payload.get("ok", True):
        critical.append("行情载荷标记为不可用")
    if history.get("status") == "UNAVAILABLE":
        critical.append("历史风险层覆盖或共同样本不足")
    if history.get("errors"):
        warnings.extend(history["errors"])

    minimum_layer = _finite_float(quality_policy.get("minimum_layer_confidence"), 0.7) or 0.7
    if critical or score < minimum_layer:
        status = "CRITICAL"
    elif score < 0.9 or history.get("status") != "FULL":
        status = "DEGRADED"
    else:
        status = "VERIFIED"
    return {
        "status": status,
        "score": round(max(0.0, min(1.0, score)), 4),
        "confidence": round(max(0.0, min(1.0, score)), 4),
        "account_snapshot": {
            "as_of": portfolio.get("as_of"),
            "trading_day_lag": account_lag,
            "maximum_lag": maximum_account_lag,
            "stale": account_stale,
            "verified": not account_stale,
            "current_verified": not account_stale,
        },
        "quotes": {
            "coverage_count": live_quotes,
            "position_count": len(positions),
            "coverage_pct": round(live_quotes / len(positions) * 100, 4) if positions else 0.0,
            "missing_positions": missing_quotes,
            "stale_positions": stale_quotes,
            "quote_age_seconds": _rounded(quote_age, 2),
            "hard_stale_seconds": hard_ttl,
            "freshness_limit_seconds": _rounded(quote_age_limit, 2),
            "market_phase": market_phase,
            "feed_hard_stale": feed_hard_stale,
        },
        "history": {
            "status": history.get("status"),
            "confidence": history.get("confidence"),
            "model_coverage_pct": history.get("model_coverage_pct"),
            "portfolio_return_sessions": history.get("portfolio_return_sessions"),
            "as_of_session": history.get("as_of_session"),
        },
        "critical_failures": list(dict.fromkeys(critical)),
        "warnings": list(dict.fromkeys(str(x) for x in warnings if x)),
    }


def _actions_for(
    state: str,
    metrics: dict[str, Any],
    breaches: list[dict[str, Any]],
    data_quality: dict[str, Any],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    account_quality = (
        data_quality.get("account_snapshot")
        if isinstance(data_quality.get("account_snapshot"), dict)
        else {}
    )
    sizing_verified = bool(
        state != "DATA_UNVERIFIED"
        and account_quality.get("verified") is True
        and account_quality.get("current_verified") is True
        and account_quality.get("stale") is False
    )
    if data_quality.get("critical_failures"):
        actions.append(
            {
                "priority": 1,
                "kind": "FREEZE_NEW_BUYS",
                "title": "暂停新增买入，先恢复数据可验证性",
                "detail": "；".join(data_quality["critical_failures"]),
                "symbols": sorted(
                    set(
                        (data_quality.get("quotes") or {}).get("missing_positions", [])
                        + (data_quality.get("quotes") or {}).get("stale_positions", [])
                    )
                ),
            }
        )

    breach_codes = {row["code"] for row in breaches}
    if breach_codes & {"GROSS_EXPOSURE", "DEBT", "MINIMUM_CASH"}:
        if sizing_verified:
            detail = (
                f"按当前 NAV 口径，敞口超限需至少降低约 "
                f"{metrics.get('risk_reduction_required_cny', 0):,.0f} 元；"
                f"负债为 {metrics.get('debt_cny', 0):,.0f} 元。实际执行需按账户分别核对可用数量。"
            )
        else:
            detail = (
                "账户或风险数据尚未核验，不能计算精确减仓金额、目标权重或买卖股数；"
                "这里只保留降低敞口、补充现金缓冲和降低负债的风险方向。"
            )
        actions.append(
            {
                "priority": 1,
                "kind": "REDUCE_EXPOSURE",
                "title": "仅减仓：先恢复现金缓冲并降低负债",
                "detail": detail,
                "symbols": [row["thscode"] for row in positions[:5]],
            }
        )

    concentration_symbols = [
        row["metric"] for row in breaches if row["code"] == "SINGLE_POSITION"
    ]
    if breach_codes & {"SINGLE_POSITION", "TOP5_CONCENTRATION", "THEME_CONCENTRATION"}:
        actions.append(
            {
                "priority": 2,
                "kind": "DIVERSIFY_CONCENTRATION",
                "title": "压降单票、前五大或主题集中度",
                "detail": "优先处理风险贡献高且权重超限的持仓，不用补仓摊低成本代替风险控制。",
                "symbols": concentration_symbols or [row["thscode"] for row in positions[:5]],
            }
        )

    if breach_codes & {
        "ANNUALIZED_VOLATILITY",
        "DAILY_ES_97_5",
        "MAX_DRAWDOWN_PROXY",
        "STRESS_LOSS",
    }:
        actions.append(
            {
                "priority": 2,
                "kind": "CUT_TAIL_RISK",
                "title": "降低尾部风险暴露",
                "detail": (
                    f"最坏压力情景损失 {metrics.get('worst_5d_stress_loss_pct')}%，"
                    f"日 ES97.5 {metrics.get('daily_es_97_5_pct')}%。"
                    "先减高风险贡献仓位，再重新计算。"
                ),
                "symbols": [
                    row["thscode"]
                    for row in sorted(
                        positions,
                        key=lambda item: _finite_float(item.get("risk_contribution_pct"), -math.inf)
                        or -math.inf,
                        reverse=True,
                    )[:5]
                ],
            }
        )

    if not actions and state == "NORMAL":
        actions.append(
            {
                "priority": 3,
                "kind": "MAINTAIN",
                "title": "当前风险预算内，保持观察",
                "detail": "新增交易前更新账户持仓并重新运行风险快照；该状态不代表未来不会亏损。",
                "symbols": [],
            }
        )
    return actions


def _previous_weekday(value: date) -> date:
    cursor = value - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _expected_completed_session(quotes_payload: dict[str, Any], now: datetime) -> str | None:
    """Best available completed-session date without treating a partial bar as final."""

    status = quotes_payload.get("market_status") or {}
    state = str(status.get("state") or "")
    quote_datetime = _parse_datetime(status.get("quote_time"))
    quote_date = quote_datetime.date() if quote_datetime else None
    partial_states = {"open", "auction", "lunch"}
    if state in partial_states:
        return _previous_weekday(now.date()).isoformat()
    if quote_date is not None and quote_date <= now.date():
        return quote_date.isoformat()
    if now.weekday() < 5 and now.hour >= 15 and state == "closed":
        return now.date().isoformat()
    return _previous_weekday(now.date()).isoformat()


def _calculate_snapshot(
    portfolio: dict[str, Any],
    policy: dict[str, Any],
    quotes_payload: dict[str, Any],
    *,
    now: datetime | None = None,
    force: bool = False,
    history_loader: Callable[..., dict[str, Any]] = _load_history_for_position,
) -> dict[str, Any]:
    now = (now or _now()).astimezone(CN_TZ)
    configured_positions = _flatten_positions(portfolio)
    mapping = _quote_map(quotes_payload)
    rows: list[dict[str, Any]] = []
    for configured in configured_positions:
        quote = _find_quote(mapping, configured)
        live_price = _finite_float((quote or {}).get("price"))
        missing = quote is None or live_price is None or live_price <= 0
        source_is_snapshot = str((quote or {}).get("data_source") or "") == "持仓快照"
        stale = bool((quote or {}).get("is_stale")) or source_is_snapshot
        price = (
            _finite_float(configured.get("snapshot_price"), 0.0) or 0.0
            if missing
            else float(live_price)
        )
        quote_status = "missing" if missing else ("stale" if stale else "live")
        quantity = int(_finite_float(configured.get("quantity"), 0) or 0)
        rows.append(
            {
                "account_id": configured["account_id"],
                "account_type": configured["account_type"],
                "thscode": configured["thscode"],
                "symbol": configured["symbol"],
                "code": configured["code"],
                "name": configured.get("name") or configured["thscode"],
                "instrument_type": configured.get("instrument_type") or "stock",
                "theme": configured.get("theme") or "unclassified",
                "industry": configured.get("industry") or "",
                "quantity": quantity,
                "available": int(_finite_float(configured.get("available"), 0) or 0),
                "holding_as_of": portfolio.get("as_of"),
                "quantity_source": "account_snapshot",
                "price": round(price, 6),
                "market_value_cny": round(quantity * price, 2),
                "weight_pct": None,
                "quote_status": quote_status,
                "quote_time": (quote or {}).get("quote_time") or "",
                "data_source": (quote or {}).get("data_source") or (
                    "portfolio-snapshot" if missing else "quotes-payload"
                ),
                "history_sessions": 0,
                "history_last_session": None,
                "history_status": "unavailable",
                "annualized_volatility_pct": None,
                "risk_contribution_pct": None,
            }
        )

    cash = sum(float(_finite_float(account.get("cash"), 0.0) or 0.0) for account in portfolio.get("accounts") or [])
    debt = sum(float(_finite_float(account.get("debt"), 0.0) or 0.0) for account in portfolio.get("accounts") or [])
    gross_value = sum(float(row["market_value_cny"]) for row in rows)
    nav = gross_value + cash - debt
    if nav <= 0:
        raise ValueError(f"Non-positive NAV: {nav}")
    for row in rows:
        row["weight_pct"] = round(float(row["market_value_cny"]) / nav * 100, 4)
    rows.sort(key=lambda row: row["market_value_cny"], reverse=True)

    theme_values: dict[str, float] = {}
    for row in rows:
        theme_values[row["theme"]] = theme_values.get(row["theme"], 0.0) + float(row["market_value_cny"])
    themes = sorted(
        (
            {
                "theme": theme,
                "market_value_cny": round(value, 2),
                "weight_pct": round(value / nav * 100, 4),
            }
            for theme, value in theme_values.items()
        ),
        key=lambda row: row["weight_pct"],
        reverse=True,
    )

    market_open = (quotes_payload.get("market_status") or {}).get("state") in {
        "open",
        "auction",
        "lunch",
    }
    expected_session = _expected_completed_session(quotes_payload, now)
    historical_metrics, history, position_history = _historical_metrics(
        rows, policy, market_open, expected_session, now, force, history_loader
    )
    contribution_map = {
        row["thscode"]: row for row in history.get("risk_contributions") or []
    }
    for row in rows:
        detail = position_history.get(row["thscode"]) or {}
        contribution = contribution_map.get(row["thscode"]) or {}
        row["history_sessions"] = detail.get("sessions", 0)
        row["history_last_session"] = detail.get("last_session")
        row["history_status"] = (
            "stale" if detail.get("stale") else ("modeled" if detail.get("modeled") else "short")
        )
        row["annualized_volatility_pct"] = contribution.get("annualized_volatility_pct")
        row["risk_contribution_pct"] = contribution.get("risk_contribution_pct")

    scenarios = _scenario_metrics(rows, nav, policy)
    worst = scenarios[0] if scenarios else None
    limits = policy.get("portfolio_limits") or {}
    gross_limit = _finite_float(limits.get("max_gross_exposure_pct"), 100.0) or 100.0
    metrics: dict[str, Any] = {
        "nav_cny": round(nav, 2),
        "gross_market_value_cny": round(gross_value, 2),
        "gross_exposure_pct": round(gross_value / nav * 100, 4),
        "equity_exposure_pct": round(gross_value / nav * 100, 4),
        "cash_cny": round(cash, 2),
        "cash_pct": round(cash / nav * 100, 4),
        "debt_cny": round(debt, 2),
        "debt_pct": round(debt / nav * 100, 4),
        "largest_position_pct": rows[0]["weight_pct"] if rows else 0.0,
        "largest_position": (
            {key: rows[0][key] for key in ("thscode", "name", "market_value_cny", "weight_pct")}
            if rows
            else None
        ),
        "top5_pct": round(sum(float(row["weight_pct"]) for row in rows[:5]), 4),
        "largest_theme_pct": themes[0]["weight_pct"] if themes else 0.0,
        "largest_theme": themes[0] if themes else None,
        "themes": themes,
        "worst_5d_stress_loss_pct": worst["loss_pct"] if worst else None,
        "worst_stress_scenario": worst["scenario"] if worst else None,
        "stress_scenarios": scenarios,
        "risk_reduction_required_cny": round(
            max(0.0, gross_value - nav * gross_limit / 100.0), 2
        ),
        **historical_metrics,
    }
    data_quality = _data_quality(portfolio, quotes_payload, rows, history, policy, now)
    breaches = _add_limit_breaches(metrics, rows, limits)
    if data_quality["account_snapshot"]["stale"]:
        breaches.insert(
            0,
            _breach(
                "ACCOUNT_SNAPSHOT_STALE",
                "critical",
                "account_snapshot_trading_day_lag",
                data_quality["account_snapshot"]["trading_day_lag"],
                data_quality["account_snapshot"]["maximum_lag"],
                "trading days",
                "账户持仓快照已过期",
            ),
        )
    if data_quality["quotes"]["coverage_count"] < data_quality["quotes"]["position_count"]:
        breaches.insert(
            0,
            _breach(
                "QUOTE_COVERAGE",
                "critical",
                "quote_coverage_pct",
                data_quality["quotes"]["coverage_pct"],
                100,
                "%",
                "实时持仓行情覆盖不足或陈旧",
            ),
        )
    if history["status"] == "UNAVAILABLE":
        breaches.append(
            _breach(
                "HISTORY_COVERAGE",
                "critical",
                "history_confidence",
                history["confidence"] * 100,
                (_finite_float((policy.get("data_quality") or {}).get("minimum_layer_confidence"), 0.7) or 0.7) * 100,
                "%",
                "历史层覆盖或样本不足，统计风险不可验证",
            )
        )

    data_breach_codes = {"ACCOUNT_SNAPSHOT_STALE", "QUOTE_COVERAGE", "HISTORY_COVERAGE"}
    risk_breaches = [row for row in breaches if row["code"] not in data_breach_codes]
    if any(row["code"] in {"GROSS_EXPOSURE", "DEBT"} for row in risk_breaches):
        risk_state = "REDUCE_ONLY_CRITICAL"
    elif risk_breaches:
        risk_state = "RISK_OFF"
    else:
        risk_state = "NORMAL"
    state = "DATA_UNVERIFIED" if data_quality["status"] == "CRITICAL" else risk_state
    new_buy_allowed = state == "NORMAL"
    if (policy.get("data_quality") or {}).get("freeze_new_buys_on_critical_failure", True):
        new_buy_allowed = new_buy_allowed and not data_quality["critical_failures"]

    public_metrics = copy.deepcopy(metrics)
    if state == "DATA_UNVERIFIED":
        # This is an action-sizing result rather than an observed account value.
        # Never expose it when holdings/cash/debt or another required input is
        # unverified: a consumer could otherwise mistake it for an order amount.
        public_metrics.pop("risk_reduction_required_cny", None)

    return {
        "schema_version": 1,
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "policy_version": str(policy.get("policy_version") or ""),
        "ok": state != "DATA_UNVERIFIED",
        "generated_at": now.isoformat(timespec="seconds"),
        "state": state,
        "risk_state": risk_state,
        "new_buy_allowed": bool(new_buy_allowed),
        "data_quality": data_quality,
        "metrics": public_metrics,
        "breaches": breaches,
        "actions": _actions_for(state, metrics, breaches, data_quality, rows),
        "positions": rows,
        "history": history,
        "cache": {
            "source": "computed",
            "fresh": True,
            "stale": False,
            "age_seconds": 0.0,
            "ttl_seconds": RISK_CACHE_TTL_SECONDS,
            "path": str(RISK_CACHE_PATH),
            "error": "",
        },
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _cache_view(snapshot: dict[str, Any], source: str, age: float) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    account_quality = (
        (result.get("data_quality") or {}).get("account_snapshot")
        if isinstance(result.get("data_quality"), dict)
        and isinstance((result.get("data_quality") or {}).get("account_snapshot"), dict)
        else {}
    )
    account_current = bool(
        account_quality.get("verified") is True
        and account_quality.get("current_verified") is True
        and account_quality.get("stale") is False
    )
    if not account_current:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        metrics.pop("risk_reduction_required_cny", None)
        safe_actions: list[dict[str, Any]] = []
        for raw in result.get("actions") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").upper()
            action = {
                key: copy.deepcopy(raw[key])
                for key in ("priority", "kind", "symbols")
                if key in raw
            }
            action["title"] = {
                "FREEZE_NEW_BUYS": "暂停新增买入，先恢复数据可验证性",
                "REDUCE_EXPOSURE": "降低敞口、补充现金缓冲并降低负债",
                "DIVERSIFY_CONCENTRATION": "复核单票、前五大或主题集中度",
                "CUT_TAIL_RISK": "复核尾部风险暴露",
            }.get(kind, "账户数据未核验，仅保留风险方向")
            action["detail"] = "未生成精确调整金额、目标权重或买卖股数。"
            safe_actions.append(action)
        result["actions"] = safe_actions
    result["cache"] = {
        **(result.get("cache") or {}),
        "source": source,
        "fresh": age < RISK_CACHE_TTL_SECONDS,
        "stale": age >= RISK_CACHE_TTL_SECONDS,
        "age_seconds": round(max(0.0, age), 3),
        "ttl_seconds": RISK_CACHE_TTL_SECONDS,
        "path": str(RISK_CACHE_PATH),
        "error": "",
    }
    return result


def _load_disk_cache() -> tuple[dict[str, Any] | None, float]:
    try:
        snapshot = _read_json(RISK_CACHE_PATH)
        generated = _parse_datetime(snapshot.get("generated_at"))
        age = max(0.0, (_now() - generated).total_seconds()) if generated else math.inf
        return snapshot, age
    except Exception:
        return None, math.inf


def _stale_fallback(snapshot: dict[str, Any], source: str, error: str, age: float) -> dict[str, Any]:
    result = copy.deepcopy(snapshot)
    result["ok"] = False
    result["state"] = "DATA_UNVERIFIED"
    result["new_buy_allowed"] = False
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    metrics.pop("risk_reduction_required_cny", None)
    quality = result.setdefault("data_quality", {})
    quality["status"] = "CRITICAL"
    failures = list(quality.get("critical_failures") or [])
    failures.append(f"风险快照刷新失败，保留旧缓存：{error}")
    quality["critical_failures"] = list(dict.fromkeys(failures))
    actions = []
    for raw in result.get("actions") or []:
        if not isinstance(raw, dict):
            continue
        action = copy.deepcopy(raw)
        if str(action.get("kind") or "").upper() == "REDUCE_EXPOSURE":
            action["detail"] = (
                "风险快照刷新失败，不能沿用旧快照计算精确减仓金额、目标权重或买卖股数；"
                "仅保留降低敞口的风险方向。"
            )
        for key in list(action):
            normalized = str(key).lower()
            if any(
                token in normalized
                for token in (
                    "target",
                    "adjustment",
                    "notional",
                    "quantity",
                    "shares",
                    "amount",
                    "proceeds",
                    "weight",
                )
            ):
                action.pop(key, None)
        actions.append(action)
    actions.insert(
        0,
        {
            "priority": 1,
            "kind": "FREEZE_NEW_BUYS",
            "title": "旧风险快照仅供减仓参考",
            "detail": error,
            "symbols": [],
        },
    )
    result["actions"] = actions
    result["cache"] = {
        **(result.get("cache") or {}),
        "source": source,
        "fresh": False,
        "stale": True,
        "age_seconds": None if not math.isfinite(age) else round(max(0.0, age), 3),
        "ttl_seconds": RISK_CACHE_TTL_SECONDS,
        "path": str(RISK_CACHE_PATH),
        "error": error,
        "served_at": _now().isoformat(timespec="seconds"),
    }
    return result


def _empty_failure(
    error: str,
    portfolio: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "portfolio_id": str((portfolio or {}).get("portfolio_id") or ""),
        "policy_version": str((policy or {}).get("policy_version") or ""),
        "ok": False,
        "generated_at": _now().isoformat(timespec="seconds"),
        "state": "DATA_UNVERIFIED",
        "risk_state": "UNKNOWN",
        "new_buy_allowed": False,
        "data_quality": {
            "status": "CRITICAL",
            "score": 0.0,
            "confidence": 0.0,
            "account_snapshot": {},
            "quotes": {},
            "history": {},
            "critical_failures": [error],
            "warnings": [],
        },
        "metrics": {},
        "breaches": [],
        "actions": [
            {
                "priority": 1,
                "kind": "FREEZE_NEW_BUYS",
                "title": "风险数据不可验证，暂停新增买入",
                "detail": error,
                "symbols": [],
            }
        ],
        "positions": [],
        "history": {
            "status": "UNAVAILABLE",
            "as_of_session": None,
            "partial_session_excluded": False,
        },
        "cache": {
            "source": "none",
            "fresh": False,
            "stale": True,
            "age_seconds": None,
            "ttl_seconds": RISK_CACHE_TTL_SECONDS,
            "path": str(RISK_CACHE_PATH),
            "error": error,
        },
    }


def _obtain_quotes_payload() -> dict[str, Any]:
    try:
        from scripts.dashboard_server import get_quotes_payload
    except ImportError:
        from dashboard_server import get_quotes_payload

    payload = get_quotes_payload()
    # Direct CLI startup uses the dashboard's non-blocking cold-start path.
    deadline = time.monotonic() + 10.0
    while not (payload.get("positions") or []) and time.monotonic() < deadline:
        time.sleep(0.2)
        payload = get_quotes_payload()
    return payload


def get_risk_snapshot(
    quotes_payload: dict | None = None,
    force: bool = False,
    portfolio: dict | None = None,
) -> dict:
    """Return the current risk snapshot with memory/disk stale-while-error cache.

    Supplying ``quotes_payload`` avoids a second market-data request. ``force``
    bypasses both risk and history memory caches, but local reproducible histories
    remain the preferred source.  Supplying ``portfolio`` evaluates an explicit
    (user-defined) portfolio instead of ``config/portfolio.json``; in that mode
    no memory/disk cache is read or written so the primary portfolio's cache can
    never be poisoned by another portfolio's snapshot.
    """

    external_portfolio = portfolio is not None
    policy: dict[str, Any] | None = None
    try:
        if not external_portfolio:
            portfolio = _read_json(PORTFOLIO_PATH)
        policy = _read_json(POLICY_PATH)
        if quotes_payload is None:
            if external_portfolio:
                raise ValueError("quotes_payload is required for an explicit portfolio")
            with _CACHE_LOCK:
                cached = _MEMORY_CACHE.get("snapshot")
                age = time.monotonic() - float(_MEMORY_CACHE.get("monotonic") or 0.0)
                if cached is not None and not force and age < RISK_CACHE_TTL_SECONDS:
                    return _cache_view(cached, "memory", age)
            quotes_payload = _obtain_quotes_payload()
        if not isinstance(quotes_payload, dict):
            raise TypeError("quotes_payload must be a dict or None")
        if external_portfolio:
            snapshot = _calculate_snapshot(portfolio, policy, quotes_payload, force=True)
            snapshot["cache"]["input_fingerprint"] = _portfolio_input_fingerprint(
                portfolio, policy, quotes_payload
            )
            snapshot["cache"]["source"] = "computed-external-portfolio"
            return snapshot
        fingerprint = _portfolio_input_fingerprint(portfolio, policy, quotes_payload)
        with _CACHE_LOCK:
            cached = _MEMORY_CACHE.get("snapshot")
            age = time.monotonic() - float(_MEMORY_CACHE.get("monotonic") or 0.0)
            if (
                cached is not None
                and not force
                and fingerprint == _MEMORY_CACHE.get("fingerprint")
                and age < RISK_CACHE_TTL_SECONDS
            ):
                return _cache_view(cached, "memory", age)

        snapshot = _calculate_snapshot(
            portfolio, policy, quotes_payload, force=force
        )
        snapshot["cache"]["input_fingerprint"] = fingerprint
        _atomic_write_json(RISK_CACHE_PATH, snapshot)
        with _CACHE_LOCK:
            _MEMORY_CACHE.update(
                {
                    "snapshot": copy.deepcopy(snapshot),
                    "fingerprint": fingerprint,
                    "monotonic": time.monotonic(),
                }
            )
        return snapshot
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if external_portfolio:
            return _empty_failure(error, portfolio, policy)
        with _CACHE_LOCK:
            cached = _MEMORY_CACHE.get("snapshot")
            age = time.monotonic() - float(_MEMORY_CACHE.get("monotonic") or 0.0)
        if isinstance(cached, dict):
            return _stale_fallback(cached, "stale-memory", error, age)
        disk, disk_age = _load_disk_cache()
        if isinstance(disk, dict):
            return _stale_fallback(disk, "stale-disk", error, disk_age)
        return _empty_failure(error, portfolio, policy)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build a reproducible portfolio risk snapshot")
    parser.add_argument("--quotes-file", type=Path, help="dashboard quotes JSON payload")
    parser.add_argument("--output", type=Path, default=RISK_CACHE_PATH, help="output JSON path")
    parser.add_argument("--force", action="store_true", help="bypass memory caches")
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    args = parser.parse_args()
    quotes = _read_json(args.quotes_file) if args.quotes_file else None
    snapshot = get_risk_snapshot(quotes, force=args.force)
    if args.output.resolve() != RISK_CACHE_PATH.resolve():
        _atomic_write_json(args.output, snapshot)
    print(
        json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            allow_nan=False,
        )
    )
    return 0 if snapshot.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(_main())
