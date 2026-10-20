"""次日做T计划的 walk-forward 回测与参数寻优。

回测口径
--------
* 严格 walk-forward：t 日计划只用 ≤t 的数据（nextday_model.compute_plan 为
  因果纯函数，指标序列亦因果），t+1 日用真实 OHLC 模拟做T执行。
* 做T模拟（基于底仓，A股 T+1 下的日内回转）：
  - 正T（先买后卖）：次日最低价触及 buy_target 即按该价买入（跳空低开按
    开盘价、更优），当日最高价触及 sell_target 即卖出等量底仓；未触卖出
    则按收盘价盯市（代表"T失败收尾"）。开盘跌破 stop_guard 视为趋势破坏，
    放弃当日做T。
  - 反T（先卖后买）：镜像逻辑。
  - 观望：不动。
* 收益以 bp（万分之 perv_close）计；未计手续费与滑点，结果仅供模型间
  相对比较，不构成收益承诺。

参数寻优：对 (buy_atr, sell_atr, theta) 的小网格在全部持仓上做聚合
walk-forward（合计交易数达标者中取 sum_bps 最高），并输出逐股明细。
"""

from __future__ import annotations

import itertools
from typing import Any, Mapping, Sequence

from scripts.nextday_model import (
    DEFAULT_CONFIG,
    MODEL_VERSION,
    compute_plan,
)

BACKTEST_VERSION = "nextday-bt.2026-09-29.1"
SCHEMA_VERSION = "nextday-bt.v1"
DEFAULT_WARMUP = 60
DEFAULT_MIN_BARS = 90
DEFAULT_MIN_TRADES = 30
DEFAULT_GRID: dict[str, tuple[float, ...]] = {
    "buy_atr": (0.40, 0.55, 0.70),
    "sell_atr": (0.50, 0.80, 1.10),
    "theta": (0.25, 0.40),
}


def simulate_t_day(
    plan: Mapping[str, Any],
    next_bar: Mapping[str, Any],
) -> dict[str, Any]:
    """按 t+1 的真实 OHLC 模拟一份做T计划的执行结果。"""
    result: dict[str, Any] = {
        "traded": False,
        "completed": False,
        "profit": None,
        "bps": None,
        "direction_hit": None,
        "reason": "",
    }
    bias = str(plan.get("bias") or "")
    kind = str(plan.get("kind") or "")
    prev_close = plan.get("prev_close")
    atr = plan.get("atr14")
    if bias == "flat" or not atr or not prev_close:
        result["reason"] = "观望"
        return result
    if plan.get("market_gate") == "demoted":
        nxt = float(next_bar["close"])
        result["direction_hit"] = (nxt > float(prev_close)) if bias == "up" else (nxt < float(prev_close))
        result["reason"] = "大盘走弱，正T降级观望"
        return result
    prev_close = float(prev_close)
    atr = float(atr)
    nxt_close = float(next_bar["close"])
    result["direction_hit"] = (
        nxt_close > prev_close if bias == "up" else nxt_close < prev_close
    )
    buy = plan.get("buy_target")
    sell = plan.get("sell_target")
    stop = plan.get("stop_guard")
    if buy is None or sell is None or stop is None:
        result["reason"] = "计划缺价位"
        return result
    buy = float(buy)
    sell = float(sell)
    stop = float(stop)
    open_ = float(next_bar.get("open") or nxt_close)
    high = float(next_bar.get("high") or nxt_close)
    low = float(next_bar.get("low") or nxt_close)

    if kind.startswith("正T"):
        if open_ <= stop:
            result["reason"] = "跳空过低，放弃"
            return result
        if low > buy:
            result["reason"] = "未触及买点"
            return result
        entry = min(open_, buy)
        exit_price = sell if high >= sell else nxt_close
        profit = exit_price - entry
        completed = high >= sell
    else:  # 反T（先卖后买）
        if open_ >= stop:
            result["reason"] = "跳空过高，放弃"
            return result
        if high < sell:
            result["reason"] = "未触及卖点"
            return result
        entry = max(open_, sell)
        exit_price = buy if low <= buy else nxt_close
        profit = entry - exit_price
        completed = low <= buy

    result.update(
        traded=True,
        completed=completed,
        profit=round(profit, 4),
        bps=round(profit / prev_close * 10000.0, 2),
        reason="两腿成交" if completed else "单腿成交，收盘盯市",
    )
    return result


def _market_bias_by_date(
    market_bars: Sequence[Mapping[str, Any]] | None, warmup: int
) -> dict[str, str] | None:
    """预计算大盘逐日次日倾向（供个股回测的风险闸门）。"""
    if not market_bars:
        return None
    from scripts.technical_indicators import compute_indicators

    market_list = list(market_bars)
    if len(market_list) < warmup + 2:
        return None
    bundle = compute_indicators(market_list)
    bias_by_date: dict[str, str] = {}
    for index in range(warmup, len(market_list)):
        plan = compute_plan(market_list[: index + 1], bundle=bundle)
        if plan.get("ok"):
            bias_by_date[str(plan.get("date") or "")] = str(plan.get("bias") or "")
    return bias_by_date


def _walk_days(
    bars: Sequence[Mapping[str, Any]],
    config: Mapping[str, float],
    bundle: Mapping[str, Any],
    warmup: int,
    fund_flow: Mapping[str, float] | None = None,
    market_bias_by_date: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    days: list[dict[str, Any]] = []
    for index in range(warmup, len(bars) - 1):
        plan = compute_plan(
            bars[: index + 1], config, bundle=bundle, fund_flow=fund_flow,
            market_bias=(market_bias_by_date or {}).get(str(bars[index].get("time") or "")),
        )
        if not plan.get("ok"):
            continue
        days.append(simulate_t_day(plan, bars[index + 1]))
    return days


def _aggregate(days: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    trades = [day for day in days if day.get("traded")]
    profits = [float(day["profit"]) for day in trades if day.get("profit") is not None]
    bps = [float(day["bps"]) for day in trades if day.get("bps") is not None]
    completed = sum(1 for day in trades if day.get("completed"))
    wins = sum(1 for value in profits if value > 0)
    directions = [
        day["direction_hit"] for day in days if day.get("direction_hit") is not None
    ]
    hits = sum(1 for value in directions if value)
    return {
        "days_evaluated": len(days),
        "trades": len(trades),
        "completed": completed,
        "completed_rate": round(completed / len(trades), 4) if trades else None,
        "win_rate": round(wins / len(profits), 4) if profits else None,
        "avg_bps": round(sum(bps) / len(bps), 2) if bps else None,
        "sum_bps": round(sum(bps), 2),
        "worst_bps": round(min(bps), 2) if bps else None,
        "direction_total": len(directions),
        "direction_hit_rate": round(hits / len(directions), 4) if directions else None,
    }


def walk_forward(
    bars: Sequence[Mapping[str, Any]],
    config: Mapping[str, float] | None = None,
    *,
    bundle: Mapping[str, Any] | None = None,
    warmup: int = DEFAULT_WARMUP,
    fund_flow: Mapping[str, float] | None = None,
    market_bars: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """单只标的的 walk-forward 回测摘要。"""
    from scripts.technical_indicators import compute_indicators

    bar_list = list(bars)
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update({k: (v if isinstance(v, str) else float(v)) for k, v in config.items() if k in cfg})
    if len(bar_list) < DEFAULT_MIN_BARS:
        return {
            "ok": False,
            "error": f"insufficient bars: {len(bar_list)} < {DEFAULT_MIN_BARS}",
            "schema_version": SCHEMA_VERSION,
            "backtest_version": BACKTEST_VERSION,
        }
    if bundle is None:
        bundle = compute_indicators(bar_list)
    market_bias_by_date = _market_bias_by_date(market_bars, warmup)
    days = _walk_days(bar_list, cfg, bundle, warmup, fund_flow=fund_flow,
                      market_bias_by_date=market_bias_by_date)
    summary = _aggregate(days)
    summary["recent_10"] = _aggregate(days[-10:])
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "backtest_version": BACKTEST_VERSION,
        "model_version": MODEL_VERSION,
        "config": cfg,
        **summary,
    }


def optimize_symbol(
    bars: Sequence[Mapping[str, Any]],
    grid: Mapping[str, Sequence[float]] | None = None,
    *,
    warmup: int = DEFAULT_WARMUP,
    min_trades: int = 25,
    fund_flow: Mapping[str, float] | None = None,
    fund_grid: Sequence[float] = (0.0, 0.5, 1.0),
    market_bars: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """单只标的的参数寻优：网格内取 sum_bps 最高且交易数达标的组合。

    附带最优参数下的近 60 个评估日统计（recent），用于展示近期做T成功率。
    样本内寻优存在过拟合风险，故网格受限并要求最低交易数。
    """
    from scripts.technical_indicators import compute_indicators

    bar_list = list(bars)
    if len(bar_list) < DEFAULT_MIN_BARS:
        return {
            "ok": False,
            "error": f"insufficient bars: {len(bar_list)} < {DEFAULT_MIN_BARS}",
            "schema_version": SCHEMA_VERSION,
            "backtest_version": BACKTEST_VERSION,
        }
    bundle = compute_indicators(bar_list)
    market_bias_by_date = _market_bias_by_date(market_bars, warmup)
    cfg_grid = dict(DEFAULT_GRID if grid is None else grid)
    keys = sorted(cfg_grid.keys())
    rows: list[dict[str, Any]] = []
    for combo in itertools.product(*(cfg_grid[key] for key in keys)):
        config = dict(zip(keys, (float(value) for value in combo)))
        days = _walk_days(bar_list, config, bundle, warmup,
                          market_bias_by_date=market_bias_by_date)
        rows.append({"config": config, **_aggregate(days)})
    eligible = [row for row in rows if row["trades"] >= min_trades]
    if not eligible:
        return {
            "ok": False,
            "error": f"no combo reached {min_trades} trades",
            "grid_size": len(rows),
            "schema_version": SCHEMA_VERSION,
            "backtest_version": BACKTEST_VERSION,
        }
    best = max(eligible, key=lambda row: row["sum_bps"] or 0.0)
    # 二阶段寻优：固定最优区间参数，网格资金因子权重（有日K历史，可回测）
    if fund_flow and fund_grid:
        variants = []
        for fw in fund_grid:
            config = dict(best["config"], fund_weight=float(fw))
            days = _walk_days(bar_list, config, bundle, warmup, fund_flow=fund_flow,
                              market_bias_by_date=market_bias_by_date)
            variants.append({"config": config, **_aggregate(days)})
        for fw in (0.5, 1.0):
            config = dict(best["config"], fund_weight=float(fw), zone_basis="percentile")
            days = _walk_days(bar_list, config, bundle, warmup, fund_flow=fund_flow,
                              market_bias_by_date=market_bias_by_date)
            variants.append({"config": config, **_aggregate(days)})
        fund_best = max(variants, key=lambda row: row["sum_bps"] or 0.0)
        if (fund_best["sum_bps"] or 0.0) > (best["sum_bps"] or 0.0):
            best = fund_best
    recent = _aggregate(_walk_days(
        bar_list, best["config"], bundle, warmup, fund_flow=fund_flow,
        market_bias_by_date=market_bias_by_date
    )[-60:])
    recent_10 = _aggregate(_walk_days(
        bar_list, best["config"], bundle, warmup, fund_flow=fund_flow,
        market_bias_by_date=market_bias_by_date
    )[-10:])
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "backtest_version": BACKTEST_VERSION,
        "config": best["config"],
        "summary": best,
        "recent": recent,
        "recent_10": recent_10,
        "grid_size": len(rows),
        "eligible": len(eligible),
    }


def direction_diagnostics(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    warmup: int = DEFAULT_WARMUP,
) -> dict[str, Any]:
    """逐特征次日方向命中率诊断（权重迭代的前置证据）。

    对每个特征统计：特征值 >0 / <0 时次日收盘相对前收的方向命中率，
    以及"按特征符号下注"的合并命中率。另含当前加权得分的基线命中率。
    """
    from scripts.technical_indicators import compute_indicators
    from scripts.nextday_model import compute_score

    feature_names = ("trend", "momentum", "volume", "stretch", "close_pos")
    # name → {"pos_hit","pos_n","neg_hit","neg_n"}
    stats: dict[str, dict[str, int]] = {
        name: {"pos_hit": 0, "pos_n": 0, "neg_hit": 0, "neg_n": 0}
        for name in feature_names
    }
    score_stats = {"pos_hit": 0, "pos_n": 0, "neg_hit": 0, "neg_n": 0}
    reversal_stats = {"pos_hit": 0, "pos_n": 0, "neg_hit": 0, "neg_n": 0}

    for _symbol, bars in bars_by_symbol.items():
        bar_list = list(bars)
        if len(bar_list) < warmup + 2:
            continue
        bundle = compute_indicators(bar_list)
        for index in range(warmup, len(bar_list) - 1):
            prev_close = bar_list[index].get("close")
            nxt_close = bar_list[index + 1].get("close")
            if not prev_close or not nxt_close:
                continue
            up = nxt_close > prev_close
            _score, features = compute_score(bar_list, bundle, index)
            # 反向特征：当日涨幅的反向（A股短线反转假说）
            ret = (float(bar_list[index]["close"]) / float(prev_close) - 1.0) if prev_close else 0.0
            values = {**{f"feat_{k}": v for k, v in features.items()},
                      "feat_reversal": -ret,
                      "feat_score": _score}
            for name, value in values.items():
                bucket = stats.setdefault(name, {"pos_hit": 0, "pos_n": 0, "neg_hit": 0, "neg_n": 0})
                if value > 0:
                    bucket["pos_n"] += 1
                    bucket["pos_hit"] += 1 if up else 0
                elif value < 0:
                    bucket["neg_n"] += 1
                    bucket["neg_hit"] += 1 if not up else 0

    result = {}
    for name, bucket in stats.items():
        total = bucket["pos_n"] + bucket["neg_n"]
        pooled_hits = bucket["pos_hit"] + bucket["neg_hit"]
        result[name] = {
            "coverage": total,
            "pos_hit_rate": round(bucket["pos_hit"] / bucket["pos_n"], 4) if bucket["pos_n"] else None,
            "neg_hit_rate": round(bucket["neg_hit"] / bucket["neg_n"], 4) if bucket["neg_n"] else None,
            "pooled_hit_rate": round(pooled_hits / total, 4) if total else None,
        }
    return {
        "ok": True,
        "schema_version": "nextday-diagnostics.v1",
        "features": result,
        "note": "pooled_hit_rate = 按特征符号下注的次日方向命中率；50% 为随机基线",
    }


def grid_search(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    grid: Mapping[str, Sequence[float]] | None = None,
    *,
    warmup: int = DEFAULT_WARMUP,
    min_trades: int = DEFAULT_MIN_TRADES,
    fund_flow_by_symbol: Mapping[str, Mapping[str, float]] | None = None,
    market_bars: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """在多只标的上聚合的参数网格寻优。

    目标函数：合计 sum_bps（bp 收益之和），要求合计交易数 ≥ min_trades；
    满足条件的组合里取最高。返回全部组合的聚合表与最优组合。
    """
    from scripts.technical_indicators import compute_indicators

    cfg_grid = dict(DEFAULT_GRID if grid is None else grid)
    keys = sorted(cfg_grid.keys())
    combos = list(itertools.product(*(cfg_grid[key] for key in keys)))

    prepared = []
    for symbol, bars in bars_by_symbol.items():
        bar_list = list(bars)
        if len(bar_list) < DEFAULT_MIN_BARS:
            continue
        prepared.append((symbol, bar_list, compute_indicators(bar_list)))
    market_bias_by_date = _market_bias_by_date(market_bars, warmup)
    if not prepared:
        return {
            "ok": False,
            "error": "no symbol has enough bars",
            "schema_version": SCHEMA_VERSION,
            "backtest_version": BACKTEST_VERSION,
        }

    rows: list[dict[str, Any]] = []
    for combo in combos:
        config = dict(zip(keys, (float(value) for value in combo)))
        pooled: list[dict[str, Any]] = []
        for _symbol, bar_list, bundle in prepared:
            pooled.extend(_walk_days(
                bar_list, config, bundle, warmup,
                fund_flow=(fund_flow_by_symbol or {}).get(_symbol),
                market_bias_by_date=market_bias_by_date,
            ))
        summary = _aggregate(pooled)
        rows.append({"config": config, **summary})

    eligible = [row for row in rows if row["trades"] >= min_trades]
    pool = eligible or rows
    best = max(pool, key=lambda row: row["sum_bps"] or 0.0)

    per_symbol: dict[str, Any] = {}
    for symbol, bar_list, bundle in prepared:
        days = _walk_days(bar_list, best["config"], bundle, warmup,
                          fund_flow=(fund_flow_by_symbol or {}).get(symbol),
                          market_bias_by_date=market_bias_by_date)
        per_symbol[symbol] = {**_aggregate(days), "recent_10": _aggregate(days[-10:])}

    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "backtest_version": BACKTEST_VERSION,
        "model_version": MODEL_VERSION,
        "symbols": len(prepared),
        "grid_size": len(rows),
        "min_trades": min_trades,
        "eligible_combos": len(eligible),
        "best": best,
        "grid": sorted(
            rows,
            key=lambda row: row["sum_bps"] or 0.0,
            reverse=True,
        ),
        "per_symbol": per_symbol,
        "disclaimer": "回测基于历史日线、未计手续费滑点，仅供模型相对比较，不构成投资建议",
    }
