"""Backtest engine for technical signals over a symbol's own history.

Given a sequence of daily OHLCV bars for one symbol, evaluate the signal
rules from ``technical_signal_engine`` at every historical bar (starting
after a warm-up window), then measure forward returns at 1/5/20 sessions
from each trigger.

The result is a per-rule statistics table:
    ``trigger_count``   number of times the rule fired
    ``wins_Xd``         count of triggers where forward return > 0
    ``win_rate_Xd``     wins / triggers with Xd horizon
    ``avg_return_Xd``   mean forward return at Xd (decimal, e.g. 0.023 = 2.3%)
    ``median_return_Xd``
    ``max_drawdown_Xd`` worst forward return at Xd (negative)
    ``coverage``        fraction of evaluable bars that had a trigger

The engine is pure pandas — no DuckDB, no network. Callers pass the same
daily bars the dashboard already fetches (Tencent qfq), so the backtest
sees exactly what the live signal view saw on each historical bar.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Mapping, Sequence

from scripts.technical_indicators import compute_indicators
from scripts.technical_signal_engine import evaluate_signals

BACKTEST_VERSION = "2026-09-18.1"
BACKTEST_SCHEMA_VERSION = "technical-signal-backtest.v1"

DEFAULT_HORIZONS = (1, 5, 20)
DEFAULT_MIN_BARS = 60  # need at least 60 bars before trusting a signal
DEFAULT_WARMUP = 26    # MACD slow=26 is the longest warm-up


def _forward_return(closes: Sequence[float], index: int, horizon: int) -> float | None:
    """Return (close[index + horizon] / close[index]) - 1, or None if OOB."""

    future_index = index + horizon
    if future_index >= len(closes):
        return None
    base = closes[index]
    future = closes[future_index]
    if base is None or future is None or base <= 0:
        return None
    return (future / base) - 1.0


def _stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {
            "trigger_count": 0,
            "win_rate": None,
            "avg_return": None,
            "median_return": None,
            "max_drawdown": None,
        }
    wins = sum(1 for r in returns if r > 0)
    return {
        "trigger_count": len(returns),
        "win_rate": round(wins / len(returns), 4),
        "avg_return": round(sum(returns) / len(returns), 5),
        "median_return": round(median(returns), 5),
        "max_drawdown": round(min(returns), 5),
    }


def backtest_signals(
    bars: Sequence[Mapping[str, Any]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_bars: int = DEFAULT_MIN_BARS,
    warmup: int = DEFAULT_WARMUP,
) -> dict[str, Any]:
    """Walk forward through ``bars`` and record per-rule forward returns.

    Parameters
    ----------
    bars:
        Daily OHLCV bars in ascending time order.  Must have at least
        ``min_bars`` entries for any signal to be evaluated.
    horizons:
        Forward-return horizons in sessions (default 1/5/20).
    min_bars:
        Minimum total bars required to run the backtest.
    warmup:
        Bars to skip at the front (indicator warm-up).

    Returns
    -------
    dict with ``schema_version``, ``engine_version``, ``bar_count``,
    ``evaluated_from``, ``evaluated_to``, ``horizons``, and ``rules`` —
    a mapping of rule name → per-horizon statistics.
    """

    bar_list = list(bars)
    if len(bar_list) < min_bars:
        return {
            "ok": False,
            "error": f"insufficient bars: {len(bar_list)} < {min_bars}",
            "schema_version": BACKTEST_SCHEMA_VERSION,
            "engine_version": BACKTEST_VERSION,
        }

    closes = [b.get("close") for b in bar_list]
    # Pre-compute indicators on the full series once; sub-slices would
    # change EMA/MACD warm-up behaviour and yield inconsistent values.
    full_bundle = compute_indicators(bar_list)

    # rule → horizon → list of forward returns
    returns_by_rule: dict[str, dict[int, list[float]]] = {}
    evaluable_bars = 0

    for index in range(warmup, len(bar_list)):
        # Skip bars whose close is missing.
        if closes[index] is None:
            continue
        # Evaluate on the slice ending at index (inclusive).
        sub_bars = bar_list[: index + 1]
        # Slice the indicator bundle to match the same length.
        sub_bundle = _slice_bundle(full_bundle, index + 1)
        result = evaluate_signals(sub_bars, indicator_bundle=sub_bundle)
        evaluable_bars += 1
        for signal in result["signals"]:
            rule = signal["rule"]
            horizon_returns = returns_by_rule.setdefault(rule, {h: [] for h in horizons})
            for horizon in horizons:
                fwd = _forward_return(closes, index, horizon)
                if fwd is not None:
                    horizon_returns[horizon].append(fwd)

    rules_payload: dict[str, Any] = {}
    for rule, horizon_returns in sorted(returns_by_rule.items()):
        per_horizon: dict[str, Any] = {}
        for horizon, returns in sorted(horizon_returns.items()):
            per_horizon[f"{horizon}d"] = _stats(returns)
        # Top-level count = max across horizons (they differ only by tail OOB).
        trigger_count = max((len(v) for v in horizon_returns.values()), default=0)
        rules_payload[rule] = {
            "trigger_count": trigger_count,
            "coverage": round(trigger_count / evaluable_bars, 4) if evaluable_bars else 0.0,
            "horizons": per_horizon,
        }

    return {
        "ok": True,
        "schema_version": BACKTEST_SCHEMA_VERSION,
        "engine_version": BACKTEST_VERSION,
        "bar_count": len(bar_list),
        "evaluated_from": bar_list[warmup].get("time") if warmup < len(bar_list) else None,
        "evaluated_to": bar_list[-1].get("time") if bar_list else None,
        "evaluable_bars": evaluable_bars,
        "horizons": list(horizons),
        "rules": rules_payload,
    }


def _slice_bundle(bundle: Mapping[str, Any], length: int) -> dict[str, Any]:
    """Slice every series in a ``compute_indicators`` bundle to ``length``.

    Used by the walk-forward loop so each step sees only the indicators that
    would have been computable at that point in time (avoids look-ahead bias
    from trailing windows like vol_ma or MA over the full series).
    """

    indicators = bundle.get("indicators") or {}
    sliced_indicators: dict[str, Any] = {}
    for group_name, group in indicators.items():
        if not isinstance(group, Mapping):
            continue
        sliced_group: dict[str, Any] = {}
        for series_name, series in group.items():
            if isinstance(series, list):
                sliced_group[series_name] = series[:length]
            else:
                sliced_group[series_name] = series
        sliced_indicators[group_name] = sliced_group
    return {
        "length": length,
        "indicators": sliced_indicators,
        # Latest is irrelevant for historical evaluation; kept for shape.
        "latest": {},
    }


__all__ = [
    "BACKTEST_VERSION",
    "BACKTEST_SCHEMA_VERSION",
    "DEFAULT_HORIZONS",
    "backtest_signals",
]
