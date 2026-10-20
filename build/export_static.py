#!/usr/bin/env python3
"""Export the local dashboard's read-only API responses into static JSON files.

The exporter talks to a locally running ``scripts/dashboard_server.py`` over
loopback HTTP, which keeps the export identical to what the real dashboard
serves (including live Tencent quotes and all derived calculations).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SLOW_KEYS = {"tomorrow-plans", "paper-t", "paper-t-history"}
DEFAULT_HISTORY_DAYS = 8


def log(message: str) -> None:
    print(f"[export] {message}", flush=True)


def fetch_json(base: str, path: str, timeout: float = 180.0) -> Any:
    url = base.rstrip("/") + path
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "a-stock-static-exporter/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        raise ValueError("empty response")
    return json.loads(raw.decode("utf-8"))


def write_json(out_dir: Path, key: str, payload: Any) -> int:
    if not SAFE_ID.match(key):
        raise ValueError(f"unsafe export key: {key!r}")
    path = out_dir / f"{key}.json"
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def canonical_from_tencent(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    return raw


def tencent_from_canonical(symbol: str) -> str:
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", str(symbol or "").strip().upper())
    if match:
        return f"{match.group(2).lower()}{match.group(1)}"
    return ""


def collect_symbols(quotes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (position/chart symbols, index/global chart symbols)."""
    canonical: list[str] = []
    for row in quotes.get("positions") or []:
        if not isinstance(row, dict):
            continue
        symbol = canonical_from_tencent(str(row.get("symbol") or ""))
        if symbol and symbol not in canonical:
            canonical.append(symbol)
    chart_only: list[str] = []
    markets = quotes.get("markets") or {}
    for group in ("indices", "global", "overnight"):
        for row in markets.get(group) or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            if symbol and symbol not in chart_only:
                chart_only.append(symbol)
    return canonical, chart_only


def main() -> int:
    parser = argparse.ArgumentParser(description="Export dashboard APIs to static JSON")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--out", required=True, help="output directory (site/)")
    parser.add_argument("--fast", action="store_true", help="skip slow endpoints")
    parser.add_argument(
        "--history-days",
        type=int,
        default=int(os.environ.get("INTRADAY_HISTORY_DAYS") or DEFAULT_HISTORY_DAYS),
        help="number of intraday history dates to export",
    )
    args = parser.parse_args()

    out_dir = Path(args.out) / "static-api"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = args.base_url

    exported: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    def save(key: str, path: str, timeout: float = 180.0, required: bool = False) -> Any:
        started = time.time()
        try:
            payload = fetch_json(base, path, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - export must continue on partial failures
            message = f"{type(exc).__name__}: {exc}"
            log(f"FAIL {key:42s} {path} ({message})")
            failures.append(f"{key}: {message}")
            payload = {
                "ok": False,
                "error_code": "STATIC_EXPORT_FAILED",
                "error": "该数据在最近一次静态导出时不可用",
                "export_error": message[:300],
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        size = write_json(out_dir, key, payload)
        exported[key] = {
            "path": path,
            "bytes": size,
            "ok": bool(payload.get("ok", True)) if isinstance(payload, dict) else True,
        }
        log(f"ok   {key:42s} {size:8d}B {time.time() - started:6.2f}s")
        return payload

    log("fetching portfolio registry")
    portfolios_payload = save("portfolios", "/api/portfolios", timeout=60, required=True)
    portfolio_ids: list[str] = []
    for item in (portfolios_payload or {}).get("portfolios") or []:
        if isinstance(item, dict) and item.get("portfolio_id"):
            portfolio_ids.append(str(item["portfolio_id"]))
    if not portfolio_ids:
        portfolio_ids = ["main"]

    log("refreshing quotes")
    # Force a synchronous refresh while the A-share session is open; outside
    # the session this simply returns the persisted close snapshot.
    quotes = save("quotes", "/api/quotes?refresh=1", timeout=180, required=True)
    positions, chart_only = collect_symbols(quotes if isinstance(quotes, dict) else {})
    log(f"portfolios={portfolio_ids} positions={len(positions)} chart_symbols={len(chart_only)}")

    # Portfolio-scoped variants. main is the default (no query parameter).
    for portfolio_id in portfolio_ids:
        suffix = "" if portfolio_id == "main" else f"--{portfolio_id}"
        query = "" if portfolio_id == "main" else f"?portfolio={urllib.parse.quote(portfolio_id)}"

        save(f"quotes{suffix}", f"/api/quotes{query}", timeout=60)
        save(f"risk{suffix}", f"/api/risk{query}", timeout=90)
        save(f"news{suffix}", f"/api/news{query}", timeout=90)
        save(f"daily-pnl-history{suffix}", f"/api/daily-pnl-history{query}", timeout=90)
        save(f"intraday-pnl{suffix}", f"/api/intraday-pnl{query}", timeout=120)
        save(f"intraday-pnl-dates{suffix}", f"/api/intraday-pnl/dates{query}", timeout=60)
        if not args.fast:
            save(f"paper-t{suffix}", f"/api/paper-t{query}", timeout=240)
            save(f"paper-t-history{suffix}", f"/api/paper-t/history{query}", timeout=120)
        else:
            log(f"skip slow paper-t{suffix}")

    # Intraday history for the most recent dates.
    for portfolio_id in portfolio_ids:
        suffix = "" if portfolio_id == "main" else f"--{portfolio_id}"
        dates_path = out_dir / f"intraday-pnl-dates{suffix}.json"
        try:
            dates_payload = json.loads(dates_path.read_text(encoding="utf-8"))
            dates = [str(item) for item in (dates_payload.get("dates") or []) if item]
        except Exception:
            dates = []
        dates = dates[: max(0, args.history_days)]
        log(f"intraday history dates for {portfolio_id}: {dates}")
        for date in dates:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                continue
            path = f"/api/intraday-pnl/history?date={urllib.parse.quote(date)}"
            if portfolio_id != "main":
                path += f"&portfolio={urllib.parse.quote(portfolio_id)}"
            save(
                f"intraday-pnl-history{suffix}--{date}",
                path,
                timeout=120,
            )

    # Global endpoints.
    save("personal-index", "/api/personal-index", timeout=90)
    save("portfolio-compare", "/api/portfolio-compare", timeout=120)
    save("market-news", "/api/market-news", timeout=90)
    save("decision-context", "/api/decision-context", timeout=120)
    save("market-state", "/api/market-state", timeout=120)
    save("events", "/api/events", timeout=120)
    save("opportunities", "/api/opportunities", timeout=120)
    save("rebalance", "/api/rebalance", timeout=120)
    save("school-signals", "/api/school-signals", timeout=120)
    save("leading-indicators", "/api/leading-indicators", timeout=120)
    save("derivatives-regime", "/api/derivatives-regime", timeout=120)
    save("autopilot", "/api/autopilot", timeout=60)
    save("autopilot-journal", "/api/autopilot/journal?limit=200", timeout=60)
    save("health", "/api/health", timeout=30)
    if not args.fast:
        save("tomorrow-plans", "/api/tomorrow-plans", timeout=240)
    else:
        log("skip slow tomorrow-plans")

    # Stock charts: held positions use canonical symbols, indices/globals use
    # Tencent symbols. Export both the sparkline variant and the full indicator
    # overlay used by the popover.
    for symbol in positions:
        save(f"stock-chart--{symbol}", f"/api/stock-chart?symbol={urllib.parse.quote(symbol)}", timeout=90)
        save(
            f"stock-chart--{symbol}--indicators",
            f"/api/stock-chart?symbol={urllib.parse.quote(symbol)}&indicators=ma,macd,rsi,kdj,boll,atr,vol_ma",
            timeout=120,
        )
    for symbol in chart_only:
        if symbol in positions:
            continue
        save(f"stock-chart--{symbol}", f"/api/stock-chart?symbol={urllib.parse.quote(symbol)}", timeout=90)
        save(
            f"stock-chart--{symbol}--indicators",
            f"/api/stock-chart?symbol={urllib.parse.quote(symbol)}&indicators=ma,macd,rsi,kdj,boll,atr,vol_ma",
            timeout=120,
        )

    # Technical signals: call with canonical symbols, then mirror each result
    # under the Tencent-style key the frontend uses for row lookups.
    if positions:
        params = ",".join(urllib.parse.quote(symbol) for symbol in positions)
        batch = save(
            "technical-signals-batch",
            f"/api/technical-signals-batch?symbols={params}",
            timeout=180,
        )
        if isinstance(batch, dict) and isinstance(batch.get("results"), dict):
            mirrored = dict(batch["results"])
            for symbol, result in list(batch["results"].items()):
                alias = tencent_from_canonical(symbol)
                if alias and alias not in mirrored:
                    mirrored[alias] = result
            batch["results"] = mirrored
            write_json(out_dir, "technical-signals-batch", batch)

    # Manifest of freshly exported keys (slow keys may come from the cache).
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fast": bool(args.fast),
        "keys": sorted(exported),
        "failures": failures,
    }
    (out_dir / "manifest-export.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"exported {len(exported)} keys, failures={len(failures)}")
    if failures:
        log("partial failures: " + "; ".join(failures[:8]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
