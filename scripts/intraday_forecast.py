"""次日预测分时 + 最高胜率做T幅度寻优（供 nextday 做T计划增强）。

口径与因果性
------------
* 全部统计只依赖 bars[0..index]（与 nextday_model 同一条严格因果约束），
  tests/test_intraday_forecast.py 中的因果性测试验证这一点。
* 预测分时：没有分钟级历史库，走统计模拟——以近 60 日跳空分布的中位数
  估计次日开盘相对前收的幅度，再按 A 股日内 U 型波动模板（开盘半小时
  波动最强、午间收敛、尾盘回升）把 ATR 年化到分钟级，合成一条带上下带
  的 5 分钟走廊，并按次日倾向（正T先探底/反T先冲高）定向演化，标注
  冲高与回撤的期望出现时段。输出明确标注"统计模拟，非真实分时"。
* 最高胜率做T幅度：对 (卖出幅度 sell%, 回落幅度 buy%) 网格做双触达
  统计——某日"双触达"= 日内最高 ≥ 前收×(1+sell%) 且 日内最低 ≤
  前收×(1-buy%)。OHLC 无法还原分钟级先后顺序，按保守代理过滤：
  反T（先卖后买）要求收盘位于当日振幅下半区（高点在前、低点在后），
  正T（先买后卖）要求收盘位于上半区（低点在前、高点在后）。
* 输出仅为统计参考，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

FORECAST_SCHEMA_VERSION = "intraday-forecast.v1"
FORECAST_NOTE = "统计模拟路径（历史跳空分布×日内U型波动模板），非真实分时"

# A股日内 U 型波动模板：分钟相对波动权重（开盘强、午间弱、尾盘回升）。
# _session_minutes 生成 09:30-11:30 + 13:00-15:00 共 240 根 5 分钟 bar。
_MORNING_BARS = 24
_AFTERNOON_BARS = 24
_TOTAL_BARS = _MORNING_BARS + _AFTERNOON_BARS


def _session_labels() -> list[str]:
    labels: list[str] = []
    hour, minute = 9, 35
    for _ in range(_MORNING_BARS):
        labels.append(f"{hour:02d}:{minute:02d}")
        minute += 5
        if minute == 60:
            hour, minute = hour + 1, 0
    hour, minute = 13, 5
    for _ in range(_AFTERNOON_BARS):
        labels.append(f"{hour:02d}:{minute:02d}")
        minute += 5
        if minute == 60:
            hour, minute = hour + 1, 0
    return labels


_SESSION_LABELS = _session_labels()


def _bar_weight(bar_index: int) -> float:
    """第 bar_index 根 5 分钟 bar 的相对波动权重（U 型）。"""

    if bar_index < _MORNING_BARS:
        t = bar_index
    else:
        t = bar_index - _MORNING_BARS
    morning = 1.0 + 1.7 * pow(2.718281828, -t / 4.0)
    tail = 1.0 + 0.8 * pow(2.718281828, -(t - (_TOTAL_BARS / 2 - 1)) / 3.0) if bar_index >= _MORNING_BARS else 0.0
    return morning + tail


_BAR_WEIGHTS = [_bar_weight(i) for i in range(_TOTAL_BARS)]
_WEIGHT_SUM = sum(_BAR_WEIGHTS)


def _percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct / 100.0)))
    return float(ordered[pos])


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    return float(ordered[n // 2]) if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _bar_float(bar: Mapping[str, Any], key: str, default: float | None = None) -> float | None:
    value = bar.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _day_records(bars: Sequence[Mapping[str, Any]], index: int, days: int) -> list[dict[str, float]]:
    """近 days 个交易日的 (前收, 高, 低, 开, 收) 序列，只含 ≤index。"""

    records: list[dict[str, float]] = []
    start = max(1, index - days + 1)
    for i in range(start, index + 1):
        close = _bar_float(bars[i], "close")
        prev = _bar_float(bars[i - 1], "close")
        high = _bar_float(bars[i], "high", close)
        low = _bar_float(bars[i], "low", close)
        open_ = _bar_float(bars[i], "open", close)
        if not close or not prev or not high or not low or not open_:
            continue
        span = high - low
        records.append(
            {
                "prev_close": prev,
                "high": high,
                "low": low,
                "open": open_,
                "close": close,
                "close_pos": (close - low) / span if span > 0 else 0.5,
                "range_pct": span / prev * 100.0 if prev else 0.0,
                "gap_pct": (open_ / prev - 1.0) * 100.0 if prev else 0.0,
            }
        )
    return records


def best_t_pair(
    bars: Sequence[Mapping[str, Any]],
    index: int,
    *,
    direction: str,
    days: int = 120,
    min_hits: int = 12,
) -> dict[str, Any] | None:
    """双触达网格寻优：历史胜率最高的 (sell%, buy%) 做T幅度组合。

    direction: "down"=反T（先卖后买），"up"=正T（先买后卖）。
    评分在 hit_count ≥ min_hits 的组合里取胜率最高者；胜率并列取
    幅度和更大者（单次完成的利润空间更大）。
    """

    records = _day_records(bars, index, days)
    if len(records) < max(40, min_hits):
        return None

    sell_grid = [round(1.0 + 0.25 * k, 2) for k in range(0, 25)]      # 1.00 ~ 7.00
    buy_grid = [round(0.5 + 0.25 * k, 2) for k in range(0, 23)]       # 0.50 ~ 6.00
    combos: list[dict[str, Any]] = []
    for sell in sell_grid:
        for buy in buy_grid:
            if buy >= sell:
                continue
            hits = 0
            for row in records:
                high_pct = (row["high"] / row["prev_close"] - 1.0) * 100.0
                low_pct = (row["low"] / row["prev_close"] - 1.0) * 100.0
                if high_pct < sell or low_pct > -buy:
                    continue
                # 序列代理：反T 要求收盘在振幅下半区（高点在前），正T 要求上半区。
                if direction == "down" and row["close_pos"] > 0.55:
                    continue
                if direction == "up" and row["close_pos"] < 0.45:
                    continue
                hits += 1
            if hits >= min_hits:
                combos.append(
                    {
                        "sell_pct": sell,
                        "buy_pct": buy,
                        "hits": hits,
                        "days": len(records),
                        "hit_rate": round(hits / len(records), 4),
                        "sum_pct": sell + buy,
                    }
                )
    if not combos:
        return None
    # 纯胜率最大化会收敛到最小幅度组合（双触达最容易），扣除手续费滑点后
    # 无利润空间：优先在幅度和 ≥ 2%（约覆盖双边成本）的组合里选最高胜率，
    # 低波动品种没有达标组合时再整体放宽。
    for floor in (2.0, 0.0):
        eligible = [c for c in combos if c["sum_pct"] >= floor]
        if eligible:
            eligible.sort(
                key=lambda c: (c["hit_rate"], c["sum_pct"], c["sell_pct"]), reverse=True
            )
            return eligible[0]
    return None


def _path_point(bar_index: int, *, open_price: float, high_zone: float, low_zone: float,
                close_price: float, direction: str, band: float) -> dict[str, Any]:
    """把第 bar_index 根 5 分钟 bar 的预测中枢映射到 [低区, 高区] 走廊。"""

    progress = (bar_index + 1) / _TOTAL_BARS
    if direction == "down":
        # 反T模板：开盘附近先冲向高区（前 1/3），午后回落至低区，尾盘小幅收敛。
        if bar_index < _MORNING_BARS:
            phase = progress / 0.5
            level = open_price + (high_zone - open_price) * min(1.0, phase * 1.9)
            if phase > 0.75:
                level -= (level - open_price) * (phase - 0.75) * 1.6
        else:
            phase = (progress - 0.5) / 0.5
            pull = (high_zone - low_zone) * min(1.0, phase * 1.25)
            level = high_zone - pull
            if phase > 0.85:
                level += (close_price - level) * (phase - 0.85) * 4.0
    else:
        # 正T模板：早盘探至低区（前 1/3），随后回升至高区，尾盘向预测收盘收敛。
        if bar_index < _MORNING_BARS:
            phase = progress / 0.5
            level = open_price - (open_price - low_zone) * min(1.0, phase * 1.9)
            if phase > 0.75:
                level += (open_price - level) * (phase - 0.75) * 1.6
        else:
            phase = (progress - 0.5) / 0.5
            rise = (high_zone - low_zone) * min(1.0, phase * 1.25)
            level = low_zone + rise
            if phase > 0.85:
                level -= (level - close_price) * (phase - 0.85) * 4.0
    span = abs(high_zone - low_zone)
    band_px = max(0.01, band * span)
    return {
        "t": _SESSION_LABELS[min(bar_index, _TOTAL_BARS - 1)],
        "price": round(level, 3),
        "hi": round(level + band_px, 3),
        "lo": round(level - band_px, 3),
    }


def build_intraday_forecast(
    bars: Sequence[Mapping[str, Any]],
    *,
    bias: str,
    score: float,
    atr14: float | None = None,
    config: Mapping[str, Any] | None = None,
    days: int = 120,
) -> dict[str, Any]:
    """为 bars 最后一日的次日合成预测分时 + 最高胜率做T建议。"""

    cfg = dict(config or {})
    index = len(bars) - 1
    last_close = _bar_float(bars[index], "close") or 0.0
    if last_close <= 0 or len(bars) < 40:
        return {"schema_version": FORECAST_SCHEMA_VERSION, "ok": False,
                "reason": f"insufficient bars: {len(bars)}"}

    records = _day_records(bars, index, 60)
    gap_median = _median([row["gap_pct"] for row in records]) if records else 0.0
    range_p85 = _percentile([row["range_pct"] for row in records], 85) if records else 2.0
    atr_pct = (atr14 / last_close * 100.0) if atr14 and last_close else max(1.0, range_p85 / 2.0)

    direction = "down" if bias == "down" else ("up" if bias == "up" else ("up" if score >= 0 else "down"))
    pair = best_t_pair(bars, index, direction=direction, days=days)
    if pair is None:
        pair = best_t_pair(bars, index, direction=direction, days=days, min_hits=6)
    if pair is None:
        # 极端窄幅品种：退化为振幅分位的对称区间
        pair = {
            "sell_pct": round(max(1.0, range_p85 * 0.5), 2),
            "buy_pct": round(max(0.5, range_p85 * 0.3), 2),
            "hits": 0, "days": len(records), "hit_rate": 0.0,
        }
        method = "振幅分位退化（双触达样本不足）"
    else:
        method = "双触达网格寻优（收盘位置序列过滤）"

    # 预测走廊：中枢按倾向定向演化，宽度按 U 型波动包络。
    sell_move = last_close * pair["sell_pct"] / 100.0
    buy_move = last_close * pair["buy_pct"] / 100.0
    high_zone = last_close + sell_move
    low_zone = last_close - buy_move
    open_pct = max(-3.0, min(3.0, gap_median + (0.10 if direction == "up" else -0.10) * max(0.0, min(1.0, abs(score)))))
    open_price = round(last_close * (1.0 + open_pct / 100.0), 3)
    close_pred_pct = max(-4.0, min(4.0, open_pct * 0.5 + (0.25 if direction == "up" else -0.25)))
    close_price = round(last_close * (1.0 + close_pred_pct / 100.0), 3)
    band = 0.30 * (atr_pct / max(0.5, pair["sell_pct"] + pair["buy_pct"]))

    path = [_path_point(i, open_price=open_price, high_zone=high_zone, low_zone=low_zone,
                        close_price=close_price, direction=direction, band=band)
            for i in range(_TOTAL_BARS)]
    sell_time = next(
        (p["t"] for p in path if p["price"] >= round(high_zone, 3) - 1e-6), None
    )
    buy_time = next(
        (p["t"] for p in path if p["price"] <= round(low_zone, 3) + 1e-6), None
    )

    advice_text = f"上涨{pair['sell_pct']:.2f}%卖出，回落{pair['buy_pct']:.2f}%买回"
    hit_rate_pct = round(pair["hit_rate"] * 100.0, 1)
    return {
        "schema_version": FORECAST_SCHEMA_VERSION,
        "ok": True,
        "note": FORECAST_NOTE,
        "days": pair["days"],
        "atr_pct": round(atr_pct, 3),
        "open_pred_pct": round(open_pct, 2),
        "close_pred_pct": round(close_pred_pct, 2),
        "high_zone": round(high_zone, 3),
        "low_zone": round(low_zone, 3),
        "path": path,
        "sell_time_hint": sell_time,
        "buy_time_hint": buy_time,
        "advice": {
            "text": advice_text,
            "direction": "反T" if direction == "down" else "正T",
            "sell_pct": pair["sell_pct"],
            "buy_pct": pair["buy_pct"],
            "sell_price": round(high_zone, 2),
            "buy_price": round(low_zone, 2),
            "hit_rate": hit_rate_pct,
            "hits": pair["hits"],
            "days": pair["days"],
            "method": method,
            "disclaimer": "历史双触达统计，未计手续费滑点，不构成投资建议",
        },
    }
