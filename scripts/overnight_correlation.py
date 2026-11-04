#!/usr/bin/env python3
"""隔夜全球标的 × 持仓组合 日收益率相关度快照。

前端 ``dashboard/app.js`` 的 ``OVERNIGHT_PORTFOLIO_CORRELATION`` 是一份硬编码
快照，本脚本就是它的生成口径，用来给新加入/替换的隔夜标的一个可复算的数字，
而不是拍脑袋填一个相关度。

口径定义
--------
* 组合口径：个人指数日K（``dashboard/personal-index/index_data.json``）——16 只
  成分股按市值加权、前复权，等价于"信用+普通账户市值加权"的持仓组合日收益率。
* 隔夜标的日收益率：韩股走 Naver fchart 日K；美股/中概/欧股/商品走东财
  push2his 日K（fqt=1 前复权）。
* ``same`` = corr(隔夜标的 t 日收益, 组合 t 日收益)
  ``next`` = corr(隔夜标的 t 日收益, 组合 t+1 交易日收益)
  后者衡量"隔夜信息对 A 股次日开盘的解释力"（韩股/美股 t 日收盘均早于 A 股
  t+1 开盘）。
* 窗口：截至 ``--end`` 的最近 ``--window`` 个组合交易日（默认 180）。

用法::

    .venv/bin/python scripts/overnight_correlation.py --end 2026-10-14
    .venv/bin/python scripts/overnight_correlation.py --end 2026-10-14 \
        --symbols b_SAMSUNG,b_BIDU --js

``--js`` 打印可直接粘进 app.js 的 ``OVERNIGHT_PORTFOLIO_CORRELATION`` 片段。
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "scripts" / "dashboard_server.py"
PERSONAL_INDEX_PATH = ROOT / "dashboard" / "personal-index" / "index_data.json"

NAVER_URL = "https://fchart.stock.naver.com/sise.nhn"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
USER_AGENT = "Mozilla/5.0"
REQUEST_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def load_instruments() -> list[dict[str, Any]]:
    """从 dashboard_server.py 解析隔夜标的清单（不 import，避免拉起整个服务）。"""
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        # 形如 ``NAME: type = [...]`` 是 AnnAssign，``NAME = [...]`` 是 Assign。
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        else:
            continue
        for target in targets:
            if getattr(target, "id", "") == "EM_OVERNIGHT_INSTRUMENTS" and value is not None:
                return list(ast.literal_eval(value))
    raise RuntimeError("EM_OVERNIGHT_INSTRUMENTS not found in dashboard_server.py")


def load_portfolio_closes() -> dict[str, float]:
    """个人指数日K收盘序列（组合市值加权口径）。"""
    payload = json.loads(PERSONAL_INDEX_PATH.read_text(encoding="utf-8"))
    closes: dict[str, float] = {}
    for bar in payload.get("kline") or []:
        date = str(bar.get("date") or "")
        close = bar.get("close")
        if date and isinstance(close, (int, float)) and close > 0:
            closes[date] = float(close)
    return closes


# ---------------------------------------------------------------------------
# 行情源
# ---------------------------------------------------------------------------
def fetch_naver_daily(symbol: str, count: int) -> dict[str, float]:
    """Naver fchart 日K → {date: close}（EUC-KR 解码）。"""
    query = urllib.parse.urlencode(
        {"symbol": symbol, "timeframe": "day", "count": int(count), "requestType": "0"}
    )
    request = urllib.request.Request(
        f"{NAVER_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://finance.naver.com/"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        text = response.read().decode("euc-kr", errors="replace")
    closes: dict[str, float] = {}
    for raw in re.findall(r'<item data="([^"]+)"', text):
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 6:
            continue
        date = _compact_to_date(parts[0])
        close = _to_float(parts[4])
        if date and close and close > 0:
            closes[date] = close
    if not closes:
        raise RuntimeError(f"naver returned no daily bars for {symbol}")
    return closes


def fetch_eastmoney_daily(secid: str, count: int) -> dict[str, float]:
    """东财 push2his 日K → {date: close}（fqt=1 前复权）。

    push2his 只完成 HTTP/2 握手，urllib/requests 的 HTTP/1.1 会被断连，
    与 dashboard_server._curl_get_json 一样走系统 curl。
    """
    query = urllib.parse.urlencode(
        {
            "secid": secid,
            "klt": 101,
            "fqt": 1,
            "lmt": int(count),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56",
        }
    )
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--http2",
            "--max-time",
            str(REQUEST_TIMEOUT_SECONDS),
            "-H",
            "Referer: https://quote.eastmoney.com/",
            f"{EASTMONEY_KLINE}?{query}",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl failed for {secid}: {result.stderr.decode('utf-8', 'replace')[:200]}")
    payload = json.loads(result.stdout.decode("utf-8", "replace"))
    body = payload.get("data")
    if not isinstance(body, dict):
        raise RuntimeError(f"eastmoney returned no kline data for {secid}")
    closes: dict[str, float] = {}
    for row in body.get("klines") or []:
        parts = [part.strip() for part in str(row).split(",")]
        if len(parts) < 3:
            continue
        date = _compact_to_date(parts[0])
        close = _to_float(parts[2])
        if date and close and close > 0:
            closes[date] = close
    if not closes:
        raise RuntimeError(f"eastmoney returned no daily bars for {secid}")
    return closes


def fetch_sina_us_daily(ticker: str) -> dict[str, float]:
    """新浪美股日K → {date: close}（不复权；东财 push2his 被 WAF 拦时的备源）。"""
    url = (
        "https://stock.finance.sina.com.cn/usstock/api/jsonp.php"
        "/var%20t=/US_MinKService.getDailyK?symbol="
        f"{urllib.parse.quote(ticker)}&___qn=3"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://finance.sina.com.cn/"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        text = response.read().decode("utf-8", errors="replace")
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end <= start:
        raise RuntimeError("sina US kline returned no JSONP payload")
    rows = json.loads(text[start + 1 : end])
    closes: dict[str, float] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        date = str(row.get("d") or "")
        close = _to_float(row.get("c"))
        if len(date) == 10 and close and close > 0:
            closes[date] = close
    if not closes:
        raise RuntimeError(f"sina US returned no daily bars for {ticker}")
    return closes


def fetch_foreign_closes(item: dict[str, Any], count: int) -> dict[str, float]:
    """按标的类型取日K：美股/中概 ADR 走新浪，其余有东财 secid 的走东财，
    只有 Naver 代码的才走 Naver。

    优先级与 dashboard_server._overnight_correlation_closes 保持一致——韩股
    在东财是 177.*（可用且含当日 bar），而 Naver 在国内常整站连不上。
    """
    secid = str(item.get("em_secid") or "")
    market = secid.split(".", 1)[0] if "." in secid else ""
    if market in {"105", "106", "153"} and "." in secid:
        # 东财 push2his 对美股 secid 直连是连接级封禁，新浪是同口径备源。
        try:
            return fetch_eastmoney_daily(secid, count)
        except Exception:
            return fetch_sina_us_daily(secid.split(".", 1)[1])
    if secid:
        return fetch_eastmoney_daily(secid, count)
    if item.get("naver_symbol"):
        return fetch_naver_daily(str(item["naver_symbol"]), count)
    raise RuntimeError(f"{item.get('symbol')} 没有可用日K源")


# ---------------------------------------------------------------------------
# 相关度计算
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_to_date(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) == 6:
        return f"{digits[0:4]}-{digits[4:6]}-01"
    return ""


def daily_returns(closes: dict[str, float]) -> dict[str, float]:
    """收盘序列 → 日收益率 {date: ret}；跨休市按上一根可用K线计算。"""
    returns: dict[str, float] = {}
    previous: float | None = None
    for date in sorted(closes):
        close = closes[date]
        if previous is not None and previous > 0:
            returns[date] = close / previous - 1.0
        previous = close
    return returns


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 20 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def correlation_vs_portfolio(
    foreign_returns: dict[str, float],
    portfolio_returns: dict[str, float],
    portfolio_dates: list[str],
    window_dates: set[str],
) -> dict[str, Any]:
    """same / next 相关度：隔夜标的 t 日收益 vs 组合 t 日 / t+1 交易日收益。"""
    same_x: list[float] = []
    same_y: list[float] = []
    next_x: list[float] = []
    next_y: list[float] = []
    for index, date in enumerate(portfolio_dates):
        if date not in window_dates:
            continue
        foreign = foreign_returns.get(date)
        if foreign is None:
            continue
        portfolio = portfolio_returns.get(date)
        if portfolio is not None:
            same_x.append(foreign)
            same_y.append(portfolio)
        if index + 1 < len(portfolio_dates):
            following = portfolio_returns.get(portfolio_dates[index + 1])
            if following is not None:
                next_x.append(foreign)
                next_y.append(following)
    same = pearson(same_x, same_y)
    next_corr = pearson(next_x, next_y)
    return {
        "same": same,
        "next": next_corr,
        "same_n": len(same_x),
        "next_n": len(next_x),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="隔夜全球标的 × 持仓组合 相关度快照")
    parser.add_argument("--end", default="2026-10-14", help="快照截止日（YYYY-MM-DD）")
    parser.add_argument("--window", type=int, default=180, help="组合交易日窗口")
    parser.add_argument("--symbols", default="", help="只算这些 symbol，逗号分隔")
    parser.add_argument("--js", action="store_true", help="打印 app.js 片段")
    parser.add_argument("--min-abs", type=float, default=0.0, help="只输出 |same| ≥ 该值的标的")
    args = parser.parse_args()

    instruments = load_instruments()
    if args.symbols:
        wanted = {item.strip() for item in args.symbols.split(",") if item.strip()}
        instruments = [item for item in instruments if item["symbol"] in wanted]
    if not instruments:
        print("no instruments matched", file=sys.stderr)
        return 1

    portfolio_closes = load_portfolio_closes()
    portfolio_returns = daily_returns(portfolio_closes)
    all_dates = sorted(portfolio_returns)
    eligible = [date for date in all_dates if date <= args.end]
    if not eligible:
        print(f"no portfolio bars on or before {args.end}", file=sys.stderr)
        return 1
    window_dates = set(eligible[-max(20, args.window) :])

    print(
        f"组合窗口: {min(window_dates)} ~ {max(window_dates)} "
        f"({len(window_dates)} 个组合交易日, 截止 {args.end})"
    )
    header = f"{'symbol':<12}{'名称':<10}{'same':>8}{'next':>8}{'n_same':>8}{'n_next':>8}"
    print(header)
    print("-" * len(header))

    snapshot: dict[str, dict[str, float]] = {}
    for item in instruments:
        symbol = str(item["symbol"])
        name = str(item.get("name") or symbol)
        try:
            closes = fetch_foreign_closes(item, args.window + 120)
        except Exception as exc:  # 单个标的失败不影响其余标的
            print(f"{symbol:<12}{name:<10}  fetch failed: {type(exc).__name__}: {exc}")
            continue
        result = correlation_vs_portfolio(
            daily_returns(closes), portfolio_returns, all_dates, window_dates
        )
        same = result["same"]
        next_corr = result["next"]
        if same is None or next_corr is None:
            print(f"{symbol:<12}{name:<10}  insufficient overlap (n={result['same_n']})")
            continue
        if abs(same) < args.min_abs:
            continue
        snapshot[symbol] = {"same": round(same, 4), "next": round(next_corr, 4)}
        print(
            f"{symbol:<12}{name:<10}{same:>8.2f}{next_corr:>8.2f}"
            f"{result['same_n']:>8}{result['next_n']:>8}"
        )

    if args.js:
        print("\n// 可粘贴片段：")
        for symbol, values in snapshot.items():
            print(f"  {symbol}: {{ same: {values['same']:.2f}, next: {values['next']:.2f} }},")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
