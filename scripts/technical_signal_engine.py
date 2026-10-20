"""Technical indicator signal engine for A-share positions and watchlist.

Consumes OHLCV bars (or a pre-computed indicator bundle from
``scripts.technical_indicators.compute_indicators``) and emits discrete
trading signals following Chinese-software conventions:

  * MACD golden/dead cross (DIF crossing DEA)
  * RSI overbought/oversold (>80 / <20 for short window, >70 / <30 for long)
  * KDJ overbought/oversold + K/D cross
  * BOLL upper break / lower break / mid-line reclaim
  * MA multi-head / empty-head alignment (ma5>ma10>ma20 or reverse)
  * Price breaking above MA20 with volume confirmation
  * ATR-based stop-loss trigger (close < highest_close_N - 2*ATR)

Each signal is a dict with:
    ``rule``       unique rule name (e.g. ``macd_golden_cross``)
    ``direction``  ``bullish`` | ``bearish`` | ``neutral``
    ``severity``   ``info`` | ``warning`` | ``critical``
    ``confidence`` 0.0-1.0, boosted by multi-indicator resonance
    ``message``    human-readable Chinese explanation
    ``evidence``   supporting values (prices, indicator values)
    ``rule_version`` engine version for reproducibility

The engine is pure: no I/O, no global state, no time-of-day checks.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from scripts.technical_indicators import (
    DEFAULT_MA_WINDOWS,
    DEFAULT_RSI_WINDOWS,
    compute_indicators,
)

ENGINE_VERSION = "2026-09-18.1"
SIGNAL_SCHEMA_VERSION = "technical-signal.v1"


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------


def _last_two(series: Sequence[float | None]) -> tuple[float | None, float | None]:
    """Return the last two non-None values as (prev, current)."""
    values = [v for v in series if v is not None]
    if len(values) < 2:
        return None, None
    return values[-2], values[-1]


def _last(series: Sequence[float | None]) -> float | None:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _crossed_above(prev_a: float | None, cur_a: float | None, prev_b: float | None, cur_b: float | None) -> bool:
    if None in (prev_a, cur_a, prev_b, cur_b):
        return False
    assert prev_a is not None and cur_a is not None and prev_b is not None and cur_b is not None
    return prev_a <= prev_b and cur_a > cur_b


def _crossed_below(prev_a: float | None, cur_a: float | None, prev_b: float | None, cur_b: float | None) -> bool:
    if None in (prev_a, cur_a, prev_b, cur_b):
        return False
    assert prev_a is not None and cur_a is not None and prev_b is not None and cur_b is not None
    return prev_a >= prev_b and cur_a < cur_b


def _signal(
    rule: str,
    direction: str,
    severity: str,
    message: str,
    *,
    confidence: float = 0.5,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "rule": rule,
        "direction": direction,
        "severity": severity,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "message": message,
        "evidence": dict(evidence or {}),
        "rule_version": ENGINE_VERSION,
    }


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def _macd_signals(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    macd_block = bundle.get("macd") or {}
    dif = macd_block.get("dif") or []
    dea = macd_block.get("dea") or []
    prev_dif, cur_dif = _last_two(dif)
    prev_dea, cur_dea = _last_two(dea)
    signals: list[dict[str, Any]] = []
    if _crossed_above(prev_dif, cur_dif, prev_dea, cur_dea):
        signals.append(_signal(
            "macd_golden_cross",
            "bullish",
            "warning",
            f"MACD 金叉（DIF {cur_dif:.3f} 上穿 DEA {cur_dea:.3f}）",
            confidence=0.65,
            evidence={"dif": cur_dif, "dea": cur_dea},
        ))
    elif _crossed_below(prev_dif, cur_dif, prev_dea, cur_dea):
        signals.append(_signal(
            "macd_dead_cross",
            "bearish",
            "warning",
            f"MACD 死叉（DIF {cur_dif:.3f} 下穿 DEA {cur_dea:.3f}）",
            confidence=0.65,
            evidence={"dif": cur_dif, "dea": cur_dea},
        ))
    return signals


def _rsi_signals(bundle: Mapping[str, Any], rsi_windows: Sequence[int]) -> list[dict[str, Any]]:
    rsi_block = bundle.get("rsi") or {}
    signals: list[dict[str, Any]] = []
    for window in rsi_windows:
        series = rsi_block.get(f"rsi{window}") or []
        current = _last(series)
        if current is None:
            continue
        # Short-window thresholds are tighter (80/20), long-window 70/30.
        if window <= 6:
            overbought, oversold = 80.0, 20.0
        else:
            overbought, oversold = 70.0, 30.0
        if current >= overbought:
            signals.append(_signal(
                f"rsi{window}_overbought",
                "bearish",
                "info" if window > 6 else "warning",
                f"RSI{window} {current:.1f} 进入超买区（≥{overbought:.0f}）",
                confidence=0.55,
                evidence={"rsi": current, "window": window},
            ))
        elif current <= oversold:
            signals.append(_signal(
                f"rsi{window}_oversold",
                "bullish",
                "info" if window > 6 else "warning",
                f"RSI{window} {current:.1f} 进入超卖区（≤{oversold:.0f}）",
                confidence=0.55,
                evidence={"rsi": current, "window": window},
            ))
    return signals


def _kdj_signals(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    kdj_block = bundle.get("kdj") or {}
    k_series = kdj_block.get("k") or []
    d_series = kdj_block.get("d") or []
    j_series = kdj_block.get("j") or []
    prev_k, cur_k = _last_two(k_series)
    prev_d, cur_d = _last_two(d_series)
    cur_j = _last(j_series)
    signals: list[dict[str, Any]] = []
    if cur_k is not None and cur_k >= 80:
        signals.append(_signal(
            "kdj_overbought",
            "bearish",
            "info",
            f"KDJ 超买（K={cur_k:.1f} D={cur_d:.1f}）",
            confidence=0.5,
            evidence={"k": cur_k, "d": cur_d, "j": cur_j},
        ))
    elif cur_k is not None and cur_k <= 20:
        signals.append(_signal(
            "kdj_oversold",
            "bullish",
            "info",
            f"KDJ 超卖（K={cur_k:.1f} D={cur_d:.1f}）",
            confidence=0.5,
            evidence={"k": cur_k, "d": cur_d, "j": cur_j},
        ))
    if _crossed_above(prev_k, cur_k, prev_d, cur_d):
        signals.append(_signal(
            "kdj_golden_cross",
            "bullish",
            "info",
            f"KDJ 金叉（K {cur_k:.1f} 上穿 D {cur_d:.1f}）",
            confidence=0.5,
            evidence={"k": cur_k, "d": cur_d},
        ))
    elif _crossed_below(prev_k, cur_k, prev_d, cur_d):
        signals.append(_signal(
            "kdj_dead_cross",
            "bearish",
            "info",
            f"KDJ 死叉（K {cur_k:.1f} 下穿 D {cur_d:.1f}）",
            confidence=0.5,
            evidence={"k": cur_k, "d": cur_d},
        ))
    return signals


def _boll_signals(
    bundle: Mapping[str, Any],
    close: float | None,
) -> list[dict[str, Any]]:
    if close is None:
        return []
    boll_block = bundle.get("boll") or {}
    upper = _last(boll_block.get("upper") or [])
    mid = _last(boll_block.get("mid") or [])
    lower = _last(boll_block.get("lower") or [])
    signals: list[dict[str, Any]] = []
    if upper is not None and close > upper:
        signals.append(_signal(
            "boll_upper_break",
            "bullish",
            "warning",
            f"收盘 {close:.2f} 突破 BOLL 上轨 {upper:.2f}",
            confidence=0.55,
            evidence={"close": close, "upper": upper},
        ))
    if lower is not None and close < lower:
        signals.append(_signal(
            "boll_lower_break",
            "bearish",
            "warning",
            f"收盘 {close:.2f} 跌破 BOLL 下轨 {lower:.2f}",
            confidence=0.55,
            evidence={"close": close, "lower": lower},
        ))
    return signals


def _ma_alignment_signals(
    bundle: Mapping[str, Any],
    close: float | None,
) -> list[dict[str, Any]]:
    ma_block = bundle.get("ma") or {}
    ma5 = _last(ma_block.get("ma5") or [])
    ma10 = _last(ma_block.get("ma10") or [])
    ma20 = _last(ma_block.get("ma20") or [])
    ma60 = _last(ma_block.get("ma60") or [])
    signals: list[dict[str, Any]] = []
    if close is None or ma5 is None or ma10 is None or ma20 is None:
        return signals
    if ma5 > ma10 > ma20:
        extra = "且站上 MA60" if (ma60 is not None and ma20 > ma60) else ""
        signals.append(_signal(
            "ma_bullish_alignment",
            "bullish",
            "info",
            f"均线多头排列 MA5({ma5:.2f})>MA10({ma10:.2f})>MA20({ma20:.2f}){extra}",
            confidence=0.6,
            evidence={"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        ))
    elif ma5 < ma10 < ma20:
        signals.append(_signal(
            "ma_bearish_alignment",
            "bearish",
            "info",
            f"均线空头排列 MA5({ma5:.2f})<MA10({ma10:.2f})<MA20({ma20:.2f})",
            confidence=0.6,
            evidence={"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
        ))
    return signals


def _ma20_breakout_signals(
    bundle: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Price crossing above MA20 with volume confirmation.

    Volume confirmation = current volume > 1.5 × vol_ma5 (last 5 days average).
    """

    if len(bars) < 2:
        return []
    ma_block = bundle.get("ma") or {}
    vol_block = bundle.get("vol_ma") or {}
    ma20_series = ma_block.get("ma20") or []
    vol_ma5_series = vol_block.get("vol_ma5") or []
    prev_ma20, cur_ma20 = _last_two(ma20_series)
    prev_close = bars[-2].get("close")
    cur_close = bars[-1].get("close")
    cur_volume = bars[-1].get("volume")
    vol_ma5 = _last(vol_ma5_series)
    if None in (prev_ma20, cur_ma20, prev_close, cur_close):
        return []
    assert prev_ma20 is not None and cur_ma20 is not None
    assert prev_close is not None and cur_close is not None
    signals: list[dict[str, Any]] = []
    crossed_up = prev_close <= prev_ma20 and cur_close > cur_ma20
    crossed_down = prev_close >= prev_ma20 and cur_close < cur_ma20
    if crossed_up:
        vol_confirm = (
            cur_volume is not None and vol_ma5 is not None and cur_volume > 1.5 * vol_ma5
        )
        confidence = 0.7 if vol_confirm else 0.5
        severity = "warning" if vol_confirm else "info"
        suffix = "（放量确认）" if vol_confirm else "（量能不足）"
        signals.append(_signal(
            "ma20_breakout_up",
            "bullish",
            severity,
            f"收盘 {cur_close:.2f} 上穿 MA20 {cur_ma20:.2f}{suffix}",
            confidence=confidence,
            evidence={
                "close": cur_close,
                "ma20": cur_ma20,
                "volume": cur_volume,
                "vol_ma5": vol_ma5,
                "volume_confirmed": vol_confirm,
            },
        ))
    elif crossed_down:
        signals.append(_signal(
            "ma20_breakout_down",
            "bearish",
            "warning",
            f"收盘 {cur_close:.2f} 跌破 MA20 {cur_ma20:.2f}",
            confidence=0.65,
            evidence={"close": cur_close, "ma20": cur_ma20},
        ))
    return signals


def _atr_stop_signal(
    bundle: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    lookback: int = 20,
) -> list[dict[str, Any]]:
    """ATR trailing stop: close < highest_close_N - 2 * ATR14."""

    if len(bars) < lookback + 1:
        return []
    atr_series = (bundle.get("atr") or {}).get("atr14") or []
    atr_value = _last(atr_series)
    if atr_value is None or atr_value <= 0:
        return []
    closes = [b.get("close") for b in bars[-lookback:]]
    closes = [c for c in closes if c is not None]
    if len(closes) < lookback:
        return []
    highest = max(closes[:-1])  # exclude current bar
    current = closes[-1]
    threshold = highest - 2.0 * atr_value
    if current < threshold:
        return [_signal(
            "atr_trailing_stop",
            "bearish",
            "critical",
            f"ATR 移动止损触发：收盘 {current:.2f} < {lookback}日最高 {highest:.2f} − 2×ATR({atr_value:.2f}) = {threshold:.2f}",
            confidence=0.7,
            evidence={
                "close": current,
                "highest_close": highest,
                "atr14": atr_value,
                "threshold": threshold,
                "lookback_days": lookback,
            },
        )]
    return []


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def evaluate_signals(
    bars: Sequence[Mapping[str, Any]],
    *,
    indicator_bundle: Mapping[str, Any] | None = None,
    ma_windows: Sequence[int] = DEFAULT_MA_WINDOWS,
    rsi_windows: Sequence[int] = DEFAULT_RSI_WINDOWS,
) -> dict[str, Any]:
    """Evaluate all signal rules against a bar series.

    Parameters
    ----------
    bars:
        OHLCV bars, ascending by time.
    indicator_bundle:
        Pre-computed ``compute_indicators`` result. When ``None``, it is
        computed internally with all supported indicators.
    ma_windows / rsi_windows:
        Custom windows used only when computing the bundle internally.

    Returns
    -------
    dict with:
        ``schema_version``, ``engine_version``, ``bar_count``,
        ``signals`` (list, newest first by severity), ``summary`` counts,
        ``latest_close``.
    """

    if not bars:
        return {
            "schema_version": SIGNAL_SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "bar_count": 0,
            "signals": [],
            "summary": {"total": 0, "bullish": 0, "bearish": 0, "critical": 0},
            "latest_close": None,
        }

    bundle = indicator_bundle or compute_indicators(
        bars, indicators=None, ma_windows=ma_windows, rsi_windows=rsi_windows
    )
    bundle_indicators = bundle.get("indicators") or {}
    latest_close = bars[-1].get("close")

    signals: list[dict[str, Any]] = []
    signals.extend(_macd_signals(bundle_indicators))
    signals.extend(_rsi_signals(bundle_indicators, rsi_windows))
    signals.extend(_kdj_signals(bundle_indicators))
    signals.extend(_boll_signals(bundle_indicators, latest_close))
    signals.extend(_ma_alignment_signals(bundle_indicators, latest_close))
    signals.extend(_ma20_breakout_signals(bundle_indicators, bars))
    signals.extend(_atr_stop_signal(bundle_indicators, bars))

    # Multi-indicator resonance boost: when >=3 bullish or >=3 bearish signals
    # agree, promote severity of the strongest one and add +0.1 confidence.
    bullish = [s for s in signals if s["direction"] == "bullish"]
    bearish = [s for s in signals if s["direction"] == "bearish"]
    if len(bullish) >= 3:
        for s in bullish:
            s["confidence"] = round(min(1.0, s["confidence"] + 0.1), 3)
    if len(bearish) >= 3:
        for s in bearish:
            s["confidence"] = round(min(1.0, s["confidence"] + 0.1), 3)

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    signals.sort(key=lambda s: (severity_rank.get(s["severity"], 3), -s["confidence"]))

    summary = {
        "total": len(signals),
        "bullish": sum(1 for s in signals if s["direction"] == "bullish"),
        "bearish": sum(1 for s in signals if s["direction"] == "bearish"),
        "critical": sum(1 for s in signals if s["severity"] == "critical"),
    }

    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "bar_count": len(bars),
        "signals": signals,
        "summary": summary,
        "latest_close": latest_close,
    }


__all__ = [
    "ENGINE_VERSION",
    "SIGNAL_SCHEMA_VERSION",
    "evaluate_signals",
]
