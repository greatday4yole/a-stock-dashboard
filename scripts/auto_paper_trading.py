#!/usr/bin/env python3
"""自动模拟炒股引擎（独立模块，多策略并行）。

对同一份持仓行情，多个策略各自在**独立模拟账户**上按信号自动买卖
（无需人工下单），运行一段时间后在「策略对比」面板比较优劣：

- ``agg-reverse``        组合反T·自适应阈值（回测校准，原单策略升级而来）
- ``agg-reverse-fixed``  组合反T·固定 22.5bp（检验自适应阈值是否有效）
- ``stock-reverse``      个股反T·自适应（每只股票独立触发，1/3 仓位示例）
- ``stock-forward``      个股正T·自适应（先低吸后高抛，与反T互为镜像）
- 持有基准（不做T）      对比面板中的参照列，来自盘中账本当日实际盈亏

依赖通过 :func:`configure` 注入（行情账本、持仓行情、时钟、锁），
使本模块不反向依赖 dashboard_server，可独立测试；真实服务在 main()
中 configure + enable，单测环境默认关闭。

所有成交按最新行情价模拟（未含滑点），费用口径见 ``paper_t.FEE_NOTE``；
只写本地模拟台账（``data/paper-trading/<strategy_id>/``），不涉及真实
券商账户。估算数据，非投资建议。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from scripts import agg_t_signal as agg_t_signal_module
    from scripts import paper_t as paper_t_module
    from scripts.paper_t import _parse_iso
    from scripts.paper_t_store import PaperTIntegrityError, PaperTStore
except ModuleNotFoundError:  # direct execution
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    import agg_t_signal as agg_t_signal_module  # type: ignore[no-redef]
    import paper_t as paper_t_module  # type: ignore[no-redef]
    from paper_t import _parse_iso  # type: ignore[no-redef]
    from paper_t_store import PaperTIntegrityError, PaperTStore  # type: ignore[no-redef]

SESSION_END = time(15, 0)
CN_TZ = timezone(timedelta(hours=8))
DEFAULT_TH_BP = agg_t_signal_module.DEFAULT_TH_BP
ENTRY_MAX_DELAY_SECONDS = paper_t_module.AUTO_MAX_DELAY_SECONDS
STOCK_SIZING_NOTE = "个股策略仓位：单笔≤2万元且≤持仓1/3，整手取整（示例规则，非投资建议）"

_SCHEMA = "auto-paper.api.v1"
_COMPARE_SCHEMA = "auto-paper-compare.api.v1"
_COMPARE_WINDOW_DAYS = 20
_THRESHOLD_HISTORY_DAYS = 15
_THRESHOLD_MIN_DAYS = 5


# ---------------------------------------------------------------------------
# 策略注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    kind: str  # "agg" 组合级 | "stock" 个股级
    direction: str  # "reverse" 反T（先卖后买）| "forward" 正T（先买后卖）
    th_mode: str  # "adaptive" | "fixed"
    th_fixed_bp: float = 22.5


STRATEGIES: Tuple[Strategy, ...] = (
    Strategy("agg-reverse", "组合反T·自适应", "agg", "reverse", "adaptive"),
    Strategy("agg-reverse-fixed", "组合反T·固定22.5bp", "agg", "reverse", "fixed"),
    Strategy("stock-reverse", "个股反T·自适应", "stock", "reverse", "adaptive"),
    Strategy("stock-forward", "个股正T·自适应", "stock", "forward", "adaptive"),
)

STRATEGY_BY_ID: Dict[str, Strategy] = {s.id: s for s in STRATEGIES}


# ---------------------------------------------------------------------------
# 依赖注入与开关
# ---------------------------------------------------------------------------

STATE: Dict[str, Any] = {
    "enabled": False,
    "intraday_store": None,
    "quotes_holdings_fn": None,
    "now_fn": None,
    "store_root": Path("data/paper-trading"),
    "lock": threading.Lock(),
    "log_fn": None,
}


def configure(
    *,
    intraday_store: Any,
    quotes_holdings_fn: Callable[[], Tuple[List[Dict[str, Any]], Dict[str, float], float]],
    now_fn: Callable[[], datetime],
    store_root: Optional[Path] = None,
    lock: Optional[threading.Lock] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """注入依赖（真实服务在 main() 中调用；测试注入假件）。"""

    STATE["intraday_store"] = intraday_store
    STATE["quotes_holdings_fn"] = quotes_holdings_fn
    STATE["now_fn"] = now_fn
    if store_root is not None:
        STATE["store_root"] = Path(store_root)
    if lock is not None:
        STATE["lock"] = lock
    STATE["log_fn"] = log_fn


def enable(enabled: bool = True) -> None:
    STATE["enabled"] = enabled


def disable() -> None:
    enable(False)


def _default_now() -> datetime:
    """Timezone-aware clock (CN_TZ).

    Every timestamp this module writes is CN_TZ-aware, so the fallback clock
    must be aware too: comparing a naive ``datetime.now()`` with an aware
    entry timestamp raises ``TypeError`` deep inside the signal engine.
    """

    return datetime.now(CN_TZ)


def _resolve_now(now: Optional[datetime] = None) -> datetime:
    """Normalize any clock source to an aware CN_TZ datetime."""

    now_fn = STATE.get("now_fn") or _default_now
    resolved = now if now is not None else now_fn()
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=CN_TZ)
    return resolved.astimezone(CN_TZ)


def _log(message: str) -> None:
    log_fn = STATE.get("log_fn")
    prefix = ""
    now_fn = STATE.get("now_fn")
    if callable(now_fn):
        try:
            prefix = f"[{now_fn().strftime('%H:%M:%S')}] "
        except Exception:
            prefix = ""
    line = f"{prefix}[auto-paper] {message}"
    if callable(log_fn):
        log_fn(line)
    else:
        print(line)


def _store_for(strategy_id: str) -> PaperTStore:
    return PaperTStore(Path(STATE["store_root"]) / strategy_id)


# ---------------------------------------------------------------------------
# 信号引擎：反T 用现成的 agg_t_signal.compute_signal；正T 在此实现镜像版。
# ---------------------------------------------------------------------------


def compute_forward_signal(
    points: Sequence[Mapping[str, Any]],
    mv: float,
    th_bp: float,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """正T（先低吸后高抛）状态机：与 compute_signal 互为镜像。

    开盘后首小时内，聚合曲线跌破开盘价 th_bp 以上 → 先买入（triggered）；
    回归开盘价 → 卖出了结（completed=True）；否则 15:00 按收盘卖出。
    realized = exit_bp - entry_bp（低买高卖为正）。
    """

    clean: List[Tuple[datetime, float]] = []
    for point in points or []:
        stamp = _parse_iso(point.get("time"))
        pnl = point.get("pnl")
        if stamp is None or pnl is None:
            continue
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        clean.append((stamp, value))
    clean.sort(key=lambda item: item[0])

    result: Dict[str, Any] = {
        "ok": True,
        "th_bp": round(th_bp, 1),
        "open_bp": None,
        "current_bp": None,
        "points": len(clean),
        "entry": None,
        "exit": None,
        "completed": None,
        "realized_bp": None,
    }
    if mv <= 0 or not clean:
        result["phase"] = "no_data"
        result["reason"] = "暂无聚合盈亏点位（休市或当日尚未记录）"
        return result

    now_dt = _parse_iso(now) if now is not None else clean[-1][0]
    if now_dt is None:
        now_dt = clean[-1][0]
    visible = [item for item in clean if item[0] <= now_dt]
    if not visible:
        result["phase"] = "pre_open"
        result["reason"] = "开盘前，等待首个点位"
        return result

    open_dt, open_pnl = visible[0]
    cur_dt, cur_pnl = visible[-1]
    open_bp = open_pnl / mv * 10000.0
    cur_bp = cur_pnl / mv * 10000.0
    result["open_bp"] = round(open_bp, 1)
    result["current_bp"] = round(cur_bp, 1)

    if open_dt.time() > time(9, 35):
        result["phase"] = "no_open"
        result["reason"] = f"当日首个点位晚于09:35（{open_dt.strftime('%H:%M:%S')}），缺少可信开盘基准，弃做"
        return result

    entry: Optional[Tuple[datetime, float]] = None
    for stamp, pnl in visible[1:]:
        if stamp.time() > agg_t_signal_module.ENTRY_WINDOW_END:
            break
        bp = pnl / mv * 10000.0
        if open_bp - bp >= th_bp:
            entry = (stamp, bp)
            break

    if entry is None:
        if now_dt.time() >= agg_t_signal_module.ENTRY_WINDOW_END:
            result["phase"] = "expired"
            result["reason"] = "入场窗口（09:30-10:30）内未触发，今日不做"
        else:
            result["phase"] = "watching"
            result["reason"] = "监控中，跌破触发线即买入"
        return result

    exit_point: Optional[Tuple[datetime, float]] = None
    completed = False
    for stamp, pnl in visible:
        if stamp <= entry[0]:
            continue
        bp = pnl / mv * 10000.0
        if bp - open_bp >= 0:
            exit_point = (stamp, bp)
            completed = True
            break
    if exit_point is None:
        if now_dt.time() >= SESSION_END:
            last_dt, last_pnl = visible[-1]
            exit_point = (last_dt, last_pnl / mv * 10000.0)
            result["phase"] = "closed"
            result["reason"] = "未回归开盘价，按收盘卖出回补"
        else:
            result.update(
                {
                    "phase": "triggered",
                    "reason": "已触发买入，等待回归开盘价卖出",
                    "entry": {"time": entry[0].isoformat(timespec="seconds"), "bp": round(entry[1], 1)},
                }
            )
            return result
    else:
        result["phase"] = "closed"
        result["reason"] = "已回归开盘价卖出" if completed else ""

    realized_bp = exit_point[1] - entry[1]
    result.update(
        {
            "entry": {"time": entry[0].isoformat(timespec="seconds"), "bp": round(entry[1], 1)},
            "exit": {"time": exit_point[0].isoformat(timespec="seconds"), "bp": round(exit_point[1], 1)},
            "completed": completed,
            "realized_bp": round(realized_bp, 1),
            "est_cny": round(realized_bp / 10000.0 * mv, 0),
        }
    )
    return result


# ---------------------------------------------------------------------------
# 自适应阈值（与回测校准同式）：0.75 × clamp(0.6×近15日振幅中位, [4,30]bp)
# ---------------------------------------------------------------------------

_AMP_CACHE: Dict[Tuple[str, str, str], Tuple[float, bool]] = {}
_AMP_CACHE_LOCK = threading.Lock()


def _active_series_id(stored: Mapping[str, Any]) -> str:
    return str(((stored.get("latest_point") or {}) if isinstance(stored, dict) else {}).get("series_id") or "")


def _series_points(stored: Mapping[str, Any], symbol: Optional[str]) -> List[Tuple[str, float]]:
    active = _active_series_id(stored)
    raw: Sequence[Mapping[str, Any]]
    if symbol is None:
        raw = ((stored.get("total") or {}).get("points") or []) if isinstance(stored, dict) else []
    else:
        rows = (stored.get("positions") or []) if isinstance(stored, dict) else []
        raw = []
        for row in rows or []:
            if isinstance(row, dict) and str(row.get("thscode") or "") == symbol:
                raw = row.get("points") or []
                break
    points: List[Tuple[str, float]] = []
    for point in raw or []:
        if not isinstance(point, dict):
            continue
        pnl = point.get("daily_pnl_cny")
        stamp = point.get("observed_at")
        if pnl is None or not stamp:
            continue
        if active and str(point.get("series_id") or "") != active:
            continue
        try:
            points.append((str(stamp), float(pnl)))
        except (TypeError, ValueError):
            continue
    return points


def _day_amplitude_bp(
    trade_date: date_type,
    *,
    symbol: Optional[str],
    mv_ref: float,
) -> Optional[Tuple[float, bool]]:
    """(当日振幅bp, 开盘参考是否可信)。

    扫描窗口从昨日回溯（当日不参与），而历史日账本 append-only 不可变，
    因此按 (symbol, date) 先查缓存再读文件——否则每次决策都要全量重读
    校验所有历史账本（单文件 0.1-0.4s），决策周期会被拖到 10 秒级。
    """

    store = STATE.get("intraday_store")
    if store is None or mv_ref <= 0:
        return None
    key = (symbol or "", trade_date.isoformat())
    with _AMP_CACHE_LOCK:
        cached = _AMP_CACHE.get(key)
    if cached is not None:
        return cached[0] / mv_ref * 10000.0, cached[1]
    try:
        stored = store.read_day(trade_date)
    except Exception:
        return None
    if not isinstance(stored, dict):
        return None
    points = _series_points(stored, symbol)
    if len(points) < 2:
        return None
    try:
        first_dt = _parse_iso(points[0][0])
    except Exception:
        return None
    if first_dt is None:
        return None
    open_ok = first_dt.time() <= time(9, 35)
    values = [pnl for _, pnl in points]
    amplitude = max(values) - min(values)
    with _AMP_CACHE_LOCK:
        if len(_AMP_CACHE) > 8192:
            _AMP_CACHE.clear()
        _AMP_CACHE[key] = (amplitude, open_ok)
    return amplitude / mv_ref * 10000.0, open_ok


def adaptive_threshold_bp(*, symbol: Optional[str] = None, mv_ref: float) -> float:
    """自适应阈值；有效历史不足 _THRESHOLD_MIN_DAYS 时回退默认值。"""

    if mv_ref <= 0:
        return DEFAULT_TH_BP
    now_fn = STATE.get("now_fn") or _default_now
    today = now_fn().date()
    amplitudes: List[float] = []
    for offset in range(1, 40):
        rec = _day_amplitude_bp(today - timedelta(days=offset), symbol=symbol, mv_ref=mv_ref)
        if rec and rec[1]:
            amplitudes.append(rec[0])
        if len(amplitudes) >= _THRESHOLD_HISTORY_DAYS:
            break
    if len(amplitudes) < _THRESHOLD_MIN_DAYS:
        return DEFAULT_TH_BP
    base = min(30.0, max(4.0, 0.6 * sorted(amplitudes)[len(amplitudes) // 2]))
    return round(0.75 * base, 1)


def threshold_for(strategy: Strategy, *, symbol: Optional[str] = None, mv_ref: float) -> float:
    if strategy.th_mode == "fixed":
        return strategy.th_fixed_bp
    return adaptive_threshold_bp(symbol=symbol, mv_ref=mv_ref)


# ---------------------------------------------------------------------------
# 今日数据快照（总曲线 + 逐股曲线）
# ---------------------------------------------------------------------------


def _today_series(trade_date: date_type) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    store = STATE.get("intraday_store")
    if store is None:
        return [], {}
    try:
        stored = store.read_day(trade_date)
    except Exception:
        return [], {}
    total_points = [
        {"time": stamp, "pnl": pnl}
        for stamp, pnl in _series_points(stored, None)
    ]
    symbol_points: Dict[str, List[Dict[str, Any]]] = {}
    rows = (stored.get("positions") or []) if isinstance(stored, dict) else []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or "")
        if not code:
            continue
        points = [
            {"time": stamp, "pnl": pnl}
            for stamp, pnl in _series_points(stored, code)
        ]
        if points:
            symbol_points[code] = points
    return total_points, symbol_points


# ---------------------------------------------------------------------------
# 执行驱动：每个策略独立模拟账户；信号翻转即自动成交，离线错过不追价。
# ---------------------------------------------------------------------------


def _symbol_leg_map(summary: Mapping[str, Any], *, direction: str = "reverse") -> Dict[str, str]:
    """Per-symbol session state used by the state machine.

    反T：第一腿是卖出（"sold"），第二腿买回后为 "done"。
    正T：第一腿是买入（"bought"），第二腿卖出后为 "done"。
    """

    forward = str(direction) == "forward"
    legs: Dict[str, str] = {}
    if forward:
        for row in summary.get("buys") or []:
            legs[str(row.get("symbol"))] = "bought"
        for row in summary.get("sells") or []:
            legs[str(row.get("symbol"))] = "done"
        return legs
    for row in summary.get("sells") or []:
        legs[str(row.get("symbol"))] = "sold"
    for row in summary.get("buys") or []:
        legs[str(row.get("symbol"))] = "done"
    return legs


def _execute_agg_leg(
    store: PaperTStore,
    records: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    signal: Mapping[str, Any],
    holdings: List[Dict[str, Any]],
    prices: Dict[str, float],
    mv: float,
    now: datetime,
) -> Optional[Dict[str, Any]]:
    trade_date = now.date()
    ts_iso = now.isoformat(timespec="seconds")
    phase = str(signal.get("phase") or "")
    action, _reason = paper_t_module.auto_decision(phase, summary, signal, now)
    if action == "skip":
        return None
    if action == "sell":
        plan = paper_t_module.plan_orders(holdings, prices, "sell", "full")
        valid = [h for h in holdings if int(h.get("quantity") or 0) > 0]
        if not plan["rows"] or len(plan["rows"]) < len(valid):
            _log("行情覆盖不足，本轮推迟自动卖出")
            return None
        if not records:
            store.append(
                trade_date,
                {
                    "event": "session_open",
                    "mode": "full",
                    "th_bp": signal.get("th_bp"),
                    "mv": round(mv, 0),
                    "executor": "auto",
                },
                now,
            )
        for row in plan["rows"]:
            store.append(trade_date, paper_t_module.fill_event(row, ts_iso), now)
        return {
            "action": "sell",
            "note": f"已自动卖出 {plan['count']} 笔 ≈ {plan['total_notional']:,.0f} 元（全篮子），等待买回信号",
        }
    plan = paper_t_module.buy_rows_from_fills(summary.get("sells") or [], prices)
    if not plan["rows"]:
        return None
    for row in plan["rows"]:
        store.append(trade_date, paper_t_module.fill_event(row, ts_iso), now)
    updated = paper_t_module.session_summary(store.read_day(trade_date))
    store.append(trade_date, paper_t_module.close_event(updated, ts_iso), now)
    realized = updated.get("realized_cny")
    realized_bp = updated.get("realized_bp")
    realized_text = f"{realized:+,.0f} 元" if isinstance(realized, (int, float)) else "--"
    bp_text = f"（{realized_bp:+.1f}bp，估算含费用）" if isinstance(realized_bp, (int, float)) else ""
    return {
        "action": "buy",
        "note": f"已自动买回 {plan['count']} 笔 ≈ {plan['total_notional']:,.0f} 元；模拟T盈亏 {realized_text}{bp_text}",
    }


def _execute_stock_legs(
    store: PaperTStore,
    records: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    signals: Mapping[str, Mapping[str, Any]],
    holdings: List[Dict[str, Any]],
    prices: Dict[str, float],
    mv: float,
    now: datetime,
    *,
    direction: str = "reverse",
) -> Optional[Dict[str, Any]]:
    trade_date = now.date()
    ts_iso = now.isoformat(timespec="seconds")
    forward = str(direction) == "forward"
    legs = _symbol_leg_map(summary, direction=direction)
    # 反T 的第二腿买回量来自卖出腿；正T 的第二腿卖出量来自买入腿。
    first_leg_rows = list(summary.get("buys") or []) if forward else list(summary.get("sells") or [])
    first_leg_by_symbol = {str(row.get("symbol")): row for row in first_leg_rows}
    sold_count = 0
    bought_count = 0
    acted: List[str] = []
    for holding in holdings:
        symbol = str(holding.get("symbol") or "")
        signal = signals.get(symbol)
        if not signal:
            continue
        leg = legs.get(symbol, "idle")
        action, _reason = paper_t_module.auto_decision(
            str(signal.get("phase") or ""),
            {"leg": leg, "voided": False},
            signal,
            now,
            direction=direction,
        )
        if action == "skip":
            continue
        if action == "sell":
            if forward:
                # 正T第二腿：卖出此前低吸买入的数量。
                first_fill = first_leg_by_symbol.get(symbol)
                if first_fill is None:
                    continue
                plan = paper_t_module.sell_rows_from_fills([first_fill], prices)
            else:
                plan = paper_t_module.plan_orders([holding], prices, "sell", "sample")
            if not plan["rows"]:
                continue
            if not records:
                store.append(
                    trade_date,
                    {
                        "event": "session_open",
                        "mode": "sample",
                        "th_bp": signal.get("th_bp"),
                        "mv": round(mv, 0),
                        "executor": "auto",
                    },
                    now,
                )
                records = store.read_day(trade_date)
            for row in plan["rows"]:
                store.append(trade_date, paper_t_module.fill_event(row, ts_iso), now)
            sold_count += 1
            acted.append(symbol)
        elif action == "buy":
            if forward:
                # 正T第一腿：低吸买入。
                plan = paper_t_module.plan_orders([holding], prices, "buy", "sample")
            else:
                sell_fill = first_leg_by_symbol.get(symbol)
                if sell_fill is None:
                    continue
                plan = paper_t_module.buy_rows_from_fills([sell_fill], prices)
            if not plan["rows"]:
                continue
            if not records and action == "buy" and forward:
                store.append(
                    trade_date,
                    {
                        "event": "session_open",
                        "mode": "sample",
                        "th_bp": signal.get("th_bp"),
                        "mv": round(mv, 0),
                        "executor": "auto",
                    },
                    now,
                )
                records = store.read_day(trade_date)
            for row in plan["rows"]:
                store.append(trade_date, paper_t_module.fill_event(row, ts_iso), now)
            bought_count += 1
            acted.append(symbol)
    if not acted:
        return None
    if forward:
        if bought_count:
            return {
                "action": "buy",
                "note": f"已自动低吸买入 {bought_count} 只（{'、'.join(acted[:4])}{'等' if len(acted) > 4 else ''}），等待回归开盘价高抛",
            }
        return {
            "action": "sell",
            "note": f"已自动高抛卖出 {sold_count} 只（{'、'.join(acted[:4])}{'等' if len(acted) > 4 else ''}）",
        }
    if sold_count:
        return {
            "action": "sell",
            "note": f"已自动卖出 {sold_count} 只（{'、'.join(acted[:4])}{'等' if len(acted) > 4 else ''}），示例仓位，等待买回信号",
        }
    return {
        "action": "buy",
        "note": f"已自动买回 {bought_count} 只（{'、'.join(acted[:4])}{'等' if len(acted) > 4 else ''}）",
    }


def run_all_strategies(
    *, now: Optional[datetime] = None, quotes_payload: Optional[Mapping[str, Any]] = None
) -> List[Dict[str, Any]]:
    """对全部策略各执行一步自动决策；返回当日实际发生的动作摘要列表。"""

    if not STATE.get("enabled"):
        return []
    now = _resolve_now(now)
    trade_date = now.date()
    quotes_fn = STATE.get("quotes_holdings_fn")
    if not callable(quotes_fn):
        return []
    results: List[Dict[str, Any]] = []
    try:
        holdings, prices, mv = quotes_fn()
    except Exception as exc:
        _log(f"持仓行情获取失败：{type(exc).__name__}: {exc}")
        return results
    total_points, symbol_points = _today_series(trade_date)
    lock: threading.Lock = STATE.get("lock") or threading.Lock()
    with lock:
        for strategy in STRATEGIES:
            store = _store_for(strategy.id)
            try:
                records = store.read_day(trade_date)
            except PaperTIntegrityError as exc:
                _log(f"[{strategy.id}] 台账完整性校验失败，冻结当日自动执行：{exc}")
                continue
            summary = paper_t_module.session_summary(records, direction=strategy.direction)
            if summary.get("voided"):
                continue
            if strategy.kind == "agg":
                if strategy.direction != "reverse":
                    continue
                th = threshold_for(strategy, mv_ref=mv)
                signal = agg_t_signal_module.compute_signal(total_points, mv, th, now=now)
                action = _execute_agg_leg(store, records, summary, signal, holdings, prices, mv, now)
            else:
                signals: Dict[str, Dict[str, Any]] = {}
                for holding in holdings:
                    symbol = str(holding.get("symbol") or "")
                    quantity = int(holding.get("quantity") or 0)
                    price = float(prices.get(symbol) or 0.0)
                    points = symbol_points.get(symbol) or []
                    symbol_mv = quantity * price
                    if quantity <= 0 or symbol_mv <= 0 or len(points) < 2:
                        continue
                    th = threshold_for(strategy, symbol=symbol, mv_ref=symbol_mv)
                    if strategy.direction == "forward":
                        sig = compute_forward_signal(points, symbol_mv, th, now=now)
                    else:
                        sig = agg_t_signal_module.compute_signal(points, symbol_mv, th, now=now)
                    if str(sig.get("phase") or ""):
                        signals[symbol] = sig
                if not signals:
                    continue
                action = _execute_stock_legs(
                    store,
                    records,
                    summary,
                    signals,
                    holdings,
                    prices,
                    mv,
                    now,
                    direction=strategy.direction,
                )
            if action:
                action["strategy"] = strategy.id
                action["strategy_name"] = strategy.name
                results.append(action)
                _log(f"[{strategy.name}] {action['note']}")
    return results


# ---------------------------------------------------------------------------
# 页面载荷：今日各策略状态 + 跨策略对比
# ---------------------------------------------------------------------------


def _compact_stock_signal(signals: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    triggered = sorted(s for s, sig in signals.items() if str(sig.get("phase")) == "triggered")
    watching = sum(1 for sig in signals.values() if str(sig.get("phase")) == "watching")
    closed = sorted(s for s, sig in signals.items() if str(sig.get("phase")) == "closed")
    return {
        "kind": "stock",
        "triggered": triggered,
        "closed": closed,
        "watching": watching,
        "scanned": len(signals),
    }


def today_payload(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = _resolve_now(now)
    try:
        run_all_strategies(now=now)
    except Exception as exc:
        _log(f"自动执行失败：{type(exc).__name__}: {exc}")
    trade_date = now.date()
    total_points, symbol_points = _today_series(trade_date)
    try:
        holdings, prices, mv = STATE["quotes_holdings_fn"]()
    except Exception:
        holdings, prices, mv = [], {}, 0.0
    entries: List[Dict[str, Any]] = []
    for strategy in STRATEGIES:
        store = _store_for(strategy.id)
        try:
            records = store.read_day(trade_date)
        except PaperTIntegrityError as exc:
            entries.append(
                {
                    "id": strategy.id,
                    "name": strategy.name,
                    "session": {"leg": "error"},
                    "signal": {},
                    "note": f"台账完整性校验失败：{exc}",
                }
            )
            continue
        summary = paper_t_module.session_summary(records, direction=strategy.direction)
        if strategy.kind == "agg":
            th = threshold_for(strategy, mv_ref=mv)
            signal = agg_t_signal_module.compute_signal(total_points, mv, th, now=now)
            compact = {key: signal.get(key) for key in ("phase", "reason", "open_bp", "current_bp", "th_bp", "entry", "exit", "completed", "realized_bp")}
        else:
            signals: Dict[str, Dict[str, Any]] = {}
            for holding in holdings:
                symbol = str(holding.get("symbol") or "")
                quantity = int(holding.get("quantity") or 0)
                price = float(prices.get(symbol) or 0.0)
                points = symbol_points.get(symbol) or []
                symbol_mv = quantity * price
                if quantity <= 0 or symbol_mv <= 0 or len(points) < 2:
                    continue
                th = threshold_for(strategy, symbol=symbol, mv_ref=symbol_mv)
                if strategy.direction == "forward":
                    sig = compute_forward_signal(points, symbol_mv, th, now=now)
                else:
                    sig = agg_t_signal_module.compute_signal(points, symbol_mv, th, now=now)
                if str(sig.get("phase") or ""):
                    signals[symbol] = sig
            compact = _compact_stock_signal(signals)
        note = ""
        leg = str(summary.get("leg") or "idle")
        if leg == "sold":
            if strategy.kind == "stock":
                note = (
                    f"已卖出 {summary.get('sell_count', 0)} 只，"
                    f"其中 {summary.get('matched_count', 0)} 只已买回"
                    f"（已实现 {paper_t_fmt(summary.get('matched_cny'))}）"
                )
            else:
                note = f"已自动卖出 {summary.get('sell_count', 0)} 笔 ≈ {summary.get('sell_notional', 0):,.0f} 元"
        elif leg == "bought":
            note = (
                f"已低吸买入 {summary.get('buy_count', 0)} 只，等待回归开盘价高抛"
            )
        elif leg == "done":
            realized = summary.get("realized_cny")
            realized_bp = summary.get("realized_bp")
            note = (
                f"模拟T完成：{paper_t_fmt(realized)}"
                + (f"（{realized_bp:+.1f}bp）" if isinstance(realized_bp, (int, float)) else "")
            )
        entries.append(
            {
                "id": strategy.id,
                "name": strategy.name,
                "session": summary,
                "signal": compact,
                "note": note,
            }
        )
    return {
        "schema_version": _SCHEMA,
        "ok": True,
        "trade_date": trade_date.isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "enabled": bool(STATE.get("enabled")),
        "strategies": entries,
        "fee_note": paper_t_module.FEE_NOTE,
        "sizing_note": STOCK_SIZING_NOTE,
        "disclaimer": "本地模拟盘：各策略在独立模拟账户按信号自动买卖，无需人工下单，不涉及真实资金与券商账户；估算数据，非投资建议。",
    }


def paper_t_fmt(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{amount:+,.0f} 元"


def void_session(strategy_id: str, reason: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    strategy = STRATEGY_BY_ID.get(str(strategy_id or ""))
    if strategy is None:
        raise ValueError(f"未知策略：{strategy_id}")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("撤销需填写原因")
    now = _resolve_now(now)
    trade_date = now.date()
    store = _store_for(strategy.id)
    with STATE.get("lock") or threading.Lock():
        records = store.read_day(trade_date)
        summary = paper_t_module.session_summary(
            records, direction=strategy.direction
        )
        if not records:
            raise ValueError("该策略当日没有模拟会话可撤销")
        if summary.get("voided"):
            raise ValueError("该策略当日模拟会话已撤销过")
        if summary.get("leg") == "done":
            raise ValueError("该策略当日模拟会话已完成，不可撤销")
        store.append(trade_date, {"event": "void", "reason": reason[:200]}, now)
    _log(f"[{strategy.name}] 已撤销当日模拟会话：{reason[:60]}")
    return today_payload(now=now)


def _hold_baseline_by_date(dates: Sequence[str]) -> Dict[str, Optional[float]]:
    store = STATE.get("intraday_store")
    out: Dict[str, Optional[float]] = {}
    for date_text in dates:
        value: Optional[float] = None
        if store is not None:
            try:
                stored = store.read_day(date_type.fromisoformat(date_text))
                points = _series_points(stored, None)
                if points:
                    value = points[-1][1]
            except Exception:
                value = None
        out[date_text] = value
    return out


def compare_payload(*, window_days: int = _COMPARE_WINDOW_DAYS, now: Optional[datetime] = None) -> Dict[str, Any]:
    """跨策略对比：日期 × 策略 的已实现盈亏矩阵 + 持有基准 + 各自合计。"""

    now = _resolve_now(now)
    date_set: set = set()
    strategy_rows: List[Dict[str, Any]] = []
    for strategy in STRATEGIES:
        store = _store_for(strategy.id)
        by_date: Dict[str, Dict[str, Any]] = {}
        try:
            all_dates = store.list_dates()
        except Exception:
            all_dates = []
        for date_text in all_dates:
            date_set.add(date_text)
            try:
                records = store.read_day(date_type.fromisoformat(date_text))
            except (ValueError, PaperTIntegrityError) as exc:
                by_date[date_text] = {"status": "台账异常", "error": str(exc)[:120]}
                continue
            summary = paper_t_module.session_summary(
                records, direction=strategy.direction
            )
            realized = summary.get("realized_cny")
            realized_bp = summary.get("realized_bp")
            if realized is None and summary.get("matched_cny") is not None:
                realized = summary["matched_cny"]
            mv = summary.get("mv")
            if realized is not None and realized_bp is None and isinstance(mv, (int, float)) and mv > 0:
                realized_bp = round(realized / mv * 10000.0, 1)
            by_date[date_text] = {
                "status": summary.get("leg"),
                "realized_cny": realized,
                "realized_bp": realized_bp,
                "sell_count": summary.get("sell_count"),
                "buy_count": summary.get("buy_count"),
                "voided": bool(summary.get("voided")),
            }
        executed = [row for row in by_date.values() if isinstance(row.get("realized_cny"), (int, float))]
        bps = [row["realized_bp"] for row in executed if isinstance(row.get("realized_bp"), (int, float))]
        totals = {
            "sessions": len(executed),
            "wins": sum(1 for row in executed if row["realized_cny"] > 0),
            "losses": sum(1 for row in executed if row["realized_cny"] <= 0),
            "total_cny": round(sum(row["realized_cny"] for row in executed), 2),
            "avg_bp": round(sum(bps) / len(bps), 1) if bps else None,
            "total_bp": round(sum(bps), 1) if bps else None,
        }
        strategy_rows.append(
            {
                "id": strategy.id,
                "name": strategy.name,
                "kind": strategy.kind,
                "direction": strategy.direction,
                "th_mode": strategy.th_mode,
                "by_date": by_date,
                "totals": totals,
            }
        )
    dates = sorted(date_set, reverse=True)[: max(1, int(window_days))]
    dates = sorted(dates)  # 展示按时间正序，最新在底部
    hold = _hold_baseline_by_date(dates)
    hold_values = [value for value in hold.values() if isinstance(value, (int, float))]
    return {
        "schema_version": _COMPARE_SCHEMA,
        "ok": True,
        "window_days": int(window_days),
        "dates": dates,
        "strategies": strategy_rows,
        "hold_baseline": {
            "label": "持有不动（基准）",
            "by_date": hold,
            "total": round(sum(hold_values), 2) if hold_values else None,
        },
        "fee_note": paper_t_module.FEE_NOTE,
        "sizing_note": STOCK_SIZING_NOTE,
        "generated_at": now.isoformat(timespec="seconds"),
        "disclaimer": "各策略在独立模拟账户自动运行；bp 为占组合市值的比例；费用估算、未含滑点；估算数据，非投资建议。",
    }
