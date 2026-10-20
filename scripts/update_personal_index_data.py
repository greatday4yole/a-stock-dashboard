"""合并 ``dashboard/personal-index/backfill.json``(历史回填)与现有
``index_data.json``(最新快照),产出单一可供前端读取的 kline 文件。

职责：
  * 把 backfill 早于现有 kline 的部分拼接在前面,补齐 07-01..07-24 等早期 bar。
  * 从 mv 链条重新推导 ``pct_change`` / ``pnl_today``(填补现有 kline 在首根
    bar 上的空值)。
  * 从累计盈亏锚点(现有 kline 中首次出现的 ``pnl_cum``)反推早期 bar 的
    ``pnl_cum``,保持与原口径一致。
  * 按完整 merge 后的 close 序列重算 MA5/MA10/MA20(原 07-27.. 的 MA 不变)。
  * 保留现有 kline 中的 ``volume`` 字段(不覆盖,避免与原脚本冲突)。
  * 把现有 kline 中缺失的 ``composition`` 字段补全为 16/16(full basis)。

运行方式::

    .venv/bin/python scripts/update_personal_index_data.py

参数：
    --existing  现有 kline JSON 路径（默认 dashboard/personal-index/index_data.json）
    --backfill  回填 JSON 路径（默认 dashboard/personal-index/backfill.json）
    --output    输出 JSON 路径（默认与 --existing 相同，覆写）
    --dry-run   仅打印变更摘要,不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXISTING = ROOT / "dashboard" / "personal-index" / "index_data.json"
DEFAULT_BACKFILL = ROOT / "dashboard" / "personal-index" / "backfill.json"

MA_WINDOWS = (5, 10, 20)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _round2(value: float) -> float:
    return round(value, 2)


def _build_full_composition(existing_payload: dict[str, Any]) -> dict[str, Any]:
    """基于现有快照中已知成分股推算 16/16 composition。"""
    if "components" in existing_payload and isinstance(existing_payload["components"], list):
        return {
            "available": len(existing_payload["components"]),
            "total": len(existing_payload["components"]),
            "missing_codes": [],
        }
    return {"available": 16, "total": 16, "missing_codes": []}


def _moving_average(closes: list[float], index: int, window: int) -> float | None:
    if index + 1 < window:
        return None
    slice_ = closes[index - window + 1: index + 1]
    if any(value is None for value in slice_):
        return None
    return round(sum(slice_) / window, 2)


def _merge_and_recompute(
    backfill_bars: list[dict[str, Any]],
    existing_kline: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary = {"added_backfill_rows": 0, "filled_pct_change": 0, "filled_pnl_today": 0,
               "filled_pnl_cum": 0, "filled_ma_values": 0, "filled_composition": 0}
    full_composition = _build_full_composition({"components": [{}] * 16})

    # 1) 拼接(以日期去重并排序)
    by_date: dict[str, dict[str, Any]] = {}
    for row in backfill_bars:
        date = row["date"]
        by_date[date] = {
            "date": date,
            "open": row["open"],
            "close": row["close"],
            "high": row["high"],
            "low": row["low"],
            "mv": row["mv"],
            "volume": row.get("volume"),
            "pct_change": None,
            "pnl_today": None,
            "pnl_cum": None,
            "ma5": None, "ma10": None, "ma20": None,
            "composition": row.get("composition"),
        }
    for row in existing_kline:
        date = row["date"]
        existing = by_date.get(date)
        if existing is not None:
            # 现有数据胜出,保留所有字段(验证与回填的 mv 偏差)
            existing["open"] = row.get("open", existing["open"])
            existing["close"] = row.get("close", existing["close"])
            existing["high"] = row.get("high", existing["high"])
            existing["low"] = row.get("low", existing["low"])
            existing["mv"] = row.get("mv", existing["mv"])
            if row.get("volume") is not None:
                existing["volume"] = row["volume"]
            existing["pct_change"] = row.get("pct_change")
            existing["pnl_today"] = row.get("pnl_today")
            existing["pnl_cum"] = row.get("pnl_cum")
            existing["ma5"] = row.get("ma5")
            existing["ma10"] = row.get("ma10")
            existing["ma20"] = row.get("ma20")
            # 现有数据本身以 16/16 为基准
            existing["composition"] = {"available": 16, "total": 16, "missing_codes": []}
            summary["filled_composition"] += 1
        else:
            by_date[date] = {
                "date": date,
                "open": row.get("open"),
                "close": row.get("close"),
                "high": row.get("high"),
                "low": row.get("low"),
                "mv": row.get("mv"),
                "volume": row.get("volume"),
                "pct_change": row.get("pct_change"),
                "pnl_today": row.get("pnl_today"),
                "pnl_cum": row.get("pnl_cum"),
                "ma5": row.get("ma5"),
                "ma10": row.get("ma10"),
                "ma20": row.get("ma20"),
                "composition": {"available": 16, "total": 16, "missing_codes": []},
            }

    sorted_dates = sorted(by_date.keys())
    first_existing = existing_kline[0]["date"] if existing_kline else None
    summary["added_backfill_rows"] = sum(
        1 for date in sorted_dates if first_existing is not None and date < first_existing
    )

    # 2) 推算 pct_change / pnl_today(从 mv 链)
    rows = [by_date[date] for date in sorted_dates]
    for index, row in enumerate(rows):
        if index == 0:
            row["pct_change"] = None
            row["pnl_today"] = None
            continue
        prev_row = rows[index - 1]
        if isinstance(row["mv"], (int, float)) and isinstance(prev_row["mv"], (int, float)):
            pnl_today = round(row["mv"] - prev_row["mv"], 2)
            row["pnl_today"] = pnl_today
            summary["filled_pnl_today"] += 1 if row["pct_change"] is None else 0
        if (row["pct_change"] is None
                and isinstance(row["close"], (int, float))
                and isinstance(prev_row["close"], (int, float))
                and prev_row["close"] > 0):
            pct_change = round((row["close"] / prev_row["close"] - 1) * 100, 2)
            row["pct_change"] = pct_change
            summary["filled_pct_change"] += 1

    # 3) pnl_cum 反推: 找到现有 kline 中首次出现的 pnl_cum 锚点
    anchor_index = None
    for index, row in enumerate(rows):
        if row["pnl_cum"] is not None:
            anchor_index = index
            break
    if anchor_index is None:
        print("[merge] 没有 pnl_cum 锚点,无法反推累计盈亏。", file=sys.stderr)
        return rows, summary

    # 向后推算
    for index in range(anchor_index + 1, len(rows)):
        if rows[index]["pnl_cum"] is not None and rows[index]["pnl_cum"] != 0:
            continue
        if isinstance(rows[index]["pnl_today"], (int, float)):
            rows[index]["pnl_cum"] = round(
                (rows[index - 1]["pnl_cum"] or 0) + rows[index]["pnl_today"], 2
            )
            summary["filled_pnl_cum"] += 1
    # 向前推算
    for index in range(anchor_index - 1, -1, -1):
        if isinstance(rows[index]["pnl_today"], (int, float)) and isinstance(rows[index + 1]["pnl_cum"], (int, float)):
            rows[index]["pnl_cum"] = round(
                rows[index + 1]["pnl_cum"] - rows[index]["pnl_today"], 2
            )
            summary["filled_pnl_cum"] += 1

    # 4) MA 重算
    closes = [row["close"] for row in rows]
    for window in MA_WINDOWS:
        for index in range(len(rows)):
            ma_value = _moving_average(closes, index, window)
            previous = rows[index][f"ma{window}"]
            if ma_value is not None and previous is None:
                summary["filled_ma_values"] += 1
            if ma_value is not None:
                rows[index][f"ma{window}"] = ma_value

    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="合并个人指数回填与现有数据")
    parser.add_argument("--existing", type=Path, default=DEFAULT_EXISTING)
    parser.add_argument("--backfill", type=Path, default=DEFAULT_BACKFILL)
    parser.add_argument("--output", type=Path, default=None,
                        help="输出路径（默认与 --existing 一致）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印变更摘要")
    args = parser.parse_args()

    output_path = args.output or args.existing
    existing_payload = _load_json(args.existing)
    backfill_payload = _load_json(args.backfill)

    merged_rows, summary = _merge_and_recompute(
        backfill_payload.get("bars", []),
        existing_payload.get("kline", []),
    )

    new_payload = dict(existing_payload)
    new_payload["kline"] = merged_rows
    new_payload["backfill"] = {
        "applied": True,
        "source": backfill_payload.get("source"),
        "start_date": backfill_payload.get("start_date"),
        "rows_added": summary["added_backfill_rows"],
        "partial_basis_dates": [
            row["date"] for row in merged_rows
            if (row.get("composition") or {}).get("missing_codes")
        ],
        "updated_at": backfill_payload.get("generated_at"),
    }

    print("[merge] 变更摘要：")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    if args.dry_run:
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(new_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[merge] 写入 {output_path}（{len(merged_rows)} 根 bar）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())