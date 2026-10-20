#!/usr/bin/env python3
"""Create a reproducible A-share research snapshot from the installed integrations.

The HTTP routes and field mapping are adapted from a-stock-data v3.7.1.  This is
a bounded research/smoke-test harness, not a replacement market-data client.
Prices for equities are forward-adjusted; index levels need no adjustment.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CN_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
# 2026-10-11 起 fqkline/get 被 WAF 501 拦截，前复权日K改走 newfqkline（需显式日期）。
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"
# 前复权日K三主机轮换：腾讯 WAF 的封禁粒度是「IP+主机」，
# proxy.finance.qq.com 与前两台不同域名（独立网关），是真正有价值的第三条线。
# 主机顺序与 derivatives_regime.py 保持一致。
TENCENT_KLINE_HOSTS = (
    TENCENT_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
TENCENT_RAW_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
SINA_KLINE = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketDataService.getKLineData"
)
SINA_FINANCE = (
    "https://quotes.sina.cn/cn/api/openapi.php/"
    "CompanyFinanceService.getFinanceReport2022"
)
# 财报备援源：东财 datacenter（新浪失效时启用）。datacenter 限流较严，
# 必须串行 + 间隔 ≥1.2s（见 _throttle_datacenter）。
EM_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_FINANCE_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
EM_MIN_INTERVAL_SECONDS = 1.2
EM_JITTER_SECONDS = 0.4

RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    status=3,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
SESSION.mount("https://", HTTPAdapter(max_retries=RETRY))

# 粘性主机：记住最近一次成功的主机，失败才轮换下一个
# （与 dashboard_server._TENCENT_KLINE_STICKY 同一思路，避免反复撞被封主机）。
_TENCENT_KLINE_STICKY_LOCK = threading.Lock()
_TENCENT_KLINE_STICKY: dict[str, int] = {}
# 东财 datacenter 的串行节流锁
_EM_LOCK = threading.Lock()
_EM_LAST_CALL = 0.0

INDEXES = {
    "上证综指": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50": "sh000688",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "中证1000": "sh000852",
}

DEFAULT_STOCKS = {
    "紫金矿业": "sh601899",
    "洛阳钼业": "sh603993",
    "华鲁恒升": "sh600426",
    "云天化": "sh600096",
    "卫星化学": "sz002648",
    "桐昆股份": "sh601233",
    "药明康德": "sh603259",
    "中信证券": "sh600030",
    "宁波银行": "sz002142",
    "云图控股": "sz002539",
    "招商轮船": "sh601872",
    "浪潮信息": "sz000977",
    "赤峰黄金": "sh600988",
    "苏垦农发": "sh601952",
    "隆平高科": "sz000998",
}


def _request_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = SESSION.get(
        url,
        params=params,
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=20,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected JSON object from {response.url}")
    return value


def _request_json_from_hosts(
    purpose: str, hosts: tuple[str, ...], params: dict[str, Any]
) -> dict[str, Any]:
    """腾讯接口轮换主机：WAF 会对单主机做整站拦截，换一台即可。

    粘住最近一次成功的主机，下次直接从它开始；只有失败才轮换到下一个主机。
    全部主机都失败才抛错。
    """
    with _TENCENT_KLINE_STICKY_LOCK:
        start_index = _TENCENT_KLINE_STICKY.get(purpose, 0)
    last_error: Exception | None = None
    for offset in range(len(hosts)):
        index = (start_index + offset) % len(hosts)
        try:
            response = SESSION.get(
                hosts[index],
                params=params,
                headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
                timeout=20,
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError(f"Expected JSON object from {hosts[index]}")
        except Exception as exc:
            last_error = exc
            continue
        with _TENCENT_KLINE_STICKY_LOCK:
            _TENCENT_KLINE_STICKY[purpose] = index
        return value
    raise RuntimeError(
        f"腾讯接口主机全部失败: {type(last_error).__name__}: {last_error}"
    )


def fetch_kline(symbol: str, count: int = 320) -> pd.DataFrame:
    from datetime import date as _date, timedelta as _timedelta

    # 快照口径 = **已完成交易日**：end 取"明天"，最后一根永远是定稿 bar。
    # 盘中临时 bar 不进快照——data_date 必须等于已收盘交易日，否则
    # forward_validation 账本登记的当日会话会被盘中数据改写（口径污染）。
    end_day = _date.today() + _timedelta(days=1)
    start_day = end_day - _timedelta(days=int(count * 1.6) + 40)
    payload = _request_json_from_hosts(
        "tencent_daily",
        TENCENT_KLINE_HOSTS,
        {"param": f"{symbol},day,{start_day.isoformat()},{end_day.isoformat()},{count},qfq"},
    )
    body = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = body.get("qfqday") or body.get("day") or []
    if not raw_rows:
        raise RuntimeError(f"Tencent returned no daily bars for {symbol}")
    # Corporate-action rows may carry a seventh marker; OHLCV are always first six.
    frame = pd.DataFrame(
        [row[:6] for row in raw_rows],
        columns=["date", "open", "close", "high", "low", "volume"],
    )
    for column in ("open", "close", "high", "low", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna().sort_values("date").reset_index(drop=True)
    if frame.empty:
        raise RuntimeError(f"Tencent returned no valid daily bars for {symbol}")
    return frame


def _return(close: pd.Series, sessions: int) -> float | None:
    if len(close) <= sessions or close.iloc[-1 - sessions] == 0:
        return None
    return float((close.iloc[-1] / close.iloc[-1 - sessions] - 1) * 100)


def _max_drawdown(close: pd.Series, sessions: int = 120) -> float:
    series = close.tail(sessions)
    drawdown = series / series.cummax() - 1
    return float(drawdown.min() * 100)


def technical_summary(frame: pd.DataFrame) -> dict[str, Any]:
    close = frame["close"]
    previous = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    relative_strength = gain / loss.replace(0, math.nan)
    daily_return = close.pct_change()

    moving_averages = {
        str(window): round(float(close.rolling(window).mean().iloc[-1]), 4)
        for window in (20, 60, 120)
        if len(close) >= window
    }
    returns = {
        str(window): None if _return(close, window) is None else round(_return(close, window), 4)
        for window in (5, 20, 60, 120)
    }
    return {
        "date": str(frame["date"].iloc[-1]),
        "close": round(float(close.iloc[-1]), 4),
        "recent_closes": [
            {"date": str(row.date), "close": round(float(row.close), 4)}
            for row in frame[["date", "close"]].tail(80).itertuples(index=False)
        ],
        "returns_pct": returns,
        "moving_averages": moving_averages,
        "atr14": round(float(true_range.rolling(14).mean().iloc[-1]), 4),
        "rsi14": round(float((100 - 100 / (1 + relative_strength)).iloc[-1]), 2),
        "volatility_20d_annualized_pct": round(
            float(daily_return.tail(20).std() * math.sqrt(242) * 100), 2
        ),
        "volatility_60d_annualized_pct": round(
            float(daily_return.tail(60).std() * math.sqrt(242) * 100), 2
        ),
        "max_drawdown_120d_pct": round(_max_drawdown(close), 2),
        "volume_5d_vs_20d_pct": round(
            float((frame["volume"].tail(5).mean() / frame["volume"].tail(20).mean() - 1) * 100),
            2,
        ),
        "high_20d": round(float(frame["high"].tail(20).max()), 4),
        "low_20d": round(float(frame["low"].tail(20).min()), 4),
        "high_60d": round(float(frame["high"].tail(60).max()), 4),
        "low_60d": round(float(frame["low"].tail(60).min()), 4),
    }


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_quotes(symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    requested = list(symbols)
    response = SESSION.get(
        TENCENT_QUOTE + ",".join(requested),
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=20,
    )
    response.raise_for_status()
    raw = response.content.decode("gbk")
    result: dict[str, dict[str, Any]] = {}
    for line in raw.split(";"):
        if '"' not in line:
            continue
        symbol = line.split("=")[0].split("_")[-1]
        values = line.split('"')[1].split("~")
        if len(values) < 80 or not values[3]:
            continue
        result[symbol] = {
            "name": values[1],
            "code": values[2],
            "price": _float(values[3]),
            "change_pct": _float(values[32]),
            "amount_yi": None if _float(values[37]) is None else round(float(values[37]) / 1e4, 4),
            "turnover_pct": _float(values[38]),
            "pe_ttm": _float(values[39]),
            "float_mcap_yi": _float(values[44]),
            "mcap_yi": _float(values[45]),
            "pb": _float(values[46]),
            "ret_5d_pct": _float(values[63]),
            "ret_10d_pct": _float(values[69]),
            "ret_20d_pct": _float(values[70]),
            "ret_60d_pct": _float(values[71]),
            "ret_ytd_pct": _float(values[79]),
            "server_time": values[30],
            "is_stale": bool(
                (_float(values[37]) or 0) == 0
                and (_float(values[3]) or 0) == (_float(values[4]) or 0)
                and (_float(values[3]) or 0) > 0
            ),
        }
    missing = sorted(set(requested) - set(result))
    if missing:
        raise RuntimeError(f"Tencent quote missing symbols: {missing}")
    return result


def fetch_breadth(symbol: str) -> dict[str, Any]:
    payload = _request_json(
        TENCENT_RAW_KLINE, {"param": f"{symbol},day,,,5"}
    )
    body = (payload.get("data") or {}).get(symbol) or {}
    quote = ((body.get("qt") or {}).get(symbol) or [])
    rank = ((body.get("qt") or {}).get("zhishu") or [])
    if len(rank) < 6 or len(quote) < 38:
        raise RuntimeError(f"Tencent breadth fields missing for {symbol}")
    return {
        "up": int(rank[2]),
        "flat": int(rank[3]),
        "down": int(rank[4]),
        "total": int(rank[5]),
        "amount_yi": round(float(quote[37]) / 1e4, 4),
        "server_time": quote[30],
    }


def _finance_item(items: list[dict[str, Any]], names: tuple[str, ...]) -> dict[str, Any] | None:
    for name in names:
        for item in items:
            if item.get("item_title") == name and item.get("item_value") not in (None, ""):
                yoy = item.get("item_tongbi")
                return {
                    "label": name,
                    "value": _float(item.get("item_value")),
                    "yoy_pct": None if _float(yoy) is None else round(float(yoy) * 100, 2),
                }
    return None


def _sina_financial_result(
    payload: dict[str, Any], as_of_date: str | None
) -> dict[str, Any]:
    """新浪财报（利润表 source=lrb）→ 对外结构。口径与改动前完全一致。"""
    reports = (((payload.get("result") or {}).get("data") or {}).get("report_list") or {})
    if not reports:
        return {"available": False}
    cutoff = as_of_date.replace("-", "") if as_of_date else None
    eligible_periods = [
        period
        for period, report in reports.items()
        if (not cutoff)
        or (
            period <= cutoff
            and str((report or {}).get("publish_date") or "") <= cutoff
            and bool((report or {}).get("publish_date"))
        )
    ]
    if not eligible_periods:
        return {"available": False, "as_of_date": as_of_date}
    period = sorted(eligible_periods, reverse=True)[0]
    report = reports.get(period) or {}
    items = report.get("data") or []
    return {
        "available": True,
        "period": f"{period[:4]}-{period[4:6]}-{period[6:8]}",
        "publish_date": str(report.get("publish_date") or ""),
        "as_of_date": as_of_date,
        "revenue": _finance_item(items, ("营业总收入", "营业收入")),
        "net_profit": _finance_item(
            items,
            (
                "归属于母公司所有者的净利润",
                "归属于母公司的净利润",
                "归属于母公司股东的净利润",
                "归属于上市公司股东的净利润",
                "净利润",
            ),
        ),
    }


def _throttle_datacenter() -> None:
    """东财 datacenter 限流较严：必须串行 + 间隔 ≥1.2s（再加一点随机抖动）。"""
    global _EM_LAST_CALL
    with _EM_LOCK:
        wait = EM_MIN_INTERVAL_SECONDS + random.uniform(0.0, EM_JITTER_SECONDS)
        elapsed = time.monotonic() - _EM_LAST_CALL
        if elapsed < wait:
            time.sleep(wait - elapsed)
        _EM_LAST_CALL = time.monotonic()


def _em_secucode(symbol: str) -> str:
    """腾讯 symbol（sh601899）→ 东财 SECUCODE（601899.SH）。"""
    prefix = symbol[:2].lower()
    code = symbol[2:] if prefix in ("sh", "sz", "bj") else symbol
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix, "SH")
    return f"{code}.{suffix}"


def _em_finance_item(
    row: dict[str, Any], label: str, value_field: str, yoy_field: str
) -> dict[str, Any] | None:
    """东财字段 → 对外结构（东财同比已是百分数，新浪是小数口径，勿混用）。"""
    value = _float(row.get(value_field))
    if value is None:
        return None
    yoy = _float(row.get(yoy_field))
    return {
        "label": label,
        "value": value,
        "yoy_pct": None if yoy is None else round(yoy, 2),
    }


def _eastmoney_financial_result(
    symbol: str, as_of_date: str | None
) -> dict[str, Any]:
    """东财 datacenter 主要财务指标（备援源）→ 对外结构。

    口径取舍：
    * 营业总收入可直接对上——实测东财 TOTALOPERATEREVE 与新浪「营业总收入 /
      营业收入」同值（一般企业、银行、券商都验过）。
    * 净利润留空——东财只给「归母净利润」，而新浪对银行/券商取的是「净利润」
      （含少数股东损益），不是同一行；挂一个口径不同的数字比留空更危险，
      按项目「宁可丢弃也不能把错误数字挂到看板上」的原则留空。
    """
    _throttle_datacenter()
    response = SESSION.get(
        EM_DATACENTER,
        params={
            "reportName": EM_FINANCE_REPORT,
            "columns": "ALL",
            "pageSize": "12",
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "filter": f'(SECUCODE="{_em_secucode(symbol)}")',
        },
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
        timeout=20,
    )
    response.raise_for_status()
    rows = ((response.json().get("result") or {}).get("data")) or []
    cutoff = as_of_date.replace("-", "") if as_of_date else None
    eligible: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = str(row.get("REPORT_DATE") or "")[:10].replace("-", "")
        publish = str(row.get("NOTICE_DATE") or "")[:10].replace("-", "")
        if not period or not publish:
            continue
        if cutoff and (period > cutoff or publish > cutoff):
            continue
        eligible.append((period, publish, row))
    if not eligible:
        return {"available": False, "as_of_date": as_of_date}
    eligible.sort(key=lambda item: item[0], reverse=True)
    period, publish, row = eligible[0]
    return {
        "available": True,
        "period": f"{period[:4]}-{period[4:6]}-{period[6:8]}",
        "publish_date": publish,
        "as_of_date": as_of_date,
        "revenue": _em_finance_item(
            row, "营业总收入", "TOTALOPERATEREVE", "TOTALOPERATEREVETZ"
        ),
        "net_profit": None,
    }


def fetch_latest_financial(
    symbol: str, as_of_date: str | None = None
) -> dict[str, Any]:
    """最新财报（利润表口径）：主源新浪 → 备源东财 datacenter。

    对外结构保持不变（available / period / publish_date / as_of_date /
    revenue / net_profit）。主备源都失败时保留原有行为：抛主源错误。
    """
    primary_error: Exception | None = None
    primary_result: dict[str, Any] | None = None
    try:
        payload = _request_json(
            SINA_FINANCE,
            {
                "paperCode": symbol,
                "source": "lrb",
                "type": "0",
                "page": "1",
                "num": "4",
            },
        )
        primary_result = _sina_financial_result(payload, as_of_date)
    except Exception as exc:
        primary_error = exc
    if primary_result is not None and primary_result.get("available"):
        return primary_result
    # 主源没给出可用报告（失效或区间内无披露）→ 走备源
    try:
        return _eastmoney_financial_result(symbol, as_of_date)
    except Exception:
        if primary_error is not None:
            raise primary_error  # 主备都失败：保留原有行为（抛主源错误）
        return primary_result if primary_result is not None else {"available": False}


def cross_check_sina(symbol: str, expected_date: str, expected_close: float) -> dict[str, Any]:
    response = SESSION.get(
        SINA_KLINE,
        params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": 5},
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise RuntimeError("Sina cross-check returned no bars")
    latest = rows[-1]
    close = float(latest["close"])
    return {
        "date": latest["day"][:10],
        "close": close,
        "expected_date": expected_date,
        "expected_close": expected_close,
        "absolute_difference": round(abs(close - expected_close), 6),
        "date_matches": latest["day"][:10] == expected_date,
    }


def _turnover_history(
    previous_snapshot: dict[str, Any] | None,
    data_date: str,
    amount_yi: float,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    values: dict[str, float] = {}
    previous = previous_snapshot if isinstance(previous_snapshot, dict) else {}
    raw_history = previous.get("market_turnover_history")
    if isinstance(raw_history, list):
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            session = str(item.get("date") or "")[:10]
            amount = item.get("amount_yi")
            try:
                number = float(amount)
                datetime.strptime(session, "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if number > 0 and session <= data_date:
                values[session] = number
    previous_date = str(previous.get("data_date") or "")[:10]
    previous_breadth = previous.get("breadth") if isinstance(previous.get("breadth"), dict) else {}
    previous_combined = (
        previous_breadth.get("沪深合计")
        if isinstance(previous_breadth.get("沪深合计"), dict)
        else {}
    )
    try:
        previous_amount = float(previous_combined.get("amount_yi"))
    except (TypeError, ValueError):
        previous_amount = 0.0
    if previous_date and previous_date <= data_date and previous_amount > 0:
        values[previous_date] = previous_amount
    values[data_date] = float(amount_yi)
    return [
        {"date": session, "amount_yi": round(values[session], 4)}
        for session in sorted(values)[-limit:]
    ]


def build_snapshot(previous_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    index_data = {
        name: {"symbol": symbol, **technical_summary(fetch_kline(symbol))}
        for name, symbol in INDEXES.items()
    }
    dates = {item["date"] for item in index_data.values()}
    if len(dates) != 1:
        raise RuntimeError(f"Index dates are inconsistent: {sorted(dates)}")
    data_date = next(iter(dates))
    breadth = {market: fetch_breadth(symbol) for market, symbol in {
        "上海": "sh000001", "深圳": "sz399001"
    }.items()}
    breadth["沪深合计"] = {
        field: round(sum(float(breadth[m][field]) for m in ("上海", "深圳")), 4)
        for field in ("up", "flat", "down", "total", "amount_yi")
    }

    quotes = fetch_quotes(DEFAULT_STOCKS.values())
    stock_data: dict[str, Any] = {}
    for name, symbol in DEFAULT_STOCKS.items():
        stock_data[name] = {
            "symbol": symbol,
            "quote": quotes[symbol],
            "technical": technical_summary(fetch_kline(symbol)),
            "latest_financial": fetch_latest_financial(symbol, data_date),
        }

    sina_checks = {
        name: cross_check_sina(symbol, item["date"], float(item["close"]))
        for name, item in index_data.items()
        for symbol in (str(item["symbol"]),)
    }
    stock_dates_match = all(
        item["technical"]["date"] == data_date for item in stock_data.values()
    )
    quote_dates_match = all(
        item["quote"]["server_time"][:8] == data_date.replace("-", "")
        for item in stock_data.values()
    )
    stale_quotes = [
        name for name, item in stock_data.items() if item["quote"]["is_stale"]
    ]
    cutoff = data_date.replace("-", "")
    financial_as_of_violations = [
        name
        for name, item in stock_data.items()
        if item["latest_financial"].get("available")
        and (
            str(item["latest_financial"].get("period") or "").replace("-", "")
            > cutoff
            or str(item["latest_financial"].get("publish_date") or "") > cutoff
        )
    ]
    max_sina_difference = max(
        float(item["absolute_difference"]) for item in sina_checks.values()
    )
    validation = {
        "sina_indices": sina_checks,
        "all_sina_dates_match": all(
            bool(item["date_matches"]) for item in sina_checks.values()
        ),
        "max_sina_close_difference": round(max_sina_difference, 6),
        "sina_close_tolerance": 0.01,
        "stock_dates_match": stock_dates_match,
        "quote_dates_match": quote_dates_match,
        "stale_quote_symbols": stale_quotes,
        "financial_as_of_violations": financial_as_of_violations,
    }
    validation["passed"] = bool(
        validation["all_sina_dates_match"]
        and max_sina_difference <= validation["sina_close_tolerance"]
        and stock_dates_match
        and quote_dates_match
        and not stale_quotes
        and not financial_as_of_violations
    )

    market_turnover_history = _turnover_history(
        previous_snapshot,
        data_date,
        float(breadth["沪深合计"]["amount_yi"]),
    )
    return {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "data_date": data_date,
        "price_adjustment": "qfq (前复权) for equities; none for indices",
        "sources": {
            "tencent_daily": TENCENT_KLINE,
            "tencent_quote": TENCENT_QUOTE,
            "tencent_breadth": TENCENT_RAW_KLINE,
            "sina_cross_check": SINA_KLINE,
            "sina_financials": SINA_FINANCE,
            "recipe": "a-stock-data v3.7.1",
        },
        "validation": validation,
        "breadth": breadth,
        "market_turnover_history": market_turnover_history,
        "indices": index_data,
        "stocks": stock_data,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", default="data/market-snapshot.json", help="JSON output path"
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    previous_snapshot = None
    if output.exists():
        try:
            loaded = json.loads(output.read_text(encoding="utf-8"))
            previous_snapshot = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            previous_snapshot = None
    snapshot = build_snapshot(previous_snapshot=previous_snapshot)
    check = snapshot["validation"]
    ok = bool(check["passed"])
    if ok:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, output)
        finally:
            temporary = Path(temporary_name)
            if temporary.exists():
                temporary.unlink()
    print(
        json.dumps(
            {
                "ok": ok,
                "data_date": snapshot["data_date"],
                "index_count": len(snapshot["indices"]),
                "stock_count": len(snapshot["stocks"]),
                "output": str(output),
                "written": ok,
                "sina_index_count": len(check["sina_indices"]),
                "max_sina_close_difference": check["max_sina_close_difference"],
                "financial_as_of_violation_count": len(
                    check["financial_as_of_violations"]
                ),
            },
            ensure_ascii=False,
        )
    )
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
