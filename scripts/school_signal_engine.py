# -*- coding: utf-8 -*-
"""三大技术分析流派信号引擎（趋势跟踪 / 道氏理论 / 量价分析Wyckoff）。

输入 OHLCV 日线 bars（dict 列表，含 time/open/close/high/low/volume），
输出各流派当前状态、具体触发价位与操作提示；纯函数，无 I/O。

与回测引擎 backtest.py 的规则保持一致：
  * trend   双均线 MA20/MA60 时序动量
  * dow     8% 波段高低点确认（更高低点+突破前高做多，跌破前低离场）
  * wyckoff 25 日窄幅整理 + 放量 1.8 倍突破上沿做多，跌破下沿/回撤 12% 离场
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

ENGINE_VERSION = "school-signal.2026-09-28.1"

SCHOOLS = (
    {"key": "trend", "name": "趋势跟踪", "rank": "全球实证 #1"},
    {"key": "dow", "name": "道氏理论", "rank": "全球实证 #2"},
    {"key": "wyckoff", "name": "量价分析(Wyckoff)", "rank": "全球实证 #3"},
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _ma(values: Sequence[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= window:
            acc -= values[i - window]
        if i >= window - 1:
            out[i] = acc / window
    return out


def _zigzag(closes: Sequence[float], threshold: float = 0.08) -> List[List[Any]]:
    """收盘价 zigzag，返回确认的拐点 [[idx, price, 'H'/'L'], ...]。"""
    pivots: List[List[Any]] = []
    n = len(closes)
    if n < 3:
        return pivots
    direction = 0
    extreme_idx, extreme = 0, closes[0]
    for i in range(1, n):
        c = closes[i]
        if direction == 0:
            if c >= extreme * (1 + threshold):
                direction = 1
                pivots.append([extreme_idx, extreme, "L"])
                extreme_idx, extreme = i, c
            elif c <= extreme * (1 - threshold):
                direction = -1
                pivots.append([extreme_idx, extreme, "H"])
                extreme_idx, extreme = i, c
            else:
                if c < extreme:
                    extreme_idx, extreme = i, c
                elif c > extreme:
                    extreme_idx, extreme = i, c
        elif direction == 1:
            if c > extreme:
                extreme_idx, extreme = i, c
            elif c <= extreme * (1 - threshold):
                pivots.append([extreme_idx, extreme, "H"])
                direction = -1
                extreme_idx, extreme = i, c
        else:
            if c < extreme:
                extreme_idx, extreme = i, c
            elif c >= extreme * (1 + threshold):
                pivots.append([extreme_idx, extreme, "L"])
                direction = 1
                extreme_idx, extreme = i, c
    return pivots


# ---------------------------------------------------------------------------
# 各流派：完整信号序列（用于检测"今日是否发生状态切换"）
# ---------------------------------------------------------------------------

def trend_signals(closes: Sequence[float], fast: int = 20, slow: int = 60) -> List[int]:
    ma_f = _ma(closes, fast)
    ma_s = _ma(closes, slow)
    out: List[int] = []
    for f, s in zip(ma_f, ma_s):
        out.append(1 if (f is not None and s is not None and f > s) else 0)
    return out


def dow_signals(closes: Sequence[float], threshold: float = 0.08) -> List[int]:
    n = len(closes)
    pivots = _zigzag(closes, threshold)
    sig = [0] * n
    highs = [p for p in pivots if p[2] == "H"]
    lows = [p for p in pivots if p[2] == "L"]
    if not highs or len(lows) < 2:
        return sig
    pos = 0
    li = 1
    hi = 0
    for t in range(n):
        while hi < len(highs) - 1 and highs[hi + 1][0] <= t:
            hi += 1
        while li < len(lows) - 1 and lows[li + 1][0] <= t:
            li += 1
        last_high = highs[hi]
        cur_low = lows[li]
        prev_low = lows[li - 1]
        higher_low = cur_low[1] > prev_low[1]
        if pos == 0:
            if higher_low and cur_low[0] > last_high[0] and closes[t] > last_high[1]:
                pos = 1
        else:
            if closes[t] < cur_low[1]:
                pos = 0
        sig[t] = pos
    return sig


def wyckoff_signals(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    vols: Sequence[Optional[float]],
    range_n: int = 25,
    range_max: float = 0.25,
    vol_mult: float = 1.8,
    trail: float = 0.12,
) -> List[int]:
    n = len(closes)
    sig = [0] * n
    pos = 0
    stop = math.nan
    peak = math.nan
    for t in range(n):
        if t >= 20:
            window = [v for v in vols[t - 20:t] if v]
            vma = sum(window) / len(window) if window else None
        else:
            vma = None
        if pos == 0 and t >= range_n:
            rh = max(highs[t - range_n:t])
            rl = min(lows[t - range_n:t])
            v_ok = vma is not None and vols[t] is not None and vols[t] >= vol_mult * vma  # type: ignore[operator]
            if rl > 0 and (rh / rl - 1) <= range_max and v_ok and closes[t] > rh:
                pos = 1
                stop = rl
                peak = closes[t]
        elif pos == 1:
            peak = max(peak, closes[t])
            if closes[t] < max(stop, peak * (1 - trail)):
                pos = 0
                stop = math.nan
                peak = math.nan
        sig[t] = pos
    return sig


# ---------------------------------------------------------------------------
# 各流派：当前状态 + 触发价
# ---------------------------------------------------------------------------

def _round(v: Optional[float], digits: int = 2) -> Optional[float]:
    return None if v is None else round(float(v), digits)


def trend_state(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [b["close"] for b in bars]
    sig = trend_signals(closes)
    holding = sig[-1] == 1
    ma20 = _ma(closes, 20)[-1]
    ma60 = _ma(closes, 60)[-1]
    spread = (ma20 / ma60 - 1) if (ma20 and ma60) else None
    # 明日均线交叉的临界收盘价：3*(S19+C)=S59+C → C=(S59-3*S19)/2
    cross_price = None
    if len(closes) >= 60:
        s19 = sum(closes[-20:-1])
        s59 = sum(closes[-60:-1])
        cross_price = (s59 - 3 * s19) / 2
    switched = len(sig) >= 2 and sig[-1] != sig[-2]
    if holding:
        hint = f"持仓中；收盘跌破约 {_round(cross_price)} 将死叉离场"
        if spread is not None and spread < 0.02:
            level = "near"
        else:
            level = "hold"
    else:
        if spread is not None and spread > -0.03:
            hint = f"空仓；接近金叉，收盘站上约 {_round(cross_price)} 触发买入"
            level = "near"
        else:
            hint = f"空仓；均线空头排列（乖离 {round((spread or 0) * 100, 1)}%），先观察收复 MA20 ≈ {_round(ma20)}"
            level = "avoid"
    if switched:
        level = "trigger"
        hint = ("今日金叉触发买入" if holding else "今日死叉触发卖出") + f"（收盘 {closes[-1]:.2f}）"
    return {
        "holding": holding, "ma20": _round(ma20), "ma60": _round(ma60),
        "spread_pct": _round(spread * 100, 2) if spread is not None else None,
        "cross_price": _round(cross_price), "switched_today": switched,
        "level": level, "hint": hint,
    }


def dow_state(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [b["close"] for b in bars]
    dates = [b["time"] for b in bars]
    sig = dow_signals(closes)
    holding = sig[-1] == 1
    pivots = _zigzag(closes)
    highs = [p for p in pivots if p[2] == "H"]
    lows = [p for p in pivots if p[2] == "L"]
    last_high = highs[-1] if highs else None
    last_low = lows[-1] if lows else None
    prev_low = lows[-2] if len(lows) > 1 else None
    switched = len(sig) >= 2 and sig[-1] != sig[-2]
    higher_low = bool(prev_low and last_low and last_low[1] > prev_low[1])
    breakout = last_high[1] if last_high else None
    breakdown = last_low[1] if last_low else None
    dist = (breakout / closes[-1] - 1) if breakout else None

    if holding:
        hint = f"持仓中；收盘跌破最近波段低点 {_round(breakdown)}（{dates[last_low[0]] if last_low else '--'}）离场"
        level = "hold"
    else:
        prefix = "更高低点已现" if higher_low else "尚无更高低点"
        hint = f"空仓；{prefix}，收盘突破前高 {_round(breakout)}（{dates[last_high[0]] if last_high else '--'}）买入"
        level = "near" if (dist is not None and dist <= 0.05 and higher_low) else "avoid"
    if switched:
        level = "trigger"
        hint = ("今日突破前高触发买入" if holding else "今日跌破波段低点触发卖出") + f"（收盘 {closes[-1]:.2f}）"
    return {
        "holding": holding, "switched_today": switched,
        "last_high": _round(breakout), "last_high_date": dates[last_high[0]] if last_high else None,
        "last_low": _round(breakdown), "last_low_date": dates[last_low[0]] if last_low else None,
        "higher_low": higher_low,
        "dist_to_breakout_pct": _round(dist * 100, 1) if dist is not None else None,
        "level": level, "hint": hint,
    }


def wyckoff_state(bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    vols = [b.get("volume") for b in bars]
    sig = wyckoff_signals(highs, lows, closes, vols)
    holding = sig[-1] == 1
    switched = len(sig) >= 2 and sig[-1] != sig[-2]

    n = len(bars)
    rh = max(highs[max(0, n - 25):n]) if n else None
    rl = min(lows[max(0, n - 25):n]) if n else None
    width = (rh / rl - 1) if (rh and rl) else None
    window = [v for v in vols[-21:-1] if v]
    vma20 = sum(window) / len(window) if window else None
    vol_ratio = (vols[-1] / vma20) if (vma20 and vols[-1] is not None) else None

    stop = None
    if holding:
        idx = None
        for t in range(n - 1, 0, -1):
            if sig[t] == 1 and sig[t - 1] == 0:
                idx = t
                break
        if idx is not None:
            rl_entry = min(lows[max(0, idx - 25):idx])
            peak = max(closes[idx:])
            stop = max(rl_entry, peak * 0.88)

    if holding:
        hint = f"持仓中；收盘跌破止损 {_round(stop)} 离场"
        level = "hold"
    else:
        vol_txt = f"今日量比 {_round(vol_ratio, 2)}"
        hint = (f"空仓；近25日区间 {_round(rl)}~{_round(rh)}（振幅 {round((width or 0) * 100, 1)}%），"
                f"放量≥1.8倍收盘突破 {_round(rh)} 买入；{vol_txt}")
        dist = (rh / closes[-1] - 1) if rh else None
        level = "near" if (dist is not None and dist <= 0.05 and (width or 9) <= 0.25) else "avoid"
    if switched:
        level = "trigger"
        hint = ("今日放量突破触发买入" if holding else "今日跌破止损触发卖出") + f"（收盘 {closes[-1]:.2f}）"
    return {
        "holding": holding, "switched_today": switched, "stop": _round(stop),
        "range_high": _round(rh), "range_low": _round(rl),
        "range_width_pct": _round(width * 100, 1) if width is not None else None,
        "vol_ratio": _round(vol_ratio, 2),
        "level": level, "hint": hint,
    }


SCHOOL_EVALUATORS = {
    "trend": trend_state,
    "dow": dow_state,
    "wyckoff": wyckoff_state,
}


def evaluate_stock(symbol: str, name: str, bars: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """对一只股票输出三流派状态与综合提示。"""
    schools = {key: fn(bars) for key, fn in SCHOOL_EVALUATORS.items()}
    close = bars[-1]["close"]
    levels = [s["level"] for s in schools.values()]
    if "trigger" in levels:
        overall = "trigger"
    elif "hold" in levels:
        overall = "hold"
    elif "near" in levels:
        overall = "near"
    else:
        overall = "avoid"
    holding_votes = sum(1 for s in schools.values() if s["holding"])
    return {
        "symbol": symbol,
        "name": name,
        "date": bars[-1]["time"],
        "close": _round(close),
        "schools": schools,
        "overall": overall,
        "holding_votes": holding_votes,
        "bar_count": len(bars),
    }


# ---------------------------------------------------------------------------
# 提示事件：从两次状态对比中提炼（用于写日志）
# ---------------------------------------------------------------------------

def extract_events(stock_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从单股评估结果提炼可记录的提示事件。"""
    events: List[Dict[str, Any]] = []
    date = stock_result["date"]
    close = stock_result["close"]
    for key, s in stock_result["schools"].items():
        if s["switched_today"]:
            events.append({
                "date": date, "symbol": stock_result["symbol"], "name": stock_result["name"],
                "school": key, "event": "buy_trigger" if s["holding"] else "sell_trigger",
                "price": close, "detail": s["hint"],
            })
        elif s["level"] == "near":
            events.append({
                "date": date, "symbol": stock_result["symbol"], "name": stock_result["name"],
                "school": key, "event": "near_trigger",
                "price": close, "detail": s["hint"],
            })
    return events


# ---------------------------------------------------------------------------
# 正确率回溯：对历史事件计算前瞻收益
# ---------------------------------------------------------------------------

def score_events(
    events: Sequence[Dict[str, Any]],
    bars_by_symbol: Dict[str, Sequence[Dict[str, Any]]],
    horizons: Sequence[int] = (5, 20),
) -> Dict[str, Any]:
    """对每个事件，用该股票事件日之后的收盘价计算前瞻收益并判定命中。

    buy_trigger / near_trigger：前瞻收益 > 0 记命中（近似提示按买入对待）；
    sell_trigger：前瞻收益 < 0 记命中（避开下跌）。
    不足 horizon 天数的事件记为 pending。
    """
    scored: List[Dict[str, Any]] = []
    stats: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        bars = bars_by_symbol.get(ev["symbol"]) or []
        dates = [b["time"] for b in bars]
        closes = [b["close"] for b in bars]
        row = dict(ev)
        row["outcomes"] = {}
        if ev["date"] in dates:
            i = dates.index(ev["date"])
            for h in horizons:
                j = i + h
                key = f"ret_{h}d"
                if j < len(closes) and closes[i] > 0:
                    ret = closes[j] / closes[i] - 1
                    hit = ret > 0 if ev["event"] != "sell_trigger" else ret < 0
                    row["outcomes"][key] = {"ret": round(ret, 4), "hit": hit}
                else:
                    row["outcomes"][key] = {"ret": None, "hit": None}  # pending
        scored.append(row)

        school = ev["school"]
        st = stats.setdefault(school, {"total": 0, "hit": 0, "pending": 0})
        out5 = row["outcomes"].get("ret_5d", {})
        if out5.get("hit") is None:
            st["pending"] += 1
        else:
            st["total"] += 1
            st["hit"] += 1 if out5["hit"] else 0
    for st in stats.values():
        st["hit_rate"] = round(st["hit"] / st["total"], 4) if st["total"] else None
    return {"events": scored, "stats": stats}
