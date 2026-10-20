"""次日做T计划模型：由技术指标推导次日倾向与日内做T价位计划。

设计原则
--------
* 严格因果：t 日计划只读取 bars[0..t] 与指标序列在 t 处的值
  （compute_indicators 的全部序列均为因果构造：MA/EMA/MACD/RSI/KDJ/BOLL/
  ATR/VOL_MA 在 i 处的值只依赖 ≤i 的数据），杜绝未来函数。
  tests/test_nextday_model.py 中的因果性测试验证这一点。
* 可回测：计划是纯函数，倾向阈值与做T区间倍数显式传入 config，
  由 scripts/nextday_backtest.py 做 walk-forward 网格寻优。
* 合规输出：所有计划仅为参考信号，不构成投资建议。

做T口径（基于底仓，非开仓）：
* 正T（先买后卖）：预期次日回落，于 前收 - buy_atr×ATR14 附近买入，
  冲高至 前收 + sell_atr×ATR14 附近卖出等量底仓。
* 反T（先卖后买）：预期次日冲高，先在高位卖出部分底仓，回落至低位买回。
* 观望：模型倾向不明确，不建议做T。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

MODEL_VERSION = "nextday-plan.2026-10-04.7"
PLAN_SCHEMA_VERSION = "nextday-plan.v1"

# 参数经 2026-09-29 网格寻优（15只持仓×~450交易日 walk-forward，18组合）：
# 最优 buy=0.40 sell=0.50 theta=0.25，合计 67,103bp（基线 40,283bp）。
DEFAULT_CONFIG: dict[str, float] = {
    "theta": 0.25,     # 倾向阈值：score ≥ theta → up，≤ -theta → down，否则观望
    "buy_atr": 0.40,   # 买入参考 = 前收 - buy_atr × ATR14
    "sell_atr": 0.50,  # 卖出参考 = 前收 + sell_atr × ATR14
    "stop_atr": 1.20,  # 放弃阈值：开盘越过 前收 ± stop_atr × ATR14 时放弃当日做T
    "news_weight": 0.15,  # 新闻情绪权重（新闻无历史归档不可回测，权重保守）
    "fund_weight": 1.0,   # 主力资金流权重（有日K历史，可回测寻优）
    "zone_coupling": 0.0, # 置信度-区间耦合强度（0=关；回测寻优后校准）
    "zone_basis": "atr",  # 区间基准："atr"=ATR倍数（回测寻优）|"percentile"=历史振幅分位（科学幅度）
}

# 特征权重（v6 反转口径，2026-10-04 方向诊断后采纳）：A 股次日短线以
# 反转效应为主导——趋势/动量/量能/摆动为正时次日更偏回落，故权重为负；
# 尾盘弱收（close_pos<0）次日偏强，权重为正。诊断：取反后方向命中率
# trend 51.0% / momentum 50.8% / volume 50.4% / stretch 51.0% / close_pos 52.5%。
_WEIGHTS = {
    "trend": -0.30,    # 均线排列 + 收盘相对 MA5 的 ATR 距离（反转）
    "momentum": -0.22, # MACD 柱 / ATR 归一 + 柱方向（反转）
    "volume": -0.10,   # 量比偏离 × 当日涨跌方向（反转）
    "stretch": -0.28,  # 布林带位置（超买超卖回拉）
    "close_pos": 0.30, # 尾盘弱收→次日偏强（反转）
}


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _series_value(bundle: Mapping[str, Any], path: Sequence[str], index: int) -> float | None:
    node: Any = bundle.get("indicators") or {}
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    if isinstance(node, (list, tuple)) and 0 <= index < len(node):
        value = node[index]
        return float(value) if isinstance(value, (int, float)) else None
    return None


def _first(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _amplitude_stats(bars: Sequence[Mapping[str, Any]], index: int, *, days: int = 120) -> dict[str, Any]:
    """近 N 日的日内波动结构统计（相对当日收盘，因果：只用 ≤index 的数据）。

    dip = (close - low) / close × 100 —— 收盘前回撤深度
    rip = (high - close) / close × 100 —— 收盘上方冲高强度
    返回中位数与 35/65 分位（做T区间的"科学幅度"依据）。
    """
    start = max(1, index - days + 1)
    dips: list[float] = []
    rips: list[float] = []
    ranges: list[float] = []
    for i in range(start, index + 1):
        close = float(bars[i]["close"])
        high = float(bars[i].get("high") or close)
        low = float(bars[i].get("low") or close)
        if close <= 0:
            continue
        dips.append(max(0.0, (close - low) / close * 100.0))
        rips.append(max(0.0, (high - close) / close * 100.0))
        ranges.append((high - low) / close * 100.0)
    if not dips:
        return {"days": 0}

    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        n = len(ordered)
        mid = n // 2
        return round((ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0), 3)

    def _percentile(values: list[float], pct: float) -> float:
        ordered = sorted(values)
        pos = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct / 100.0)))
        return round(ordered[pos], 3)

    return {
        "days": len(dips),
        "median_dip": _median(dips),
        "median_rip": _median(rips),
        "p35_dip": _percentile(dips, 35),
        "p65_dip": _percentile(dips, 65),
        "p35_rip": _percentile(rips, 35),
        "p65_rip": _percentile(rips, 65),
        "median_range": _median(ranges),
    }


def compute_score(
    bars: Sequence[Mapping[str, Any]],
    bundle: Mapping[str, Any],
    index: int,
) -> tuple[float, dict[str, float]]:
    """计算 t=index 的次日倾向得分（[-1,1]）与各特征值。

    只读取 index 及之前的数据；bundle 序列在 index 处的值即为因果值。
    """
    bar = bars[index]
    close = float(bar["close"])
    prev_close = float(bars[index - 1]["close"]) if index >= 1 else close
    high = float(bar.get("high") or close)
    low = float(bar.get("low") or close)
    atr = _series_value(bundle, ("atr", "atr14"), index)

    features: dict[str, float] = {
        "trend": 0.0, "momentum": 0.0, "volume": 0.0, "stretch": 0.0, "close_pos": 0.0,
    }
    if not atr or atr <= 0:
        return 0.0, features

    # --- 趋势：均线排列 + 收盘相对 MA5 的 ATR 距离 ---
    ma5 = _series_value(bundle, ("ma", "ma5"), index)
    ma10 = _series_value(bundle, ("ma", "ma10"), index)
    ma20 = _series_value(bundle, ("ma", "ma20"), index)
    if ma5 is not None and ma10 is not None and ma20 is not None:
        if ma5 > ma10 > ma20:
            align = 1.0
        elif ma5 < ma10 < ma20:
            align = -1.0
        else:
            align = 0.0
        features["trend"] = _clip(align * 0.7 + (close - ma5) / atr * 0.3)

    # --- 动能：MACD 柱 / ATR 归一 + 柱增减方向 ---
    hist = _series_value(bundle, ("macd", "hist"), index)
    hist_prev = _series_value(bundle, ("macd", "hist"), index - 1) if index >= 1 else None
    if hist is not None:
        momentum = hist / atr * 0.8
        if hist_prev is not None:
            momentum += 0.2 if hist > hist_prev else -0.2 if hist < hist_prev else 0.0
        features["momentum"] = _clip(momentum)

    # --- 量能：量比偏离 × 当日涨跌方向 ---
    vol = bar.get("volume")
    vol_ma5 = _series_value(bundle, ("vol_ma", "vol_ma5"), index)
    if isinstance(vol, (int, float)) and vol and vol_ma5 and vol_ma5 > 0:
        day_dir = 0.0
        if close > prev_close:
            day_dir = 1.0
        elif close < prev_close:
            day_dir = -1.0
        features["volume"] = _clip((vol / vol_ma5 - 1.0)) * day_dir

    # --- 摆动：布林带位置（相对中轨，宽度归一）---
    upper = _series_value(bundle, ("boll", "upper"), index)
    lower = _series_value(bundle, ("boll", "lower"), index)
    mid = _series_value(bundle, ("boll", "mid"), index)
    if upper is not None and lower is not None and mid is not None and upper > lower:
        features["stretch"] = _clip((close - mid) / (upper - lower) * 1.2)

    # --- 尾盘强弱：收盘在当日振幅中的位置 ---
    if high > low:
        features["close_pos"] = _clip(((close - low) / (high - low) - 0.5) * 2.0)

    score = sum(_WEIGHTS[name] * features[name] for name in features)
    return _clip(score), features


def _rationale(features: Mapping[str, float], bias: str, score: float) -> list[str]:
    parts: list[str] = []
    if abs(features["trend"]) >= 0.3:
        parts.append("均线多头排列" if features["trend"] > 0 else "均线空头排列")
    if abs(features["momentum"]) >= 0.3:
        parts.append("MACD动能偏多" if features["momentum"] > 0 else "MACD动能偏空")
    if abs(features["volume"]) >= 0.3:
        parts.append("放量" if features["volume"] > 0 else "缩量")
    if features["stretch"] >= 0.7:
        parts.append("触及布林上轨附近")
    elif features["stretch"] <= -0.7:
        parts.append("触及布林下轨附近")
    if not parts:
        parts.append("技术特征中性")
    return parts


def compute_plan(
    bars: Sequence[Mapping[str, Any]],
    config: Mapping[str, float] | None = None,
    *,
    bundle: Mapping[str, Any] | None = None,
    news: Mapping[str, Any] | None = None,
    fund_flow: Mapping[str, float] | None = None,
    market_bias: str | None = None,
) -> dict[str, Any]:
    """为 bars 的最后一个交易日生成次日做T计划。

    参数 bundle 可传入预计算的 compute_indicators 结果（回测逐日推进时避免
    重复计算指标）；不传则内部计算一次。
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update({k: (v if isinstance(v, str) else float(v)) for k, v in config.items() if k in cfg})
    if len(bars) < 30:
        return {
            "ok": False, "schema_version": PLAN_SCHEMA_VERSION, "model_version": MODEL_VERSION,
            "reason": f"insufficient bars: {len(bars)} < 30",
        }
    if bundle is None:
        from scripts.technical_indicators import compute_indicators
        bundle = compute_indicators(bars)

    index = len(bars) - 1
    bar = bars[index]
    close = float(bar["close"])
    atr = _series_value(bundle, ("atr", "atr14"), index)
    price_score, features = compute_score(bars, bundle, index)

    # 新闻情绪因子：来自 news_sentiment.py 的规则词库打分（标的专属优先，
    # 缺失时按 0.6 权重折算大盘）。无历史归档不可回测，权重保守。
    news_adjust = 0.0
    news_info: dict[str, Any] = {"available": False}
    if news:
        raw_sent = news.get("sentiment")
        if isinstance(raw_sent, (int, float)):
            news_adjust = float(cfg["news_weight"]) * float(raw_sent)
            news_info = {
                "available": True,
                "sentiment": round(float(raw_sent), 3),
                "positive": int(news.get("positive") or 0),
                "negative": int(news.get("negative") or 0),
                "fallback_to_market": bool(news.get("fallback_to_market")),
                "headlines": list(news.get("headlines") or [])[:2],
            }
    # 主力资金流因子：最近 3 个有数据交易日的主力净占比均值（上游按成交额
    # 归一，%），÷15 归一到 [-1,1]。有日K历史，可回测寻优 fund_weight。
    fund_adjust = 0.0
    fund_info: dict[str, Any] = {"available": False}
    if fund_flow:
        dates_before = [str(bar.get("time") or "") for bar in bars[: index + 1]]
        recent = [
            float(fund_flow[date])
            for date in dates_before
            if date in fund_flow and fund_flow[date] is not None
        ][-3:]
        if recent:
            mean_pct = sum(recent) / len(recent)
            fund_adjust = float(cfg["fund_weight"]) * _clip(mean_pct / 15.0)
            fund_info = {
                "available": True,
                "days": len(recent),
                "mean_pct_3d": round(mean_pct, 2),
                "last_pct": round(recent[-1], 2),
            }
    score = _clip(price_score + news_adjust + fund_adjust)
    confidence = min(1.0, abs(score) / 0.8)

    theta = float(cfg["theta"])
    if score >= theta:
        bias, kind = "up", "正T（先买后卖）"
    elif score <= -theta:
        bias, kind = "down", "反T（先卖后买）"
    else:
        bias, kind = "flat", "观望"

    # 大盘风险闸门：大盘次日偏空时压制个股正T（逆势接飞刀风险最高）
    market_gate = "off"
    if market_bias == "down":
        if kind.startswith("正T"):
            kind = "观望（大盘走弱压制正T）"
            market_gate = "demoted"
        else:
            market_gate = "pass"
    elif market_bias == "up":
        market_gate = "pass"

    rationale = list(_rationale(features, bias, score))
    if fund_info.get("available"):
        tone = "净流入" if fund_info["mean_pct_3d"] > 0 else "净流出"
        rationale.insert(0, f"资金面：3日主力净{tone}占比均值 {fund_info['mean_pct_3d']:+.2f}%")
    if news_info.get("available"):
        tone = "偏正面" if news_adjust > 0 else "偏负面" if news_adjust < 0 else "中性"
        rationale.insert(0, f"新闻情绪{tone}({news_info['sentiment']:+.2f})：利好{news_info['positive']}条/利空{news_info['negative']}条")
    plan: dict[str, Any] = {
        "ok": True,
        "schema_version": PLAN_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "date": str(bar.get("time") or ""),
        "bias": bias,
        "market_bias": market_bias,
        "market_gate": market_gate,
        "score": round(score, 3),
        "price_score": round(price_score, 3),
        "news_adjust": round(news_adjust, 3),
        "news": news_info,
        "fund": fund_info,
        "fund_adjust": round(fund_adjust, 3),
        "confidence": round(confidence, 3),
        "kind": kind,
        "prev_close": close,
        "atr14": round(atr, 4) if atr else None,
        "buy_target": None,
        "sell_target": None,
        "stop_guard": None,
        "rationale": rationale,
        "features": {k: round(v, 3) for k, v in features.items()},
        "disclaimer": "模型参考信号，不构成投资建议",
    }
    amplitude = _amplitude_stats(bars, index)
    plan["amplitude"] = amplitude

    if atr and atr > 0:
        # 置信度-区间耦合：高置信收窄（更易成交）、低置信放宽（要求更大波动）
        coupling = float(cfg.get("zone_coupling", 0.0))
        zone_scale = 1.0
        if coupling > 0:
            zone_scale = max(0.75, min(1.45, 1.0 + coupling * (0.55 - confidence)))
        zone_basis = str(cfg.get("zone_basis", "atr"))
        if zone_basis == "percentile" and amplitude.get("days", 0) >= 30:
            # 科学幅度：按该股历史日内波动结构的分位定价，并结合次日倾向做
            # 不对称调整——正T（偏多）买浅卖深、反T（偏空）卖浅买深。
            if bias == "down":
                dip_pct = amplitude.get("p35_rip")
                rip_pct = amplitude.get("p65_dip")
            else:
                dip_pct = amplitude.get("p35_dip")
                rip_pct = amplitude.get("p65_rip")
            if dip_pct is None or rip_pct is None or dip_pct <= 0 or rip_pct <= 0:
                dip_pct = amplitude.get("median_dip")
                rip_pct = amplitude.get("median_rip")
            if dip_pct and rip_pct:
                buy_target = close * (1.0 - dip_pct / 100.0)
                sell_target = close * (1.0 + rip_pct / 100.0)
                if zone_scale != 1.0:
                    buy_target = close + (buy_target - close) * zone_scale
                    sell_target = close + (sell_target - close) * zone_scale
                stop = close - float(cfg["stop_atr"]) * atr if bias != "down" else close + float(cfg["stop_atr"]) * atr
                buy_target = round(buy_target, 2)
                sell_target = round(sell_target, 2)
                stop = round(stop, 2)
                buy_pct = round((buy_target / close - 1.0) * 100.0, 2)
                sell_pct = round((sell_target / close - 1.0) * 100.0, 2)
                stop_pct = round((stop / close - 1.0) * 100.0, 2)
                if bias == "down":
                    triggers = [
                        {"step": 1, "label": "冲高卖出", "move_pct": sell_pct, "price": sell_target, "action": "卖出部分底仓（反T第一步）"},
                        {"step": 2, "label": "回落买回", "move_pct": buy_pct, "price": buy_target, "action": "买回等量（反T完成）"},
                        {"step": 0, "label": "放弃线", "move_pct": stop_pct, "price": stop, "action": "跳空越过则放弃反T"},
                    ]
                else:
                    triggers = [
                        {"step": 1, "label": "回落买入", "move_pct": buy_pct, "price": buy_target, "action": "买入等量底仓（正T第一步）"},
                        {"step": 2, "label": "冲高卖出", "move_pct": sell_pct, "price": sell_target, "action": "卖出等量（正T完成）"},
                        {"step": 0, "label": "放弃线", "move_pct": stop_pct, "price": stop, "action": "跳空跌破则放弃正T"},
                    ]
                plan.update(
                    buy_target=buy_target, sell_target=sell_target, stop_guard=stop,
                    buy_pct=buy_pct, sell_pct=sell_pct, stop_pct=stop_pct,
                    triggers=triggers, zone_basis="percentile",
                )
                return plan
        buy_target = close - float(cfg["buy_atr"]) * atr * zone_scale
        sell_target = close + float(cfg["sell_atr"]) * atr * zone_scale
        stop = close - float(cfg["stop_atr"]) * atr if bias != "down" else close + float(cfg["stop_atr"]) * atr
        buy_target = round(buy_target, 2)
        sell_target = round(sell_target, 2)
        stop = round(stop, 2)
        # 百分比按取整后的展示价计算，保证页面显示的价格与涨跌幅完全一致
        buy_pct = round((buy_target / close - 1.0) * 100.0, 2)
        sell_pct = round((sell_target / close - 1.0) * 100.0, 2)
        stop_pct = round((stop / close - 1.0) * 100.0, 2)
        # 按涨跌幅度的分步触发指引（相对前收的百分比 + 绝对价格）
        if bias == "down":
            triggers = [
                {"step": 1, "label": "冲高卖出", "move_pct": sell_pct, "price": sell_target, "action": "卖出部分底仓（反T第一步）"},
                {"step": 2, "label": "回落买回", "move_pct": buy_pct, "price": buy_target, "action": "买回等量（反T完成）"},
                {"step": 0, "label": "放弃线", "move_pct": stop_pct, "price": stop, "action": "跳空越过则放弃反T"},
            ]
        else:
            triggers = [
                {"step": 1, "label": "回落买入", "move_pct": buy_pct, "price": round(buy_target, 2), "action": "买入等量底仓（正T第一步）"},
                {"step": 2, "label": "冲高卖出", "move_pct": sell_pct, "price": round(sell_target, 2), "action": "卖出等量（正T完成）"},
                {"step": 0, "label": "放弃线", "move_pct": stop_pct, "price": round(stop, 2), "action": "跳空跌破则放弃正T"},
            ]
        plan.update(
            buy_target=round(buy_target, 2),
            sell_target=round(sell_target, 2),
            stop_guard=round(stop, 2),
            buy_pct=buy_pct,
            sell_pct=sell_pct,
            stop_pct=stop_pct,
            triggers=triggers,
        )
    return plan
