#!/usr/bin/env python3
"""模拟盘组合反T执行引擎（paper trading for the portfolio reverse-T signal）.

职责：把 ``agg_t_signal`` 的信号阶段翻译成"允许执行哪一腿"，生成模拟订单、
按最新价撮合、按估算费用模型计费并汇总已实现盈亏。所有写入都通过
``paper_t_store``（追加式哈希链台账），不触碰真实券商账户。

状态机（当日会话）：
    idle --触发时模拟卖出--> sold --信号了结后模拟买回--> done
    任意未完成状态 --撤销(void)--> voided（统计剔除）

费用模型（估算，可按券商实际费率调整）：
    佣金 万2.5 双边、单笔最低5元；印花税 0.05% 仅卖出（股票）；
    过户费 0.001% 双边（股票）。ETF 免印花税与过户费。

估算数据，非投资建议。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from scripts import agg_t_signal as agg_t_signal_module  # type: ignore
except ModuleNotFoundError:  # direct execution
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import agg_t_signal as agg_t_signal_module  # type: ignore[no-redef]

FEE_MODEL = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_rate": 0.0005,
    "transfer_rate": 0.00001,
}
FEE_NOTE = (
    "费用估算：佣金万2.5（最低5元/笔）双边，印花税0.05%仅卖出，过户费0.001%双边；"
    "ETF免印花税与过户费。"
)
ETF_PREFIXES = ("51", "56", "58", "15", "50")

# 自动执行（本地模拟盘）：信号翻转即自动模拟成交，无需人工下单。
AUTO_MODE = "full"  # 仓位口径：全篮子（回测验证口径）
AUTO_MAX_DELAY_SECONDS = 180.0  # 信号翻转后超过该时长未执行＝离线错过，跳过不追价


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def auto_decision(
    phase: str,
    summary: Mapping[str, Any],
    signal: Mapping[str, Any],
    now: datetime,
    *,
    direction: str = "reverse",
) -> tuple[str, str]:
    """自动执行决策（纯函数）：返回 ("sell"|"buy"|"skip", 原因)。

    纪律与回测一致，另加"离线错过不追价"约束：
    - 反T（direction="reverse"）：phase=triggered 且触发点在
      AUTO_MAX_DELAY_SECONDS 内（服务重启后发现的是陈旧触发，按现价补成交会
      污染有效性检验，宁可不做）；phase=closed 才买回。
    - 正T（direction="forward"）：与反T 互为镜像——phase=triggered 是**低吸
      买入**，phase=closed 是**高抛卖出**。两者的腿序必须与信号语义一致，
      否则会在下跌时先卖、反弹时买回，与策略意图完全相反。

    买回/卖出腿的时效门槛同样适用（completed=True 时了结点须新鲜）；
    completed=False 为 15:00 强制平仓，了结点本就是收盘，不设新鲜度门槛。
    """

    leg = str(summary.get("leg") or "idle")
    if summary.get("voided"):
        return "skip", "当日会话已撤销"
    forward = str(direction) == "forward"
    # 会话状态：idle=尚未建仓；"sold"(反T)/"bought"(正T)=第一腿已成交、等待第二腿。
    open_state = "bought" if forward else "sold"
    if leg == "idle" and phase == "triggered":
        entry = (signal.get("entry") or {}) if isinstance(signal.get("entry"), dict) else {}
        entry_at = _parse_iso(entry.get("time"))
        if entry_at is None:
            return "skip", "触发点时间缺失"
        delay = (now - entry_at).total_seconds()
        if delay > AUTO_MAX_DELAY_SECONDS:
            return "skip", f"触发已过去 {delay:.0f} 秒（离线错过），跳过当日建仓"
        if forward:
            return "buy", "信号触发，自动模拟买入（正T低吸）"
        return "sell", "信号触发，自动模拟卖出"
    if leg == open_state and phase == "closed":
        if bool(signal.get("completed")):
            exit_ = (signal.get("exit") or {}) if isinstance(signal.get("exit"), dict) else {}
            exit_at = _parse_iso(exit_.get("time"))
            if exit_at is not None:
                delay = (now - exit_at).total_seconds()
                if delay > AUTO_MAX_DELAY_SECONDS:
                    return "skip", f"了结点已过去 {delay:.0f} 秒（离线错过），不追价回补"
        if forward:
            return "sell", "信号了结，自动模拟卖出（正T高抛）"
        return "buy", "信号了结，自动模拟买回"
    return "skip", ""


class PaperTActionError(Exception):
    """模拟执行请求与信号/会话状态冲突（映射为 HTTP 409）。"""


def is_etf(symbol: str) -> bool:
    code = str(symbol or "")
    return code[:2] in ETF_PREFIXES


def leg_fees(symbol: str, side: str, notional: float) -> float:
    """单腿估算费用（元）。``side``: sell/buy。"""

    fees = max(FEE_MODEL["commission_min"], FEE_MODEL["commission_rate"] * notional)
    if not is_etf(symbol):
        fees += FEE_MODEL["transfer_rate"] * notional
        if side == "sell":
            fees += FEE_MODEL["stamp_rate"] * notional
    return round(fees, 2)


def plan_orders(
    holdings: Sequence[Mapping[str, Any]],
    prices: Mapping[str, float],
    action: str,
    mode: str = "sample",
) -> Dict[str, Any]:
    """生成模拟委托计划。``mode``: sample（示例仓位）| full（全篮子，回测口径）。"""

    if action not in ("sell", "buy"):
        raise ValueError(f"unknown action: {action}")
    if mode not in ("sample", "full"):
        raise ValueError(f"unknown mode: {mode}")
    if mode == "full":
        rows: List[Dict[str, Any]] = []
        total_notional = 0.0
        for holding in holdings:
            symbol = str(holding.get("symbol") or "")
            try:
                quantity = int(holding.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            price = float(prices.get(symbol) or 0.0)
            if quantity <= 0 or price <= 0:
                continue
            notional = round(quantity * price, 2)
            total_notional += notional
            rows.append(
                {
                    "symbol": symbol,
                    "name": str(holding.get("name") or symbol),
                    "action": action,
                    "action_label": "卖出" if action == "sell" else "买入",
                    "shares": quantity,
                    "price": price,
                    "notional": notional,
                }
            )
        sizing = "全篮子：按持仓全部数量（回测验证口径），参考价为最新价"
        return {
            "mode": mode,
            "rows": rows,
            "total_notional": round(total_notional, 2),
            "count": len(rows),
            "sizing": sizing,
        }
    sample = agg_t_signal_module.suggested_orders(holdings, prices, action)
    sample["mode"] = mode
    return sample


def buy_rows_from_fills(
    sell_fills: Sequence[Mapping[str, Any]],
    prices: Mapping[str, float],
) -> Dict[str, Any]:
    """按已成交的卖出腿逐股等量生成买回计划；缺行情则整体拒绝。"""

    missing: List[str] = []
    rows: List[Dict[str, Any]] = []
    total_notional = 0.0
    for fill in sell_fills:
        symbol = str(fill.get("symbol") or "")
        try:
            shares = int(fill.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        price = float(prices.get(symbol) or 0.0)
        if shares <= 0:
            continue
        if price <= 0:
            missing.append(symbol)
            continue
        notional = round(shares * price, 2)
        total_notional += notional
        rows.append(
            {
                "symbol": symbol,
                "name": str(fill.get("name") or symbol),
                "action": "buy",
                "action_label": "买入",
                "shares": shares,
                "price": price,
                "notional": notional,
            }
        )
    if missing:
        raise PaperTActionError(
            "以下卖出标的缺少最新行情，无法生成等量买回清单：" + "、".join(missing)
        )
    return {
        "mode": "match",
        "rows": rows,
        "total_notional": round(total_notional, 2),
        "count": len(rows),
        "sizing": "等量买回：逐股买回卖出腿的全部数量（参考价为最新价）",
    }


def sell_rows_from_fills(
    buy_fills: Sequence[Mapping[str, Any]],
    prices: Mapping[str, float],
) -> Dict[str, Any]:
    """按已成交的买入腿逐股等量生成卖出计划（正T第二腿：低吸后高抛）。

    与 :func:`buy_rows_from_fills` 互为镜像；缺行情则整体拒绝，避免按错价
    成交把模拟账本污染成不可复核的状态。
    """

    missing: List[str] = []
    rows: List[Dict[str, Any]] = []
    total_notional = 0.0
    for fill in buy_fills:
        symbol = str(fill.get("symbol") or "")
        try:
            shares = int(fill.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        price = float(prices.get(symbol) or 0.0)
        if shares <= 0:
            continue
        if price <= 0:
            missing.append(symbol)
            continue
        notional = round(shares * price, 2)
        total_notional += notional
        rows.append(
            {
                "symbol": symbol,
                "name": str(fill.get("name") or fill["symbol"]),
                "action": "sell",
                "action_label": "卖出",
                "shares": shares,
                "price": price,
                "notional": notional,
            }
        )
    if missing:
        raise PaperTActionError(
            "以下买入标的缺少最新行情，无法生成等量卖出清单：" + "、".join(missing)
        )
    return {
        "mode": "match",
        "rows": rows,
        "total_notional": round(total_notional, 2),
        "count": len(rows),
        "sizing": "等量卖出：逐股卖出买入腿的全部数量（参考价为最新价）",
    }


def session_summary(
    records: Sequence[Mapping[str, Any]], *, direction: str = "reverse"
) -> Dict[str, Any]:
    """从台账记录推导当日模拟会话状态与已实现盈亏（幂等，纯函数）。

    ``direction`` 与策略方向一致：``reverse``（反T，先卖后买）、
    ``forward``（正T，先买后卖）。两条腿的先后顺序不同，会话状态与已实现
    盈亏的符号都必须随之改变——正T 先买后卖，卖出腿是"高抛"兑现，买入腿
    是"低吸"建仓。

    已实现盈亏只统计**已配对**的腿（同一标的既有第一腿又有第二腿）。未配对
    的卖出腿代表尚未买回的敞口（反T）或尚未高抛的持仓（正T），把它当作
    已实现利润会凭空造出巨额收益。
    """

    forward = str(direction) == "forward"
    meta: Dict[str, Any] = {}
    sells: List[Dict[str, Any]] = []
    buys: List[Dict[str, Any]] = []
    voided = False
    void_reason = ""
    for record in records or []:
        event = str(record.get("event") or "")
        if event == "session_open" and "mode" not in meta:
            meta = {
                "mode": record.get("mode"),
                "th_bp": record.get("th_bp"),
                "mv": record.get("mv"),
            }
        elif event == "fill":
            row = {
                "ts": record.get("ts"),
                "leg": record.get("leg"),
                "symbol": record.get("symbol"),
                "name": record.get("name"),
                "shares": record.get("shares"),
                "price": record.get("price"),
                "notional": record.get("notional"),
                "fees": record.get("fees"),
            }
            (sells if record.get("leg") == "sell" else buys).append(row)
        elif event == "void":
            voided = True
            void_reason = str(record.get("reason") or "")
    sell_notional = round(sum(float(row["notional"] or 0.0) for row in sells), 2)
    buy_notional = round(sum(float(row["notional"] or 0.0) for row in buys), 2)
    fees = round(sum(float(row["fees"] or 0.0) for row in sells + buys), 2)
    # 逐股配对（同一天同一代码两腿都有）：只有配对成功的部分才算已实现盈亏。
    # 反T：卖出在前、买回在后 → 盈亏 = 卖出额 − 买回额 − 费用。
    # 正T：买入在前、卖出在后 → 盈亏 = 卖出额 − 买入额 − 费用。
    sell_by_symbol: Dict[str, Dict[str, Any]] = {}
    for row in sells:
        sell_by_symbol.setdefault(str(row["symbol"]), row)
    buy_by_symbol: Dict[str, Dict[str, Any]] = {}
    for row in buys:
        buy_by_symbol.setdefault(str(row["symbol"]), row)
    matched_cny = 0.0
    matched_count = 0
    unmatched_symbols: List[str] = []
    for symbol in set(sell_by_symbol) | set(buy_by_symbol):
        sell_row = sell_by_symbol.get(symbol)
        buy_row = buy_by_symbol.get(symbol)
        if sell_row is None or buy_row is None:
            # 只有一腿：仍是敞口，不是已实现盈亏。
            unmatched_symbols.append(symbol)
            continue
        matched_cny += (
            float(sell_row["notional"] or 0.0)
            - float(buy_row["notional"] or 0.0)
            - float(sell_row["fees"] or 0.0)
            - float(buy_row["fees"] or 0.0)
        )
        matched_count += 1
    matched_cny = round(matched_cny, 2)
    mv = float(meta.get("mv") or 0.0)
    realized_cny: Optional[float] = None
    realized_bp: Optional[float] = None
    leg = "idle"
    if voided:
        leg = "voided"
    elif sells and buys:
        # 两腿都有：已完成一轮 T。未配对的敞口不计入已实现盈亏。
        leg = "done"
        if matched_count:
            realized_cny = matched_cny
            if mv > 0:
                realized_bp = round(realized_cny / mv * 10000.0, 1)
    elif sells and not forward:
        leg = "sold"
    elif buys and forward:
        leg = "bought"
    opened_at = (sells or buys or [{}])[0].get("ts") if (sells or buys) else None
    return {
        "exists": bool(records),
        "leg": leg,
        "voided": voided,
        "void_reason": void_reason,
        "mode": meta.get("mode"),
        "th_bp": meta.get("th_bp"),
        "mv": meta.get("mv"),
        "opened_at": opened_at,
        "direction": "forward" if forward else "reverse",
        "sell_count": len(sells),
        "buy_count": len(buys),
        "sell_notional": sell_notional if sells else None,
        "buy_notional": buy_notional if buys else None,
        "fees": fees if (sells or buys) else None,
        "matched_count": matched_count,
        "matched_cny": matched_cny if matched_count else None,
        "unmatched_count": len(unmatched_symbols),
        "unmatched_symbols": sorted(unmatched_symbols),
        "realized_cny": realized_cny,
        "realized_bp": realized_bp,
        "sells": sells,
        "buys": buys,
    }


def allowed_actions(phase: str, summary: Mapping[str, Any]) -> Dict[str, bool]:
    """状态机出口：给定信号阶段与会话状态，判断允许模拟执行哪一腿。

    纪律与信号引擎一致：只在 phase=triggered 允许卖出；只有 phase=closed
    （回归开盘价了结，或收盘强制平仓）才允许买回；提前买回不允许——
    模拟盘的意义就在于检验是否守得住纪律。
    """

    leg = str(summary.get("leg") or "idle")
    voided = bool(summary.get("voided"))
    sell_ok = phase == "triggered" and leg == "idle" and not voided
    buy_ok = phase == "closed" and leg == "sold" and not voided
    return {"sell": sell_ok, "buy": buy_ok}


def validate_execute(action: str, phase: str, summary: Mapping[str, Any]) -> None:
    actions = allowed_actions(phase, summary)
    if action == "sell" and not actions["sell"]:
        raise PaperTActionError(_reject_reason(action, phase, summary))
    if action == "buy" and not actions["buy"]:
        raise PaperTActionError(_reject_reason(action, phase, summary))


def _reject_reason(action: str, phase: str, summary: Mapping[str, Any]) -> str:
    leg = str(summary.get("leg") or "idle")
    if summary.get("voided"):
        return "当日模拟会话已撤销，如需继续请在下一交易日重新模拟"
    if action == "sell":
        if leg != "idle":
            return "当日已有模拟卖出腿，不能重复卖出"
        if phase == "closed":
            return "信号已了结（未在触发后执行卖出），今日模拟不再建仓"
        return f"当前信号阶段为 {phase}，尚未触发卖出条件（聚合曲线需冲高至触发线）"
    if leg != "sold":
        return "尚无模拟卖出腿，无需买回"
    if phase == "triggered":
        return "聚合曲线尚未回归开盘价，信号未给买回指令（保持纪律，等待）"
    return f"当前信号阶段为 {phase}，不允许买回"


def fill_event(row: Mapping[str, Any], ts_iso: str) -> Dict[str, Any]:
    return {
        "event": "fill",
        "leg": str(row["action"]),
        "symbol": str(row["symbol"]),
        "name": str(row.get("name") or row["symbol"]),
        "shares": int(row["shares"]),
        "price": round(float(row["price"]), 4),
        "notional": round(float(row["notional"]), 2),
        "fees": leg_fees(str(row["symbol"]), str(row["action"]), float(row["notional"])),
        "ts": ts_iso,
    }


def close_event(summary: Mapping[str, Any], ts_iso: str) -> Dict[str, Any]:
    return {
        "event": "session_close",
        "sell_notional": summary.get("sell_notional"),
        "buy_notional": summary.get("buy_notional"),
        "fees": summary.get("fees"),
        "realized_cny": summary.get("realized_cny"),
        "realized_bp": summary.get("realized_bp"),
        "completed": True,
        "ts": ts_iso,
    }
