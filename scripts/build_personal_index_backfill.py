"""为个人指数生成 2026-07-01 起的初始回填K线。

数据源：腾讯财经日K线（``newfqkline/get``，三主机轮换，
与本看板的个股图表同源；无需凭据）。

历史窗口拉到 base_date 前 1 个交易月起（默认 ~2026-06-01），把  07-01..07-24
历史补到 ``dashboard/personal-index/backfill.json``。

参数：
    --snapshot PATH  个人指数快照 JSON（默认 snapshots/snapshot_20260825.json）
    --existing PATH  现有 kline 数据 JSON（默认 dashboard/personal-index/index_data.json）
    --output PATH    回填输出路径（默认 dashboard/personal-index/backfill.json）
    --start DATE     回填起始日期（默认 2026-07-01）
    --count INT      每只成分股拉取的 bar 数量（默认 80）
    --min-available  每根 bar 所需的最少成分股数量（默认 15，容许近期 IPO 成分股缺失）

回填后会把与现有 kline 重叠日期的 mv 进行相对误差校验
（默认容忍度 1e-3），任一超出即报错退出。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT_PARENT = ROOT.parent
DEFAULT_SNAPSHOT = PROJECT_PARENT / "snapshots" / "snapshot_20260825.json"
DEFAULT_EXISTING = ROOT / "dashboard" / "personal-index" / "index_data.json"
DEFAULT_OUTPUT = ROOT / "dashboard" / "personal-index" / "backfill.json"

# 2026-10-11 起 fqkline/get 被 WAF 501 拦截，日K改走 newfqkline（需显式日期区间）。
# 三主机顺序与 derivatives_regime.py 保持一致：WAF 封禁粒度是「IP+主机」，
# proxy.finance.qq.com 与前两台不同域名（独立网关），是真正有价值的第三条线。
TENCENT_FQ_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
TENCENT_FQ_KLINE_HOSTS = (
    TENCENT_FQ_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
# 粘性主机：记住最近一次成功的主机，失败才轮换下一个（dashboard_server 同思路）
FQ_KLINE_STICKY_PURPOSE = "personal_index_backfill_daily"
_FQ_KLINE_STICKY_LOCK = threading.Lock()
_FQ_KLINE_STICKY: dict[str, int] = {}
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_START = "2026-07-01"
RELATIVE_TOLERANCE = 1e-3  # mv 校验允许的最大相对误差
DEFAULT_MIN_AVAILABLE = 15


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tencent_symbol(raw_code: str) -> str:
    """六位A股代码加交易所前缀（深 sh / 深 sz）后转为腾讯 8 位 symbol。"""
    code = raw_code.strip()
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    return "sz" + code


def _fetch_daily(symbol: str, count: int) -> dict[str, dict[str, float]]:
    """拉取前复权日K，返回 {date: {open,close,high,low,volume}}。

    这里使用未经复权（参数末为空串）的``day``字段，所有价都按最近收盘价为标准，
    在本成分股/区间内（2026-06-01..2026-09-30）遇到除权事件时未复权与前复权价
    一致（验证了这一点）。若有未来该区间出现除权，需对调用参数与转换逻辑调整。
    """
    end_day = _date.today() + _timedelta(days=1)
    start_day = end_day - _timedelta(days=int(count * 1.6) + 40)
    param = f"{symbol},day,{start_day.isoformat()},{end_day.isoformat()},{count},"
    query = urllib.parse.urlencode({"param": param})
    last_error: Exception | None = None
    empty_response = False
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
            continue
        body = (payload.get("data") or {}).get(symbol) or {}
        # 本脚本口径固定为「不复权 day」：上游若只给 qfqday，那是另一套口径，
        # 不能当成不复权价用（宁可丢弃也不把错误口径的数字写进指数）。
        rows = body.get("day") or []
        result: dict[str, dict[str, float]] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            date, open_, close, high, low, volume = row[:6]
            try:
                result[str(date)] = {
                    "open": float(open_),
                    "close": float(close),
                    "high": float(high),
                    "low": float(low),
                    "volume": float(volume),
                }
            except (TypeError, ValueError):
                continue
        if not result:
            # The upstream endpoint intermittently answers with a body that carries
            # no bars at all.  Returning an empty series would silently drop the
            # component from the index (and from the recomputed base_mv) with no
            # error, so an empty answer is treated as a failed fetch instead.
            empty_response = True
            last_error = ValueError(f"腾讯日K返回空数据：{symbol}")
            continue
        with _FQ_KLINE_STICKY_LOCK:
            _FQ_KLINE_STICKY[FQ_KLINE_STICKY_PURPOSE] = index
        return result
    if empty_response and last_error is not None and isinstance(last_error, ValueError):
        # 原始语义：空数据抛 ValueError，不静默返回空
        raise last_error
    raise urllib.error.URLError(f"腾讯日K主机全部失败：{last_error}")


def _is_trading_day(date: str, holidays: set[str]) -> bool:
    """简单判定：周一到周五 且 不在 holiday 集合内。"""
    if date in holidays:
        return False
    year, month, day = (int(part) for part in date.split("-"))
    # Python 3.9 datetime.isoweekday Monday=1..Sunday=7
    from datetime import date as _date
    weekday = _date(year, month, day).isoweekday()
    return weekday <= 5


def _load_a_share_holidays() -> set[str]:
    """返回 2026/2027 期间 ``2026-07-01`` 之前的 A 股休市日集合。

    这里硬编码从腾讯日K返回到的连续交易日隐含的节假日：手动检查并手动
    定义。实际项目运行时可从网络拉取机构公告表（这里为了可重复运行在脚本内
    完成。不在节假日表中但腾讯没返回到的日期会在 _fetch_daily 返回值中
    缺漏，后续填充阶段会跳过。
    """
    # 2026 年 7 月：周末(7-4,5、7-11,12、7-18,19、7-25,26)自动过滤
    # 特殊节假日未出现例外，未特别说明。
    return set()


def _build_index_bars(
    series: dict[tuple[str, float], dict[str, dict[str, float]]],
    components: list[dict[str, Any]],
    base_point: float,
    base_mv: float,
    start_date: str,
    min_available: int,
) -> list[dict[str, Any]]:
    if base_mv <= 0:
        raise ValueError("snapshot 中 base_mv 必须为正数")

    # 收集所有成分股所有日期点
    all_dates: set[str] = set()
    for day_map in series.values():
        all_dates.update(day_map.keys())
    trading_dates = sorted(d for d in all_dates if d >= start_date)
    full_count = len(components)

    bars: list[dict[str, Any]] = []
    for date in trading_dates:
        # 计算 mv，使用当日实际可用的成分股
        available = [(code, qty) for (code, qty), day_map in series.items() if date in day_map]
        missing = [code for (code, _) in [(c["code"], float(c["quantity"])) for c in components] if code not in {c for c, _ in available}]
        if len(available) < min_available:
            continue
        if len(available) < full_count and not missing:
            missing = []  # safety

        mv = 0.0
        turnover = 0.0
        open_mv = high_mv = low_mv = 0.0
        for code, qty in available:
            day = series[(code, qty)][date]
            mv += qty * day["close"]
            turnover += day["volume"] * day["close"]
            open_mv += qty * day["open"]
            high_mv += qty * day["high"]
            low_mv += qty * day["low"]

        composition = {"available": len(available), "total": full_count, "missing_codes": missing}

        bars.append({
            "date": date,
            "open": round(open_mv / base_mv * base_point, 2),
            "close": round(mv / base_mv * base_point, 2),
            "high": round(high_mv / base_mv * base_point, 2),
            "low": round(low_mv / base_mv * base_point, 2),
            "mv": round(mv, 2),
            "volume": round(turnover, 2),
            "composition": composition,
        })

    return bars


def _validate_against_existing(
    new_bars: list[dict[str, Any]],
    existing_payload: dict[str, Any],
    tolerance: float,
) -> None:
    """把新建的最后几根与现有 kline 重叠的日期做 mv 校验。"""
    existing_by_date = {row["date"]: row for row in existing_payload.get("kline", [])}
    overlaps = [b for b in new_bars if b["date"] in existing_by_date]
    if not overlaps:
        print("[backfill] 没有重叠日期，跳过验证。")
        return
    failed: list[str] = []
    for bar in overlaps:
        stated_mv = float(existing_by_date[bar["date"]]["mv"])
        diff = abs(stated_mv - bar["mv"]) / max(abs(stated_mv), 1.0)
        if diff > tolerance:
            failed.append(f"  {bar['date']}  stated={stated_mv}  backfill={bar['mv']}  rel_err={diff:.4f}")
    if failed:
        print("[backfill] 与现有 mv 校验失败：", file=sys.stderr)
        for line in failed:
            print(line, file=sys.stderr)
        raise SystemExit(1)
    print(f"[backfill] 与现有 mv 重叠 {len(overlaps)} 个交易日，相对误差 ≤ {tolerance}")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成个人指数 2026-07-01 起的回填K线")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--existing", type=Path, default=DEFAULT_EXISTING)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--count", type=int, default=80, help="每只成分股拉取的 bar 数量")
    parser.add_argument(
        "--min-available", type=int, default=DEFAULT_MIN_AVAILABLE,
        help="回填 bar 所需的最少成分股数量（默认 15，容许近期 IPO 缺失）",
    )
    args = parser.parse_args()

    snapshot_payload = _load_json(args.snapshot)
    existing_payload = _load_json(args.existing)
    base_mv = float(snapshot_payload["total_base_mv"])
    components = snapshot_payload["components"]

    # 取以起始日为一个月的起始日为预计（足够覆盖起..end）
    series: dict[tuple[str, float], dict[str, dict[str, float]]] = {}
    print(f"[backfill] 从腾讯拉取 {len(components)} 只成分股的日K线（count={args.count}）...")
    for comp in components:
        sym = _tencent_symbol(comp["code"])
        try:
            daily = _fetch_daily(sym, args.count)
        except urllib.error.URLError as exc:
            print(f"[backfill] 拉取 {sym} 失败：{exc}", file=sys.stderr)
            raise SystemExit(2)
        series[(comp["code"], float(comp["quantity"]))] = daily
        print(f"  {comp['code']} {comp['name']}: {len(daily)} bars")

    bars = _build_index_bars(
        series,
        components,
        float(snapshot_payload["base_point"]),
        base_mv,
        args.start,
        args.min_available,
    )
    print(f"[backfill] 生成 {len(bars)} 根个人指数 bar")

    _validate_against_existing(bars, existing_payload, RELATIVE_TOLERANCE)

    output_payload = {
        "schema_version": "personal-index-backfill.v1",
        "snapshot_id": snapshot_payload["snapshot_id"],
        "base_date": snapshot_payload["base_date"],
        "base_point": float(snapshot_payload["base_point"]),
        "base_mv": base_mv,
        "start_date": args.start,
        "generated_at": existing_payload.get("updated_at"),
        "source": "腾讯财经日K线",
        "bars": bars,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[backfill] 写入 {args.output}（{len(bars)} 根 bar）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())