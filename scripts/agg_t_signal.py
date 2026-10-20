#!/usr/bin/env python3
"""Portfolio-level reverse-T (反T) signal engine.

Signal (calibrated on 2026-09-10 ~ 10-09, see ``scripts/agg_t_backtest.py``):
the portfolio aggregate intraday P&L curve, expressed in basis points of
portfolio market value, is monitored against its opening level.  When, within
the first hour (09:30-10:30), the curve rises above the open by the threshold
(default 22.5 bp), the whole basket is sold (反T first leg); when the curve
falls back to the opening level, the basket is bought back (second leg).  If
the pullback never comes the basket is marked to the close.

``compute_signal`` is a pure replay of that state machine over one day's
aggregate P&L series, so the live endpoint, the unit tests and the replay
harness all exercise exactly the same code path.  Estimates only — not
investment advice.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    from scripts.intraday_pnl_timeseries import CN_TZ  # type: ignore
except ModuleNotFoundError:  # direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from intraday_pnl_timeseries import CN_TZ  # type: ignore[no-redef]

SESSION_START = time(9, 30)
ENTRY_WINDOW_END = time(10, 30)
SESSION_END = time(15, 0)
DEFAULT_TH_BP = 22.5


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _bp(pnl: float, mv: float) -> float:
    return pnl / mv * 10000.0


def compute_signal(
    points: Sequence[Mapping[str, Any]],
    mv: float,
    th_bp: float = DEFAULT_TH_BP,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Replay the 反T state machine over one day's aggregate P&L series.

    ``points``: ``[{"time": iso8601, "pnl": cny}]`` for one trade date.
    ``mv``: portfolio market value used to convert CNY to basis points.
    ``now``: evaluation instant; defaults to the last point (full-day replay).
    """

    clean: List[tuple[datetime, float]] = []
    for point in points or []:
        stamp = _parse(point.get("time"))
        pnl = point.get("pnl")
        if stamp is None or pnl is None:
            continue
        value = float(pnl)
        if not math.isfinite(value):
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

    now_dt = _parse(now) if now is not None else clean[-1][0]
    assert now_dt is not None
    visible = [item for item in clean if item[0] <= now_dt]
    if not visible:
        result["phase"] = "pre_open"
        result["reason"] = "开盘前，等待首个点位"
        return result

    open_dt, open_pnl = visible[0]
    cur_dt, cur_pnl = visible[-1]
    open_bp = _bp(open_pnl, mv)
    cur_bp = _bp(cur_pnl, mv)
    result["open_bp"] = round(open_bp, 1)
    result["current_bp"] = round(cur_bp, 1)

    # 开盘参考可信度：首个点位必须出现在 09:35 前，否则"开盘基准"失真
    #（服务当日启动过晚），当日弃做——宁可错过不可错做。
    if open_dt.astimezone(CN_TZ).time() > time(9, 35):
        result["phase"] = "no_open"
        result["reason"] = f"当日首个点位晚于09:35（{open_dt.astimezone(CN_TZ).strftime('%H:%M:%S')}），缺少可信开盘基准，弃做"
        return result

    entry: Optional[tuple[datetime, float]] = None
    for stamp, pnl in visible[1:]:
        if stamp.time() > ENTRY_WINDOW_END:
            break
        bp = _bp(pnl, mv)
        if bp - open_bp >= th_bp:
            entry = (stamp, bp)
            break

    if entry is None:
        now_clock = now_dt.astimezone(CN_TZ).time()
        if now_clock >= ENTRY_WINDOW_END:
            result["phase"] = "expired"
            result["reason"] = "入场窗口（09:30-10:30）内未触发，今日不做"
        else:
            result["phase"] = "watching"
            result["reason"] = "监控中，冲高至触发线即批量卖出"
        return result

    exit_point: Optional[tuple[datetime, float]] = None
    completed = False
    for stamp, pnl in visible:
        if stamp <= entry[0]:
            continue
        bp = _bp(pnl, mv)
        if bp - open_bp <= 0:
            exit_point = (stamp, bp)
            completed = True
            break
    now_clock = now_dt.astimezone(CN_TZ).time()
    if exit_point is None:
        if now_clock >= SESSION_END:
            # 注意单位统一：visible 存的是 CNY，了结点一律折成 bp。
            last_dt, last_pnl = visible[-1]
            exit_point = (last_dt, _bp(last_pnl, mv))
            result["phase"] = "closed"
            result["reason"] = "未回归开盘价，按收盘平仓"
        else:
            result.update(
                {
                    "phase": "triggered",
                    "reason": "已触发批量卖出，等待回落至开盘价买回",
                    "entry": {"time": entry[0].isoformat(timespec="seconds"), "bp": round(entry[1], 1)},
                }
            )
            return result
    else:
        result["phase"] = "closed"
        result["reason"] = "已回归开盘价买回" if completed else ""

    realized_bp = entry[1] - exit_point[1]
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


def suggested_orders(
    holdings: Sequence[Mapping[str, Any]],
    prices: Mapping[str, float],
    action: str,
    *,
    max_notional: float = 20000.0,
    position_fraction: float = 3.0,
) -> Dict[str, Any]:
    """Example sizing for the basket order list (示例仓位，非投资建议).

    ``holdings``: ``[{"symbol", "name", "quantity"}]``; ``prices`` keyed by
    symbol.  Each order is capped at ``max_notional`` and at
    ``1/position_fraction`` of the holding, rounded down to a whole lot
    (科创板 200 股起).  ``action`` is ``sell`` (反T 第一腿) or ``buy`` (第二腿).
    """

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
        lot = 200 if symbol.startswith("688") else 100
        by_notional = int(max_notional / price) // lot * lot
        by_position = int(quantity / position_fraction) // lot * lot
        shares = max(0, min(by_notional, by_position))
        if shares == 0 and quantity >= lot:
            shares = lot
        shares = min(shares, quantity)
        if shares <= 0:
            continue
        notional = round(shares * price, 2)
        total_notional += notional
        rows.append(
            {
                "symbol": symbol,
                "name": str(holding.get("name") or symbol),
                "action": action,
                "action_label": "卖出" if action == "sell" else "买入",
                "shares": shares,
                "price": price,
                "notional": notional,
            }
        )
    return {
        "rows": rows,
        "total_notional": round(total_notional, 2),
        "count": len(rows),
        "sizing": f"示例仓位：单笔≤{max_notional:.0f}元且≤持仓1/{position_fraction:.0f}，整手取整；参考价为最新价而非成交价",
    }


def replay_ledger(
    data_dir: Path,
    mv: float,
    th_bp: float = DEFAULT_TH_BP,
) -> List[Dict[str, Any]]:
    """Replay harness (模拟盘回放): run the live engine over recorded days."""

    from datetime import date as date_type

    from intraday_pnl_timeseries import IntradayPnlStore

    store = IntradayPnlStore(data_dir)
    results: List[Dict[str, Any]] = []
    for path in sorted(data_dir.glob("intraday-pnl-*.jsonl")):
        trade_date = path.stem.replace("intraday-pnl-", "")
        try:
            stored = store.read_day(date_type.fromisoformat(trade_date))
        except Exception as exc:  # noqa: BLE001 - harness reports and continues
            results.append({"date": trade_date, "phase": "error", "reason": str(exc)[:80]})
            continue
        points_raw = (stored.get("total") or {}).get("points") or []
        # 与实盘接口同口径：只取当日最新持仓绑定（active series_id）的点位，
        # 否则盘中改过配置的日期会混入两种口径的盈亏点。
        active_series = str((stored.get("latest_point") or {}).get("series_id") or "")
        points = [
            {"time": point.get("observed_at"), "pnl": point.get("daily_pnl_cny")}
            for point in points_raw
            if not active_series or str(point.get("series_id") or "") == active_series
        ]
        # 历史日均已收盘：评估时点取当日盘后，确保状态机走到终态。
        now = datetime.fromisoformat(f"{trade_date}T23:59:00+08:00")
        signal = compute_signal(points, mv, th_bp, now=now)
        results.append({"date": trade_date, **signal})
    return results


def main(argv: Sequence[str]) -> int:  # pragma: no cover - manual harness
    data_dir = Path(argv[1]) if len(argv) > 1 else Path("data/intraday-pnl")
    mv = float(argv[2]) if len(argv) > 2 else 1_920_000.0
    th_bp = float(argv[3]) if len(argv) > 3 else DEFAULT_TH_BP
    rows = replay_ledger(data_dir, mv, th_bp)
    wins = sum(1 for row in rows if row.get("realized_bp") is not None and row["realized_bp"] > 0)
    losses = sum(1 for row in rows if row.get("realized_bp") is not None and row["realized_bp"] <= 0)
    print(f"{'日期':<12}{'阶段':<10}{'触发时间':<20}{'了结时间':<20}{' realized':>10}  说明")
    for row in rows:
        entry = (row.get("entry") or {}).get("time", "-") if row.get("entry") else "-"
        exit_ = (row.get("exit") or {}).get("time", "-") if row.get("exit") else "-"
        realized = row.get("realized_bp")
        print(
            f"{row['date']:<12}{row.get('phase', '-'):<10}{str(entry)[11:19]:<20}"
            f"{str(exit_)[11:19]:<20}{realized if realized is not None else '--':>10}  {row.get('reason') or ''}"
        )
    print(f"\n触发 {wins + losses} 天：胜 {wins} / 负 {losses}；窗口 mv={mv:,.0f} th={th_bp}bp")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
