"""按 2026-08-25 固定成分篮子、单一前复权口径重建个人指数全历史K线。

背景（为什么需要重建）：index_data.json 里 2026-01..07 的回填段来自旧项目
的旧持仓篮子（数量基准不同，mv 水平高约 23% 且每日权重不同），且全历史用
不复权价——除权日会出现假亏损；累计盈亏的锚点也来自旧项目（首日之前的
幻影值）。这使得 每日涨跌明细 的 当日盈亏/累计盈亏 与面板声明的口径
（"16只成分股数量固定；前复权价，正确处理分红除权"）不符。

重建规则：
  * 成分与数量：快照文件（默认 snapshots/snapshot_20260825.json），
    与 dashboard_server.PERSONAL_INDEX_HOLDINGS 保持一致。
  * 价格：腾讯财经前复权日K（单一"当前复权基准"，分红除权自动折算进价格，
    日差即含分红的篮子盈亏）。
  * mv = Σ(数量 × 前复权价)；指数点 = 1000 × mv / base_mv，
    base_mv 同口径重算（基准日收盘仍为 1000 点）。
  * pct_change = close/prev_close - 1；pnl_today = mv - prev_mv。
  * pnl_cum 自K线首日起链式累计（首日 = 0），不再沿用旧锚点。
  * 上市晚于区间起点的成分（如 688825）按当日可得数量计入，记录 composition。

注意：若某个成分在重建之后发生除权，腾讯会把其全部历史前复权价整体重算，
文件里的历史 mv 会再次与"当前基准"出现小幅漂移。除权事件后重跑本脚本即可
重新对齐（日常的盘中 bar 由 dashboard_server 实时合并，不受影响）。

运行方式::

    .venv/bin/python scripts/rebuild_personal_index_kline.py [--dry-run]

参数：
    --snapshot  篮子快照（默认 ../snapshots/snapshot_20260825.json）
    --existing  现有 index_data.json（提供起始日期与 benchmarks 等元数据）
    --output    输出路径（默认与 --existing 相同，覆写）
    --start     重建起始日（默认沿用现有K线首日）
    --count     每只成分股拉取的日K根数（默认 400）
    --min-available  每根 bar 所需的最少成分股数量（默认 15）
    --dry-run   只打印摘要，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PARENT = ROOT.parent
DEFAULT_SNAPSHOT = PROJECT_PARENT / "snapshots" / "snapshot_20260825.json"
DEFAULT_EXISTING = ROOT / "dashboard" / "personal-index" / "index_data.json"

# 2026-10-11 起 fqkline/get 在 web./ifzq 两台主机同时被 WAF 501 拦截（端点级），
# 前复权日K改走 newfqkline/get；newfqkline 不接受空起止日期，
# 必须显式传日期区间（见 _fetch_qfq_daily）。
# 三主机顺序与 derivatives_regime.py 保持一致：WAF 封禁粒度是「IP+主机」，
# proxy.finance.qq.com 与前两台不同域名（独立网关），是真正有价值的第三条线。
TENCENT_FQ_KLINE_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
# 粘性主机：记住最近一次成功的主机，下次直接从它开始，失败才轮换下一个
# （与 dashboard_server._TENCENT_KLINE_STICKY 同一思路，避免反复撞被封的主机）。
FQ_KLINE_STICKY_PURPOSE = "personal_index_fq_daily"
_FQ_KLINE_STICKY_LOCK = threading.Lock()
_FQ_KLINE_STICKY: dict[str, int] = {}
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_PAUSE_SECONDS = 0.4
MA_WINDOWS = (5, 10, 20)
DEFAULT_MIN_AVAILABLE = 15
DEFAULT_COUNT = 400

CN_TZ = timezone(timedelta(hours=8))


def _tencent_symbol(code: str) -> str:
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    return "sz" + code


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_qfq_daily(symbol: str, count: int) -> dict[str, dict[str, float]]:
    """拉取前复权日K，返回 {date: {open, close, high, low, volume}}。

    腾讯对个别品种可能只返回未复权 ``day`` 字段（无 ``qfqday``），此时退回
    ``day``——ETF 等无分红除权的品种两者一致。
    """
    end_day = date.today() + timedelta(days=1)
    start_day = end_day - timedelta(days=int(count * 1.6) + 40)
    param = f"{symbol},day,{start_day.isoformat()},{end_day.isoformat()},{int(count)},qfq"
    query = urllib.parse.urlencode({"param": param})
    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    with _FQ_KLINE_STICKY_LOCK:
        start_index = _FQ_KLINE_STICKY.get(FQ_KLINE_STICKY_PURPOSE, 0)
    for offset in range(len(TENCENT_FQ_KLINE_HOSTS)):
        index = (start_index + offset) % len(TENCENT_FQ_KLINE_HOSTS)
        request = urllib.request.Request(
            TENCENT_FQ_KLINE_HOSTS[index] + "?" + query,
            headers={"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
        except Exception as exc:  # WAF 拦截/网络错误 → 换下一主机
            last_error = exc
            payload = None
            continue
        # 粘住这次成功的主机，下次直接复用
        with _FQ_KLINE_STICKY_LOCK:
            _FQ_KLINE_STICKY[FQ_KLINE_STICKY_PURPOSE] = index
        break
    if not isinstance(payload, dict):
        raise RuntimeError(f"Tencent fqkline hosts unavailable: {last_error}")
    body = (payload.get("data") or {}).get(symbol) or {}
    rows = body.get("qfqday") or body.get("day") or []
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            result[str(row[0])] = {
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            }
        except (TypeError, ValueError):
            continue
    if not result:
        # The upstream endpoint intermittently answers with a body that carries
        # no bars at all.  Returning an empty series would silently drop the
        # component from the index (and from the recomputed base_mv) with no
        # error, so an empty answer is treated as a failed fetch instead.
        raise RuntimeError(f"腾讯日K返回空数据：{symbol}")
    return result


def _fetch_all_closes(
    components: list[tuple[str, str, float]], count: int
) -> dict[str, dict[str, dict[str, float]]]:
    series: dict[str, dict[str, dict[str, float]]] = {}
    for index, (code, name, _qty) in enumerate(components):
        symbol = _tencent_symbol(code)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                series[code] = _fetch_qfq_daily(symbol, count)
                last_error = None
                break
            except Exception as exc:  # 网络抖动重试
                last_error = exc
                time.sleep(2.0 * (attempt + 1))
        if last_error is not None:
            raise SystemExit(f"[rebuild] 拉取 {name}({symbol}) 失败：{last_error}")
        if index < len(components) - 1:
            time.sleep(REQUEST_PAUSE_SECONDS)
    return series


def _moving_average(closes: list[float], index: int, window: int) -> float | None:
    if index + 1 < window:
        return None
    tail = closes[index - window + 1 : index + 1]
    return round(sum(tail) / window, 2)


def _rebuild_bars(
    series: dict[str, dict[str, dict[str, float]]],
    components: list[tuple[str, str, float]],
    *,
    start_date: str,
    base_date: str,
    min_available: int,
) -> tuple[list[dict[str, Any]], float]:
    """按固定篮子重建全部 bar（open/close/high/low 先存原始 mv）。

    返回 (bars, base_mv)：base_mv 是基准日在同一口径下的篮子 mv。
    """
    all_dates: set[str] = set()
    for day_map in series.values():
        all_dates.update(day_map.keys())
    trading_dates = sorted(d for d in all_dates if d >= start_date)
    if not trading_dates:
        raise SystemExit("[rebuild] 拉取的数据没有覆盖起始日期之后的交易日")
    total_components = len(components)

    bars: list[dict[str, Any]] = []
    base_mv: float | None = None
    for date in trading_dates:
        available = [
            (code, name, qty)
            for code, name, qty in components
            if date in series.get(code, {})
        ]
        if len(available) < min_available:
            continue
        missing = [
            name for code, name, _qty in components if date not in series.get(code, {})
        ]
        mv = mv_open = mv_high = mv_low = volume = 0.0
        for code, _name, qty in available:
            day = series[code][date]
            mv += qty * day["close"]
            mv_open += qty * day["open"]
            mv_high += qty * day["high"]
            mv_low += qty * day["low"]
            volume += qty * day["volume"]

        if date == base_date:
            base_mv = mv

        prev_bar = bars[-1] if bars else None
        prev_mv = float(prev_bar["mv"]) if prev_bar else None
        pct_change = (
            round((mv / prev_mv - 1) * 100, 2) if prev_mv is not None and prev_mv > 0 else None
        )
        # The basket's membership can change between two bars (a component may
        # start trading later, or stop quoting entirely).  A membership change
        # moves the total market value without being a price move, so it must
        # not be booked as P&L.  The day-over-day P&L is therefore computed on
        # the intersection of both bars' components; the value of a newly
        # added component is treated as a capital contribution, and the value
        # of a dropped component as a capital return.
        pnl_today: float | None = None
        if prev_bar is not None:
            prev_components: dict[str, float] = prev_bar.get("component_mv") or {}
            current_components = {
                code: qty * series[code][date]["close"] for code, _n, qty in available
            }
            shared = set(prev_components) & set(current_components)
            pnl_today = round(
                sum(current_components[code] - prev_components[code] for code in shared),
                2,
            )
        pnl_cum = (
            round(float(prev_bar["pnl_cum"]) + pnl_today, 2)
            if prev_bar is not None and pnl_today is not None
            else 0.0
        )
        bars.append(
            {
                "date": date,
                "open": round(mv_open, 2),
                "close": round(mv, 2),
                "high": round(mv_high, 2),
                "low": round(mv_low, 2),
                "mv": round(mv, 2),
                "volume": round(volume, 2),
                "pct_change": pct_change,
                "pnl_today": pnl_today,
                "pnl_cum": pnl_cum,
                "composition": {
                    "available": len(available),
                    "total": total_components,
                    "missing_codes": missing,
                },
                "component_mv": {
                    code: qty * series[code][date]["close"] for code, _n, qty in available
                },
            }
        )

    if base_mv is None or base_mv <= 0:
        raise SystemExit(
            f"[rebuild] 拉取的数据未覆盖基准日 {base_date}，无法归一指数点"
        )
    return bars, base_mv


def _finalize_points(
    bars: list[dict[str, Any]], base_mv: float
) -> None:
    """把 mv 换算成指数点字段（基准日收盘=1000），MA 基于点序列重算。"""
    scale = 1000.0 / base_mv
    closes: list[float] = []
    for bar in bars:
        bar["open"] = round(float(bar["open"]) * scale, 2)
        bar["close"] = round(float(bar["close"]) * scale, 2)
        bar["high"] = round(float(bar["high"]) * scale, 2)
        bar["low"] = round(float(bar["low"]) * scale, 2)
        closes.append(float(bar["close"]))
    for index, bar in enumerate(bars):
        for window in MA_WINDOWS:
            bar[f"ma{window}"] = _moving_average(closes, index, window)


def main() -> int:
    parser = argparse.ArgumentParser(description="按固定篮子重建个人指数全历史K线")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--existing", type=Path, default=DEFAULT_EXISTING)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--min-available", type=int, default=DEFAULT_MIN_AVAILABLE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = _load_json(args.snapshot)
    components = [
        (str(item["code"]), str(item.get("name") or item["code"]), float(item["quantity"]))
        for item in snapshot.get("components", [])
    ]
    if not components:
        raise SystemExit("[rebuild] 快照中没有 components")
    base_date = str(snapshot.get("base_date") or "")
    if not base_date:
        raise SystemExit("[rebuild] 快照缺少 base_date")

    existing = _load_json(args.existing)
    existing_kline = existing.get("kline", [])
    start_date = args.start or (existing_kline[0]["date"] if existing_kline else None)
    if not start_date:
        raise SystemExit("[rebuild] 无法确定起始日期（现有K线为空且未传 --start）")

    print(f"[rebuild] 成分 {len(components)} 只 | 区间 {start_date} 起 | 基准日 {base_date}")
    series = _fetch_all_closes(components, args.count)

    bars, base_mv = _rebuild_bars(
        series,
        components,
        start_date=start_date,
        base_date=base_date,
        min_available=args.min_available,
    )
    _finalize_points(bars, base_mv)

    old_base_mv = float(existing.get("base_mv") or 0.0)
    print(
        f"[rebuild] 重算基准日({base_date}) mv = {base_mv:.2f}"
        f"（原 base_mv = {old_base_mv:.2f}，偏差 {base_mv - old_base_mv:+.2f}）"
    )
    print(f"[rebuild] 重建 {len(bars)} 根 bar（原 {len(existing_kline)} 根）")
    if bars and existing_kline:
        old_first = existing_kline[0]
        new_first = bars[0]
        print(
            f"[rebuild] 首日对比 {new_first['date']}: close {old_first.get('close')} → {new_first['close']},"
            f" pnl_today {old_first.get('pnl_today')} → {new_first['pnl_today']}"
        )
        old_last = existing_kline[-1]
        new_last = bars[-1]
        print(
            f"[rebuild] 末日对比 {new_last['date']}: close {old_last.get('close')} → {new_last['close']},"
            f" pnl_today {old_last.get('pnl_today')} → {new_last['pnl_today']},"
            f" pnl_cum {old_last.get('pnl_cum')} → {new_last['pnl_cum']}"
        )
    changed = sum(
        1
        for new, old in zip(bars, existing_kline)
        if old.get("pnl_today") is None
        or abs(float(new["pnl_today"] or 0) - float(old.get("pnl_today") or 0)) > 0.5
    )
    print(f"[rebuild] 当日盈亏有变化的 bar：{changed} 根")

    if args.dry_run:
        return 0

    now = datetime.now(CN_TZ)
    new_payload = dict(existing)
    new_payload["base_mv"] = round(base_mv, 2)
    # ``component_mv`` is an internal helper used to attribute day-over-day
    # P&L across membership changes; it is not part of the served payload.
    published_bars = [
        {key: value for key, value in bar.items() if key != "component_mv"}
        for bar in bars
    ]
    new_payload["kline"] = published_bars
    new_payload["update_date"] = bars[-1]["date"] if bars else existing.get("update_date")
    new_payload["total_mv_today"] = bars[-1]["mv"] if bars else existing.get("total_mv_today")
    new_payload["updated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    new_payload["rebuild"] = {
        "applied": True,
        "rebuilt_at": now.isoformat(timespec="seconds"),
        "basis": "前复权（腾讯财经），固定篮子=快照成分",
        "start_date": start_date,
        "bars": len(bars),
        "pnl_cum_anchor": "kline_first_bar",
    }

    output_path = args.output or args.existing
    backup_path = output_path.with_suffix(".pre-rebuild.json")
    if output_path.exists():
        backup_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[rebuild] 原文件已备份到 {backup_path.name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(new_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[rebuild] 已写入 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
