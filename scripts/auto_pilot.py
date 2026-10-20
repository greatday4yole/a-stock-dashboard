#!/usr/bin/env python3
"""自动领航模拟盘（波段级自动决策引擎，独立模块）。

与 ``auto_paper_trading``（日内做 T、多策略并行）互补：本模块维护一个
**完整镜像实盘组合的模拟账户**，每个交易日收盘前的决策窗口内，根据
最新可获取的信息（三大流派信号、统一决策快照中的市场状态与仓位指引、
最新行情）自动决定每只股票的目标仓位并模拟成交，全程留痕于
``data/autopilot/journal.jsonl``，用于和实盘（人工操作）对比。

决策规则（透明、可复核，非投资建议）：

1. 总敞口上限来自市场状态引擎的仓位指引
   （``market_state.guidance.equity_exposure_ceiling_pct``），缺失时按
   状态回退：RISK_OFF 35% / CAUTIOUS 60% / NEUTRAL 80% / RISK_ON 95%；
   UNVERIFIED（数据门禁未通过）当日不执行任何交易。
2. 个股目标权重 = 初始基准权重 × 三流派持仓票数 / 3
   （0 票清仓、1 票 1/3、2 票 2/3、3 票满仓）；流派未覆盖的股票维持现权重。
3. 再平衡阈值：目标市值与现市值偏差 ≥ max(6000 元, 总权益 2%) 才成交，
   整手（100 股）取整；先卖后买，买入受现金约束。
4. 费用口径与 paper_t 一致：佣金万 2.5 最低 5 元、印花税 0.05%（卖出）、
   过户费 0.001%；未含滑点。

依赖通过 :func:`configure` 注入（行情、流派信号、决策快照、组合现金、
时钟、锁），模块不反向依赖 dashboard_server，可独立测试；真实服务在
main() 中 configure + enable，单测环境默认关闭（run_cycle 直接返回）。

只写本地模拟台账，不涉及真实券商账户。估算数据，非投资建议。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import date as date_type
from datetime import datetime, time as daytime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

try:  # 与 dashboard_server 一致的交易日判定（法定节假日）
    from chinese_calendar import is_holiday as _china_is_holiday
except ImportError:  # pragma: no cover - 缺失时退化为仅按周末判断
    _china_is_holiday = None

CN_TZ = timezone(timedelta(hours=8))
SCHEMA = "auto-pilot.v1"

# 决策窗口：收盘前（可在测试或环境中覆盖）
_DECISION_WINDOW = os.environ.get("AUTOPILOT_DECISION_WINDOW", "14:40-14:56")
# 当日净值落盘起点：决策窗口结束至收盘后一段
_NAV_FINAL_START = daytime(14, 40)

FEE_COMMISSION_RATE = 0.00025   # 佣金万 2.5
FEE_COMMISSION_MIN = 5.0        # 最低 5 元/笔
FEE_STAMP_RATE = 0.0005         # 印花税 0.05%（仅卖出）
FEE_TRANSFER_RATE = 0.00001     # 过户费 0.001%

MIN_TRADE_VALUE = 6000.0        # 再平衡最小成交金额
REBALANCE_BAND_PCT = 0.02       # 或总权益的 2%
LOT = 100                       # 整手

REGIME_EXPOSURE_FALLBACK = {
    "RISK_OFF": 35.0,
    "CAUTIOUS": 60.0,
    "NEUTRAL": 80.0,
    "RISK_ON": 95.0,
}
REGIME_LABELS = {
    "RISK_OFF": "风险收缩",
    "CAUTIOUS": "谨慎",
    "NEUTRAL": "中性",
    "RISK_ON": "风险偏好改善",
    "UNVERIFIED": "数据待核验",
}

RULES_TEXT = [
    "总敞口上限取市场状态引擎的仓位指引（缺失时按状态回退：风险收缩35%/谨慎60%/中性80%/风险偏好改善95%）；数据待核验当日不交易。",
    "个股目标权重 = 初始基准权重 × 三大流派持仓票数 ÷ 3（0票清仓、3票满仓）；流派未覆盖的股票维持现权重。",
    "偏差 ≥ max(6000元, 总权益2%) 才再平衡，整手取整，先卖后买、买入受现金约束。",
    "费用：佣金万2.5最低5元、印花税0.05%（卖出）、过户费0.001%；未含滑点。",
    "全部操作写入 data/autopilot/journal.jsonl，可逐笔复核；估算数据，非投资建议。",
]

STATE: Dict[str, Any] = {
    "enabled": False,
    "quotes_fn": None,     # () -> (holdings[{symbol,name,quantity}], prices{symbol:float}, real_mv)
    "school_fn": None,     # () -> /api/school-signals 形态 payload
    "decision_fn": None,   # () -> /api/decision-context 形态 payload
    "portfolio_fn": None,  # () -> {"cash": float, "debt": float}（主组合口径，仅初始化用）
    "now_fn": None,
    "store_root": Path("data/autopilot"),
    "lock": threading.Lock(),
    "log_fn": None,
}


def configure(
    *,
    quotes_fn: Callable[..., Any],
    school_fn: Callable[..., Any],
    decision_fn: Callable[..., Any],
    portfolio_fn: Callable[..., Any],
    now_fn: Callable[[], datetime],
    store_root: Path,
    lock: Optional[threading.Lock] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    STATE.update(
        quotes_fn=quotes_fn,
        school_fn=school_fn,
        decision_fn=decision_fn,
        portfolio_fn=portfolio_fn,
        now_fn=now_fn,
        store_root=Path(store_root),
        lock=lock or threading.Lock(),
        log_fn=log_fn,
    )


def enable() -> None:
    STATE["enabled"] = True


def disable() -> None:
    STATE["enabled"] = False


def _log(message: str) -> None:
    fn = STATE.get("log_fn")
    if callable(fn):
        try:
            fn(f"[auto-pilot] {message}")
        except Exception:
            pass


def _now() -> datetime:
    fn = STATE.get("now_fn")
    now = fn() if callable(fn) else datetime.now(CN_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    return now.astimezone(CN_TZ)


def _decision_window() -> Tuple[daytime, daytime]:
    try:
        start_raw, end_raw = str(_DECISION_WINDOW).split("-", 1)
        sh, sm = (int(x) for x in start_raw.strip().split(":"))
        eh, em = (int(x) for x in end_raw.strip().split(":"))
        return daytime(sh, sm), daytime(eh, em)
    except Exception:
        return daytime(14, 40), daytime(14, 56)


def _is_trade_day(day: date_type) -> bool:
    if day.weekday() >= 5:
        return False
    if _china_is_holiday is not None:
        try:
            if _china_is_holiday(day):
                return False
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _root() -> Path:
    return Path(STATE.get("store_root") or "data/autopilot")


def _state_path() -> Path:
    return _root() / "state.json"


def _journal_path() -> Path:
    return _root() / "journal.jsonl"


def _nav_path() -> Path:
    return _root() / "nav.json"


def _load_state() -> Optional[Dict[str, Any]]:
    path = _state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_state(state: Mapping[str, Any]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state-", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, _state_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_journal(entry: Mapping[str, Any]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    with _journal_path().open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# Journal entries produced by a decision but not yet durable in the account
# state.  They are flushed only after the state file has been committed, so
# the audit trail can never claim a trade the account did not apply.
_pending_journal: List[Dict[str, Any]] = []


def _flush_pending_journal() -> None:
    """Append the journal entries queued by the last decision."""

    if not _pending_journal:
        return
    pending = list(_pending_journal)
    _pending_journal.clear()
    for entry in pending:
        _append_journal(entry)


def _load_journal(limit: int = 500) -> List[Dict[str, Any]]:
    path = _journal_path()
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    out.append(item)
    except Exception:
        return []
    return out[-limit:]


def _load_nav() -> Dict[str, Any]:
    path = _nav_path()
    if not path.is_file():
        return {"schema": SCHEMA, "days": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), list):
            return data
    except Exception:
        pass
    return {"schema": SCHEMA, "days": []}


def _save_nav(nav: Mapping[str, Any]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="nav-", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(nav, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, _nav_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _upsert_nav_day(record: Mapping[str, Any]) -> None:
    nav = _load_nav()
    days = [d for d in nav["days"] if isinstance(d, dict) and d.get("date") != record.get("date")]
    days.append(dict(record))
    days.sort(key=lambda d: str(d.get("date") or ""))
    nav["days"] = days[-400:]
    _save_nav(nav)


# ---------------------------------------------------------------------------
# 费用与账户
# ---------------------------------------------------------------------------


ETF_PREFIXES = ("51", "56", "58", "15", "50")


def is_etf(symbol: str) -> bool:
    """Whether the code is an ETF (no stamp duty, no transfer fee)."""

    return str(symbol or "")[:2] in ETF_PREFIXES


def _fees(side: str, amount: float, symbol: str = "") -> float:
    """Single-leg fee estimate, matching ``paper_t.leg_fees``.

    ETFs pay neither stamp duty nor the transfer fee; stocks pay both (stamp
    duty on sells only).  Keeping the two modules on one fee model avoids the
    simulated account silently diverging from the paper-trading ledger.
    """

    commission = max(FEE_COMMISSION_MIN, FEE_COMMISSION_RATE * amount)
    transfer = 0.0
    stamp = 0.0
    if not is_etf(symbol):
        transfer = FEE_TRANSFER_RATE * amount
        if side == "sell":
            stamp = FEE_STAMP_RATE * amount
    return commission + transfer + stamp


def _invested(state: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    total = 0.0
    for symbol, pos in (state.get("positions") or {}).items():
        price = prices.get(symbol) or float(pos.get("avg_cost") or 0.0)
        total += int(pos.get("shares") or 0) * price
    return total


def _total_value(state: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    return float(state.get("cash") or 0.0) + _invested(state, prices)


# ---------------------------------------------------------------------------
# 初始化：镜像实盘组合
# ---------------------------------------------------------------------------


def _initialize(now: datetime) -> Optional[Dict[str, Any]]:
    quotes_fn = STATE.get("quotes_fn")
    portfolio_fn = STATE.get("portfolio_fn")
    if not callable(quotes_fn) or not callable(portfolio_fn):
        return None
    holdings, prices, real_mv = quotes_fn()
    if not holdings or real_mv <= 0:
        return None
    money = portfolio_fn() or {}
    cash = float(money.get("cash") or 0.0) - float(money.get("debt") or 0.0)
    cash = max(cash, 0.0)
    positions: Dict[str, Any] = {}
    base_weights: Dict[str, float] = {}
    for row in holdings:
        symbol = str(row["symbol"])
        qty = int(row["quantity"])
        price = float(prices.get(symbol) or 0.0)
        if qty <= 0 or price <= 0:
            continue
        positions[symbol] = {
            "name": str(row.get("name") or symbol),
            "shares": qty,
            "avg_cost": round(price, 4),
        }
    invested = sum(
        int(p["shares"]) * float(prices.get(s) or p["avg_cost"])
        for s, p in positions.items()
    )
    total = cash + invested
    if total <= 0:
        return None
    for symbol, pos in positions.items():
        value = int(pos["shares"]) * float(prices.get(symbol) or pos["avg_cost"])
        base_weights[symbol] = value / total
    state: Dict[str, Any] = {
        "schema": SCHEMA,
        "started_at": now.isoformat(timespec="seconds"),
        "base_date": now.date().isoformat(),
        "base_value": round(total, 2),
        "base_real_mv": round(real_mv, 2),
        "cash": round(cash, 2),
        "positions": positions,
        "base_weights": {k: round(v, 6) for k, v in base_weights.items()},
        "names": {s: p["name"] for s, p in positions.items()},
        "realized_pnl": 0.0,
        "total_fees": 0.0,
        "trade_count": 0,
        "last_decision_date": "",
        "last_error": "",
    }
    _save_state(state)
    _append_journal(
        {
            "ts": now.isoformat(timespec="seconds"),
            "type": "init",
            "note": "模拟账户初始化：镜像实盘持仓与净现金（现金−负债），自此独立决策",
            "base_value": state["base_value"],
            "cash": state["cash"],
            "base_real_mv": state["base_real_mv"],
            "positions": {s: dict(p) for s, p in positions.items()},
        }
    )
    return state


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------


def _market_state_view(decision_payload: Mapping[str, Any]) -> Dict[str, Any]:
    market_state = decision_payload.get("market_state")
    if not isinstance(market_state, Mapping):
        return {"regime": "UNVERIFIED", "score": None, "ceiling_pct": None, "label": "数据待核验"}
    regime = str(market_state.get("regime") or "UNVERIFIED")
    guidance = market_state.get("guidance") if isinstance(market_state.get("guidance"), Mapping) else {}
    ceiling = guidance.get("equity_exposure_ceiling_pct")
    try:
        ceiling_pct = float(ceiling) if ceiling is not None else None
    except (TypeError, ValueError):
        ceiling_pct = None
    if ceiling_pct is None:
        ceiling_pct = REGIME_EXPOSURE_FALLBACK.get(regime)
    return {
        "regime": regime,
        "label": str(market_state.get("label") or REGIME_LABELS.get(regime, regime)),
        "score": market_state.get("score"),
        "confidence": market_state.get("confidence"),
        "ceiling_pct": ceiling_pct,
        "guidance_message": str(guidance.get("message") or ""),
    }


def _school_votes(school_payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for stock in school_payload.get("stocks") or []:
        if not isinstance(stock, Mapping):
            continue
        symbol = str(stock.get("symbol") or "")
        if not symbol:
            continue
        out[symbol] = {
            "votes": stock.get("holding_votes"),
            "overall": stock.get("overall"),
            "date": stock.get("date"),
        }
    return out


def _run_decisions(
    state: Dict[str, Any],
    now: datetime,
    holdings: List[Dict[str, Any]],
    prices: Mapping[str, float],
    real_mv: float,
) -> Dict[str, Any]:
    """执行一次每日决策；返回决策摘要（同时写 journal）。"""

    today = now.date().isoformat()
    errors: List[str] = []

    decision_fn = STATE.get("decision_fn")
    school_fn = STATE.get("school_fn")
    decision_payload: Mapping[str, Any] = {}
    school_payload: Mapping[str, Any] = {}
    if callable(decision_fn):
        try:
            decision_payload = decision_fn() or {}
        except Exception as exc:
            errors.append(f"决策快照获取失败：{type(exc).__name__}: {exc}")
    if callable(school_fn):
        try:
            school_payload = school_fn() or {}
        except Exception as exc:
            errors.append(f"流派信号获取失败：{type(exc).__name__}: {exc}")

    market = _market_state_view(decision_payload)
    votes_map = _school_votes(school_payload)
    regime = market["regime"]

    cash = float(state.get("cash") or 0.0)
    invested = _invested(state, prices)
    equity = cash + invested
    if equity <= 0:
        state["last_error"] = "模拟账户权益非正，跳过决策"
        return {"date": today, "acted": False, "reason": state["last_error"]}

    plans: List[Dict[str, Any]] = []
    trades: List[Dict[str, Any]] = []
    journal_entries: List[Dict[str, Any]] = []
    blocked = regime == "UNVERIFIED" or market.get("ceiling_pct") is None

    if blocked:
        _pending_journal.append(
            {
                "ts": now.isoformat(timespec="seconds"),
                "type": "decision",
                "date": today,
                "regime": regime,
                "regime_label": market["label"],
                "action": "skip_all",
                "reason": "市场状态数据门禁未通过（UNVERIFIED），当日不执行任何交易",
                "errors": errors,
            }
        )
        state["last_decision_date"] = today
        return {"date": today, "acted": False, "reason": "UNVERIFIED"}

    ceiling_pct = float(market["ceiling_pct"])
    base_weights: Dict[str, float] = dict(state.get("base_weights") or {})
    names: Dict[str, str] = dict(state.get("names") or {})

    # 新出现的实盘持仓（初始化后人工新买）并入基准权重，按当前市值占比
    for row in holdings:
        symbol = str(row["symbol"])
        if symbol not in base_weights and symbol in prices:
            value = int(row["quantity"]) * prices[symbol]
            if value > 0:
                base_weights[symbol] = value / equity
                names[symbol] = str(row.get("name") or symbol)
                state.setdefault("positions", {})[symbol] = {
                    "name": names[symbol],
                    "shares": 0,
                    "avg_cost": 0.0,
                }

    # 目标权重
    raw_targets: Dict[str, float] = {}
    # 流派未覆盖的标的按规则"维持现权重"，它不是敞口上限要压缩的对象：
    # 上限只应作用在"票数决定的目标权重"上。若一并缩放，本该维持的持仓
    # 会被按比例清仓，违反第 2 条规则。
    vote_driven: Dict[str, float] = {}
    for symbol, base_w in base_weights.items():
        if symbol not in prices:
            continue
        current_value = int((state["positions"].get(symbol) or {}).get("shares") or 0) * prices[symbol]
        info = votes_map.get(symbol)
        if info is None or info.get("votes") is None:
            target_w = current_value / equity  # 流派未覆盖：维持
            vote_note = "流派未覆盖，维持"
        else:
            votes = max(0, min(3, int(info["votes"])))
            target_w = float(base_w) * votes / 3.0
            vote_note = f"流派持仓票数 {votes}/3"
            vote_driven[symbol] = target_w
        raw_targets[symbol] = target_w
        plans.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, symbol),
                "votes": (info or {}).get("votes"),
                "overall": (info or {}).get("overall"),
                "note": vote_note,
                "current_weight": round(current_value / equity, 4),
                "target_weight_raw": round(target_w, 4),
            }
        )

    # 敞口上限只压缩票数驱动的目标部分；维持现权重的部分原样保留。
    vote_sum = sum(vote_driven.values())
    maintain_sum = sum(
        weight for symbol, weight in raw_targets.items() if symbol not in vote_driven
    )
    scale = 1.0
    if vote_sum > 0 and vote_sum + maintain_sum > ceiling_pct / 100.0:
        # 先压票数部分，压到底仍超限的部分再由维持部分让渡。
        allowed_vote_sum = max(0.0, ceiling_pct / 100.0 - maintain_sum)
        scale = min(1.0, allowed_vote_sum / vote_sum)

    def _scaled_target(symbol: str) -> float:
        weight = raw_targets[symbol]
        if symbol in vote_driven:
            return weight * scale
        return weight

    band = max(MIN_TRADE_VALUE, REBALANCE_BAND_PCT * equity)
    positions: Dict[str, Any] = state["positions"]

    # 先卖后买
    for side in ("sell", "buy"):
        for symbol, target_w in raw_targets.items():
            price = float(prices.get(symbol) or 0.0)
            if price <= 0:
                continue
            pos = positions.get(symbol) or {"name": names.get(symbol, symbol), "shares": 0, "avg_cost": 0.0}
            current_shares = int(pos.get("shares") or 0)
            current_value = current_shares * price
            target_value = _scaled_target(symbol) * equity
            delta = target_value - current_value
            if side == "sell" and delta >= -band:
                continue
            if side == "buy" and delta <= band:
                continue

            if side == "sell":
                if target_value <= band:
                    shares = current_shares  # 目标≈0 → 清仓
                else:
                    shares = int((-delta) / price / LOT) * LOT
                shares = min(shares, current_shares)
                if shares <= 0:
                    continue
                amount = shares * price
                fee = _fees("sell", amount, symbol)
                cash += amount - fee
                avg_cost = float(pos.get("avg_cost") or 0.0)
                # avg_cost already carries the buy-side commission (amortized
                # into the per-share cost on entry), so only the sell fee is
                # deducted here.
                realized = (price - avg_cost) * shares - fee
                state["realized_pnl"] = round(float(state.get("realized_pnl") or 0.0) + realized, 2)
                pos["shares"] = current_shares - shares
                trades.append(
                    {
                        "symbol": symbol,
                        "name": names.get(symbol, symbol),
                        "side": "sell",
                        "shares": shares,
                        "price": round(price, 4),
                        "amount": round(amount, 2),
                        "fee": round(fee, 2),
                        "realized_pnl": round(realized, 2),
                    }
                )
            else:
                affordable = cash - FEE_COMMISSION_MIN
                shares = int(min(delta, affordable) / price / LOT) * LOT
                if shares <= 0:
                    continue
                amount = shares * price
                fee = _fees("buy", amount, symbol)
                while shares > 0 and amount + fee > cash:
                    shares -= LOT
                    amount = shares * price
                    fee = _fees("buy", amount, symbol)
                if shares <= 0:
                    continue
                cash -= amount + fee
                avg_cost = float(pos.get("avg_cost") or 0.0)
                new_shares = current_shares + shares
                # The buy-side commission is amortized into the per-share cost
                # so that a later sale nets *both* legs' fees against the
                # realized P&L.  Without it, the commission paid on entry is
                # silently missing from realized_pnl.
                pos["avg_cost"] = round(
                    (avg_cost * current_shares + amount + fee) / new_shares, 4
                )
                pos["shares"] = new_shares
                trades.append(
                    {
                        "symbol": symbol,
                        "name": names.get(symbol, symbol),
                        "side": "buy",
                        "shares": shares,
                        "price": round(price, 4),
                        "amount": round(amount, 2),
                        "fee": round(fee, 2),
                    }
                )
            positions[symbol] = pos
            state["total_fees"] = round(float(state.get("total_fees") or 0.0) + trades[-1]["fee"], 2)
            state["trade_count"] = int(state.get("trade_count") or 0) + 1

    for trade in trades:
        journal_entries.append(
            {
                "ts": now.isoformat(timespec="seconds"),
                "type": "trade",
                "date": today,
                **trade,
                "reason": f"{trade['name']} 目标权重偏离超阈值，按决策窗口价模拟{'卖出' if trade['side']=='sell' else '买入'}",
            }
        )

    state["cash"] = round(cash, 2)
    state["base_weights"] = {k: round(v, 6) for k, v in base_weights.items()}
    state["names"] = names
    state["last_decision_date"] = today
    state["last_error"] = "; ".join(errors)[:500]

    invested_after = _invested(state, prices)
    equity_after = state["cash"] + invested_after
    journal_entries.append(
        {
            "ts": now.isoformat(timespec="seconds"),
            "type": "decision",
            "date": today,
            "regime": regime,
            "regime_label": market["label"],
            "score": market.get("score"),
            "confidence": market.get("confidence"),
            "ceiling_pct": ceiling_pct,
            "guidance": market.get("guidance_message") or "",
            "scale": round(scale, 4),
            "plans": plans,
            "trades": len(trades),
            "exposure_after_pct": round(invested_after / equity_after * 100, 1) if equity_after > 0 else None,
            "errors": errors,
        }
    )
    # The journal is only written after the account state has been committed
    # by the caller.  Writing it first would leave trades in the audit trail
    # that the account never applied, and the next cycle would repeat them.
    _pending_journal.extend(journal_entries)
    return {"date": today, "acted": bool(trades), "trades": len(trades), "regime": regime}


# ---------------------------------------------------------------------------
# 净值
# ---------------------------------------------------------------------------


def _record_nav(
    state: Mapping[str, Any], now: datetime, prices: Mapping[str, float], real_mv: float
) -> Dict[str, Any]:
    cash = float(state.get("cash") or 0.0)
    invested = _invested(state, prices)
    value = cash + invested
    base_value = float(state.get("base_value") or value)
    base_real_mv = float(state.get("base_real_mv") or real_mv)
    record = {
        "date": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
        "value": round(value, 2),
        "cash": round(cash, 2),
        "invested": round(invested, 2),
        "exposure_pct": round(invested / value * 100, 1) if value > 0 else None,
        "nav": round(value / base_value * 1000, 2) if base_value > 0 else None,
        "return_pct": round((value / base_value - 1) * 100, 2) if base_value > 0 else None,
        "real_mv": round(real_mv, 2),
        "real_return_pct": round((real_mv / base_real_mv - 1) * 100, 2) if base_real_mv > 0 else None,
    }
    _upsert_nav_day(record)
    return record


# ---------------------------------------------------------------------------
# 主循环入口（后台调度器调用）
# ---------------------------------------------------------------------------


def run_cycle(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """每个行情周期调用一次；内部按交易日与决策窗口幂等执行。"""

    if not STATE.get("enabled"):
        return {"ok": False, "reason": "disabled"}
    quotes_fn = STATE.get("quotes_fn")
    if not callable(quotes_fn):
        return {"ok": False, "reason": "not-configured"}
    now = now or _now()
    trade_day = _is_trade_day(now.date())

    lock: threading.Lock = STATE.get("lock") or threading.Lock()
    with lock:
        try:
            holdings, prices, real_mv = quotes_fn()
        except Exception as exc:
            _log(f"行情获取失败：{type(exc).__name__}: {exc}")
            return {"ok": False, "reason": f"quotes: {type(exc).__name__}"}
        if not prices:
            return {"ok": False, "reason": "no-prices"}

        state = _load_state()
        if state is None:
            # 初始化不限制交易日：非交易日用最近收盘镜像建仓，首个交易日即开始对比
            try:
                state = _initialize(now)
            except Exception as exc:
                _log(f"初始化失败：{type(exc).__name__}: {exc}")
                return {"ok": False, "reason": f"init: {type(exc).__name__}"}
            if state is None:
                return {"ok": False, "reason": "init-unavailable"}
        if not trade_day:
            return {"ok": True, "trade_day": False}

        result: Dict[str, Any] = {"ok": True, "trade_day": True}
        # 交易时段内持续刷新当日净值记录
        if daytime(9, 30) <= now.time() <= daytime(15, 30):
            try:
                result["nav"] = _record_nav(state, now, prices, real_mv)
            except Exception as exc:
                _log(f"净值记录失败：{type(exc).__name__}: {exc}")

        start, end = _decision_window()
        if start <= now.time() <= end and state.get("last_decision_date") != now.date().isoformat():
            try:
                result["decision"] = _run_decisions(state, now, holdings, prices, real_mv)
                _save_state(state)
                # The account state is durable now, so the audit trail may
                # safely record the trades it applied.
                _flush_pending_journal()
            except Exception as exc:
                _log(f"决策执行失败：{type(exc).__name__}: {exc}")
                result["decision_error"] = f"{type(exc).__name__}: {exc}"
        return result


# ---------------------------------------------------------------------------
# 对外 payload
# ---------------------------------------------------------------------------


def _positions_view(
    state: Mapping[str, Any],
    prices: Mapping[str, float],
    holdings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    real_qty = {str(h["symbol"]): int(h["quantity"]) for h in holdings}
    cash = float(state.get("cash") or 0.0)
    equity = cash + _invested(state, prices)
    rows: List[Dict[str, Any]] = []
    names = state.get("names") or {}
    symbols = set((state.get("positions") or {}).keys()) | set(names.keys())
    for symbol in sorted(symbols):
        pos = (state.get("positions") or {}).get(symbol) or {}
        shares = int(pos.get("shares") or 0)
        price = prices.get(symbol)
        value = shares * price if price else None
        avg_cost = float(pos.get("avg_cost") or 0.0)
        pnl_pct = None
        if price and avg_cost > 0 and shares > 0:
            pnl_pct = round((price / avg_cost - 1) * 100, 2)
        rows.append(
            {
                "symbol": symbol,
                "name": str(names.get(symbol) or pos.get("name") or symbol),
                "shares": shares,
                "real_shares": real_qty.get(symbol, 0),
                "price": price,
                "value": round(value, 2) if value is not None else None,
                "weight_pct": round(value / equity * 100, 2) if value and equity > 0 else 0.0,
                "base_weight_pct": round(float((state.get("base_weights") or {}).get(symbol) or 0.0) * 100, 2),
                "avg_cost": avg_cost or None,
                "pnl_pct": pnl_pct,
            }
        )
    rows.sort(key=lambda r: -(r.get("value") or 0.0))
    return rows


def payload() -> Dict[str, Any]:
    """/api/autopilot 主接口：账户现状 + 净值历史 + 与实盘对照 + 规则说明。"""

    now = _now()
    state = _load_state()
    base: Dict[str, Any] = {
        "ok": True,
        "schema": SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "enabled": bool(STATE.get("enabled")),
        "decision_window": str(_DECISION_WINDOW),
        "rules": RULES_TEXT,
        "trade_day": _is_trade_day(now.date()),
    }
    if state is None:
        # 读取路径惰性初始化：服务重启后首个页面请求即尝试镜像建仓，
        # 成功后由后台调度接管后续决策与净值记录。
        if STATE.get("enabled") and callable(STATE.get("quotes_fn")) and callable(STATE.get("portfolio_fn")):
            lock: threading.Lock = STATE.get("lock") or threading.Lock()
            with lock:
                state = _load_state()
                if state is None:
                    try:
                        state = _initialize(now)
                    except Exception as exc:
                        _log(f"惰性初始化失败：{type(exc).__name__}: {exc}")
    if state is None:
        base.update(
            {
                "initialized": False,
                "message": "模拟账户尚未初始化（将在下个行情周期自动镜像实盘组合）",
            }
        )
        return base

    prices: Dict[str, float] = {}
    holdings: List[Dict[str, Any]] = []
    real_mv = 0.0
    quote_error = ""
    quotes_fn = STATE.get("quotes_fn")
    if callable(quotes_fn):
        try:
            holdings, prices, real_mv = quotes_fn()
        except Exception as exc:
            quote_error = f"{type(exc).__name__}: {exc}"

    cash = float(state.get("cash") or 0.0)
    invested = _invested(state, prices) if prices else None
    value = cash + invested if invested is not None else None
    base_value = float(state.get("base_value") or 0.0)
    base_real_mv = float(state.get("base_real_mv") or 0.0)

    nav = _load_nav()
    journal = _load_journal(limit=400)
    recent = list(reversed(journal[-60:]))
    last_decision = next(
        (j for j in reversed(journal) if j.get("type") == "decision"), None
    )

    base.update(
        {
            "initialized": True,
            "quote_error": quote_error,
            "account": {
                "cash": round(cash, 2),
                "invested": round(invested, 2) if invested is not None else None,
                "total_value": round(value, 2) if value is not None else None,
                "exposure_pct": round(invested / value * 100, 1) if value else None,
                "realized_pnl": state.get("realized_pnl"),
                "total_fees": state.get("total_fees"),
                "trade_count": state.get("trade_count"),
            },
            "base": {
                "date": state.get("base_date"),
                "started_at": state.get("started_at"),
                "base_value": base_value,
                "base_real_mv": base_real_mv,
            },
            "performance": {
                "return_pct": round((value / base_value - 1) * 100, 2)
                if value is not None and base_value > 0
                else None,
                "real_return_pct": round((real_mv / base_real_mv - 1) * 100, 2)
                if real_mv > 0 and base_real_mv > 0
                else None,
                "real_mv": round(real_mv, 2) if real_mv > 0 else None,
                "excess_pct": (
                    round((value / base_value - 1) * 100, 2)
                    - round((real_mv / base_real_mv - 1) * 100, 2)
                )
                if value is not None and base_value > 0 and real_mv > 0 and base_real_mv > 0
                else None,
            },
            "positions": _positions_view(state, prices, holdings) if prices else [],
            "nav_days": nav.get("days") or [],
            "last_decision": last_decision,
            "recent_journal": recent,
            "journal_count": len(journal),
            "last_error": state.get("last_error") or "",
        }
    )
    return base


def journal_payload(limit: int = 200) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    entries = _load_journal(limit=limit)
    return {
        "ok": True,
        "schema": SCHEMA,
        "generated_at": _now().isoformat(timespec="seconds"),
        "count": len(entries),
        "entries": list(reversed(entries)),
    }
