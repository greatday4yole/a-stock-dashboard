"""Technical indicator computations for A-share K-line series.

Pure pandas/numpy implementation — no external TA library — so the
conventions match international practice (Wilder RSI, MACD 12/26/9,
Bollinger 20/2, standard KDJ 9/3/3, ATR 14). All functions take a list of
OHLCV bars (``{time, open, high, low, close, volume}``) and return
arrays aligned with the input length (leading ``None`` for warm-up windows).

The module is intentionally side-effect free so it can be imported by both
the dashboard server and batch scripts without pulling in any state.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

Bar = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Convert a list of OHLCV bars into a sorted DataFrame.

    Raises ``ValueError`` if the input is empty or lacks required columns.
    Missing/invalid numeric values become ``NaN`` so downstream indicators
    propagate ``None`` instead of crashing.
    """

    if not bars:
        raise ValueError("bars must be a non-empty sequence")
    rows: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        rows.append(
            {
                "time": str(bar.get("time") or ""),
                "open": _safe_float(bar.get("open")),
                "high": _safe_float(bar.get("high")),
                "low": _safe_float(bar.get("low")),
                "close": _safe_float(bar.get("close")),
                "volume": _safe_float(bar.get("volume")),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty or frame["close"].notna().sum() == 0:
        raise ValueError("bars contain no usable close prices")
    return frame.reset_index(drop=True)


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if np.isnan(number) or np.isinf(number):
        return float("nan")
    return number


def _series_to_list(series: pd.Series, digits: int = 4) -> list[float | None]:
    """Round and convert a series to ``float | None`` (NaN → None)."""

    out: list[float | None] = []
    for value in series:
        if pd.isna(value):
            out.append(None)
        else:
            out.append(round(float(value), digits))
    return out


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------


def sma(close: pd.Series, window: int) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    return close.rolling(window, min_periods=window).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    if span <= 0:
        raise ValueError("span must be positive")
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


# ---------------------------------------------------------------------------
# MACD (12/26/9, international convention)
# ---------------------------------------------------------------------------


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, pd.Series]:
    """Return ``{dif, dea, hist}`` — DIF/DEA naming matches Chinese software.

    ``hist = 2 * (dif - dea)`` follows the Chinese convention (通达信/同花顺),
    which is exactly twice the Western ``macd - signal`` histogram.
    """

    if fast >= slow:
        raise ValueError("fast span must be < slow span")
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = (dif - dea) * 2.0
    return {"dif": dif, "dea": dea, "hist": hist}


# ---------------------------------------------------------------------------
# RSI (Wilder smoothing)
# ---------------------------------------------------------------------------


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When avg_loss is 0 and avg_gain > 0, RSI is 100 by convention.
    out = out.where(~((avg_loss == 0.0) & (avg_gain > 0.0)), 100.0)
    return out.clip(0.0, 100.0)


# ---------------------------------------------------------------------------
# KDJ (9,3,3) — Chinese-standard stochastic
# ---------------------------------------------------------------------------


def kdj(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> dict[str, pd.Series]:
    """Return ``{k, d, j}``. K and D use EMA-style smoothing with alpha=1/n.

    Initial K/D values default to 50 as in Chinese trading software.
    """

    if window <= 0:
        raise ValueError("window must be positive")
    lowest = low.rolling(window, min_periods=window).min()
    highest = high.rolling(window, min_periods=window).max()
    rsv = (close - lowest) / (highest - lowest).replace(0.0, np.nan) * 100.0
    rsv = rsv.fillna(50.0)

    k_values: list[float] = []
    d_values: list[float] = []
    k_prev, d_prev = 50.0, 50.0
    for value in rsv:
        k_current = (k_smooth - 1) / k_smooth * k_prev + 1.0 / k_smooth * float(value)
        d_current = (d_smooth - 1) / d_smooth * d_prev + 1.0 / d_smooth * k_current
        k_values.append(k_current)
        d_values.append(d_current)
        k_prev, d_prev = k_current, d_current
    k_series = pd.Series(k_values, index=close.index)
    d_series = pd.Series(d_values, index=close.index)
    j_series = 3.0 * k_series - 2.0 * d_series
    # Warm-up rows (before `window`) carry no information — mask them.
    warmup = close.index[: window - 1]
    k_series.loc[warmup] = np.nan
    d_series.loc[warmup] = np.nan
    j_series.loc[warmup] = np.nan
    return {"k": k_series, "d": d_series, "j": j_series}


# ---------------------------------------------------------------------------
# Bollinger Bands (20, 2)
# ---------------------------------------------------------------------------


def bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> dict[str, pd.Series]:
    if window <= 0:
        raise ValueError("window must be positive")
    mid = sma(close, window)
    std = close.rolling(window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return {"upper": upper, "mid": mid, "lower": lower}


# ---------------------------------------------------------------------------
# ATR (Wilder, 14)
# ---------------------------------------------------------------------------


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    if window <= 0:
        raise ValueError("window must be positive")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


# ---------------------------------------------------------------------------
# OBV (On-Balance Volume)
# ---------------------------------------------------------------------------


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume.fillna(0.0)).cumsum()


# ---------------------------------------------------------------------------
# Public orchestration API
# ---------------------------------------------------------------------------

SUPPORTED_INDICATORS = frozenset(
    {"ma", "ema", "macd", "rsi", "kdj", "boll", "atr", "obv", "vol_ma"}
)

DEFAULT_MA_WINDOWS = (5, 10, 20, 60)
DEFAULT_RSI_WINDOWS = (6, 12, 24)


def compute_indicators(
    bars: Sequence[Bar],
    indicators: Iterable[str] | None = None,
    *,
    ma_windows: Sequence[int] = DEFAULT_MA_WINDOWS,
    rsi_windows: Sequence[int] = DEFAULT_RSI_WINDOWS,
) -> dict[str, Any]:
    """Compute a bundle of indicators over ``bars``.

    Parameters
    ----------
    bars:
        Sequence of OHLCV bars (``time/open/high/low/close/volume``).
    indicators:
        Names drawn from ``SUPPORTED_INDICATORS``. ``None`` → compute all.
    ma_windows / rsi_windows:
        Customizable lookback windows.

    Returns
    -------
    dict with:
        - ``length``: number of bars consumed
        - ``indicators``: mapping of indicator name → payload
        - ``latest``: last-bar snapshot of scalar values for quick display
    """

    frame = bars_to_frame(bars)
    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]

    requested = (
        sorted(SUPPORTED_INDICATORS)
        if indicators is None
        else sorted({str(name).lower() for name in indicators} & SUPPORTED_INDICATORS)
    )
    if not requested:
        raise ValueError("no supported indicator requested")

    out: dict[str, Any] = {"length": len(frame), "indicators": {}, "latest": {}}
    latest: dict[str, Any] = out["latest"]
    bundle: dict[str, Any] = out["indicators"]

    if "ma" in requested:
        ma_payload = {f"ma{w}": _series_to_list(sma(close, w)) for w in ma_windows}
        bundle["ma"] = ma_payload
        for name, series in ma_payload.items():
            latest[name] = series[-1]

    if "ema" in requested:
        ema_payload = {f"ema{w}": _series_to_list(ema(close, w)) for w in (12, 26)}
        bundle["ema"] = ema_payload
        for name, series in ema_payload.items():
            latest[name] = series[-1]

    if "macd" in requested:
        macd_parts = macd(close)
        bundle["macd"] = {k: _series_to_list(v) for k, v in macd_parts.items()}
        for name, series in bundle["macd"].items():
            latest[f"macd_{name}"] = series[-1]

    if "rsi" in requested:
        rsi_payload = {f"rsi{w}": _series_to_list(rsi(close, w), digits=2) for w in rsi_windows}
        bundle["rsi"] = rsi_payload
        for name, series in rsi_payload.items():
            latest[name] = series[-1]

    if "kdj" in requested:
        kdj_parts = kdj(high, low, close)
        bundle["kdj"] = {k: _series_to_list(v, digits=2) for k, v in kdj_parts.items()}
        for name, series in bundle["kdj"].items():
            latest[f"kdj_{name}"] = series[-1]

    if "boll" in requested:
        boll_parts = bollinger(close)
        bundle["boll"] = {k: _series_to_list(v) for k, v in boll_parts.items()}
        for name, series in bundle["boll"].items():
            latest[f"boll_{name}"] = series[-1]

    if "atr" in requested:
        atr_series = atr(high, low, close)
        bundle["atr"] = {"atr14": _series_to_list(atr_series)}
        latest["atr14"] = bundle["atr"]["atr14"][-1]

    if "obv" in requested:
        obv_series = obv(close, volume)
        bundle["obv"] = {"obv": _series_to_list(obv_series, digits=0)}
        latest["obv"] = bundle["obv"]["obv"][-1]

    if "vol_ma" in requested:
        vol_payload = {f"vol_ma{w}": _series_to_list(sma(volume, w), digits=0) for w in (5, 10)}
        bundle["vol_ma"] = vol_payload
        for name, series in vol_payload.items():
            latest[name] = series[-1]

    return out


__all__ = [
    "SUPPORTED_INDICATORS",
    "DEFAULT_MA_WINDOWS",
    "DEFAULT_RSI_WINDOWS",
    "bars_to_frame",
    "compute_indicators",
    "sma",
    "ema",
    "macd",
    "rsi",
    "kdj",
    "bollinger",
    "atr",
    "obv",
]
