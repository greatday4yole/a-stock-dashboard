#!/usr/bin/env python3
"""A股看板数据源健康检查（零项目依赖，可独立分享运行）。

按 dashboard_server.py / market_intel.py / market_news.py 的真实调用方式
（同 Header、同 Referer、同传输通道）逐一探测每个数据源，输出通过/失败与关键样例。

用法：
    .venv/bin/python scripts/data_source_healthcheck.py            # 全量探测
    .venv/bin/python scripts/data_source_healthcheck.py --only tencent,em
    .venv/bin/python scripts/data_source_healthcheck.py --json out.json

探测纪律：串行 + 间隔，绝不并发；东财系每次请求间隔 ≥1.2s（低于生产节流也
低于社区实测封禁阈值 5 req/s），避免健康检查自己触发 WAF。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CN_TZ = timezone(timedelta(hours=8))

GROUPS = {
    "tencent": "腾讯",
    "sina": "新浪",
    "eastmoney": "东方财富",
    "news": "资讯",
    "official": "官方/机构",
}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    adapter = HTTPAdapter(max_retries=0, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _session()


class Result:
    def __init__(self, group: str, name: str) -> None:
        self.group = group
        self.name = name
        self.ok = False
        self.latency_ms = 0
        self.detail = ""
        self.error = ""

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "name": self.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "error": self.error,
        }


RESULTS: list[Result] = []


def record(group: str, name: str):
    def deco(fn):
        def run():
            res = Result(GROUPS[group], name)
            start = time.monotonic()
            try:
                detail = fn()
                res.ok = True
                res.detail = detail or "ok"
            except Exception as exc:
                res.error = f"{type(exc).__name__}: {exc}"[:300]
            res.latency_ms = int((time.monotonic() - start) * 1000)
            RESULTS.append(res)
            mark = "PASS" if res.ok else "FAIL"
            print(f"[{mark}] {res.group:<6} {res.name:<28} {res.latency_ms:>5}ms  {res.detail or res.error}")
        return run
    return deco


def curl_get_json(url: str, referer: str, timeout: float = 10.0) -> dict:
    """东财 push2/push2his 系只接受 HTTP/2，requests 会被断连，必须走系统 curl。"""
    command = [
        "/usr/bin/curl", "--silent", "--show-error", "--fail", "--compressed",
        "--max-time", str(int(timeout)),
        "-H", f"User-Agent: {UA}",
        "-H", f"Referer: {referer}",
        url,
    ]
    proc = subprocess.run(command, capture_output=True, check=True, timeout=timeout + 4)
    payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("response is not a JSON object")
    return payload


def sleep_pace(seconds: float) -> None:
    time.sleep(seconds)


# ── 腾讯 ──────────────────────────────────────────────────────────────

@record("tencent", "实时行情 qt.gtimg.cn")
def tencent_quote():
    res = SESSION.get(
        "https://qt.gtimg.cn/q=sh600519,sz000977,sh000001",
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    text = res.content.decode("gbk", "replace")
    lines = [ln for ln in text.split(";") if '="' in ln]
    if len(lines) < 3:
        raise RuntimeError(f"only {len(lines)} quote lines")
    first = lines[0].split('"')[1].split("~")
    return f"3 quotes, e.g. {first[1]}={first[3]}"


@record("tencent", "前复权日K newfqkline(web.ifzq)")
def tencent_newfqkline_web():
    # 2026-10-11 起 fqkline/get 双主机被 WAF 501 拦截（端点级），日K改走
    # newfqkline/get；newfqkline 必须显式传起止日期，空日期返回 200 但数据为空。
    end_day = datetime.now(CN_TZ).date() + timedelta(days=1)
    start_day = end_day - timedelta(days=500 * 1.6 + 40)
    res = SESSION.get(
        "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
        params={"param": f"sh600519,day,{start_day.isoformat()},{end_day.isoformat()},500,qfq"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("sh600519") or {}
    rows = body.get("qfqday") or body.get("day") or []
    if not rows:
        raise RuntimeError("empty qfqday (空日期区间会被静默返回空)")
    return f"500 bars, latest {rows[-1][0]}"


@record("tencent", "前复权日K newfqkline(ifzq备机)")
def tencent_newfqkline_ifzq():
    end_day = datetime.now(CN_TZ).date() + timedelta(days=1)
    start_day = end_day - timedelta(days=500 * 1.6 + 40)
    res = SESSION.get(
        "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
        params={"param": f"sh600519,day,{start_day.isoformat()},{end_day.isoformat()},500,qfq"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("sh600519") or {}
    rows = body.get("qfqday") or body.get("day") or []
    if not rows:
        raise RuntimeError("empty qfqday")
    return f"500 bars via ifzq, latest {rows[-1][0]}"


@record("tencent", "美股指数日K usfqkline")
def tencent_usfqkline():
    res = SESSION.get(
        "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
        params={"param": "usDJI,day,,,5,qfq"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("usDJI") or {}
    rows = body.get("qfqday") or body.get("day") or []
    if not rows:
        raise RuntimeError("empty usfqkline")
    return f"5 bars, latest {rows[-1][0]} close={rows[-1][2]}"


@record("tencent", "5分钟K线 mkline")
def tencent_mkline():
    res = SESSION.get(
        "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
        params={"param": "sh600519,m5,,40"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    rows = ((res.json().get("data") or {}).get("sh600519") or {}).get("m5") or []
    if not rows:
        raise RuntimeError("empty m5")
    return f"{len(rows)} m5 bars, latest {rows[-1][0]}"


@record("tencent", "港美指数分时 minute/query")
def tencent_minute():
    res = SESSION.get(
        "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
        params={"code": "hkHSI"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = ((res.json().get("data") or {}).get("hkHSI") or {}).get("data") or {}
    points = body.get("data") or []
    if not points:
        raise RuntimeError("empty minute points")
    return f"{len(points)} points, date={body.get('date')}"


@record("tencent", "港美分时备主机 proxy.finance.qq.com")
def tencent_minute_proxy():
    # 港美分时原为单一主机（web.ifzq），无任何备援；proxy.finance.qq.com 是独立
    # 网关，web.ifzq 被 WAF 整主机拦截时接管（2026-10-20 实测 193 点）。
    res = SESSION.get(
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
        params={"code": "hkHSI"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = ((res.json().get("data") or {}).get("hkHSI") or {}).get("data") or {}
    points = body.get("data") or []
    if not points:
        raise RuntimeError("empty minute points")
    return f"{len(points)} points, date={body.get('date')}"


@record("tencent", "市场广度 kline(涨跌家数)")
def tencent_breadth():
    res = SESSION.get(
        "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
        params={"param": "sh000001,day,,,5"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("sh000001") or {}
    qt = body.get("qt") or {}
    rank = qt.get("zhishu") or []
    if len(rank) < 6:
        raise RuntimeError("qt.zhishu missing")
    return f"up={rank[2]} down={rank[4]} total={rank[5]}"


@record("tencent", "日K第三主机 proxy.finance.qq.com")
def tencent_newfqkline_proxy():
    # 第三主机（来源：stock_all_in_1 共享指南）；独立网关，web./ifzq 被 WAF 拦时仍可用。
    end_day = datetime.now(CN_TZ).date() + timedelta(days=1)
    start_day = end_day - timedelta(days=100)
    res = SESSION.get(
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        params={"param": f"sh600519,day,{start_day.isoformat()},{end_day.isoformat()},100,qfq"},
        headers={"Referer": "https://gu.qq.com/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("sh600519") or {}
    rows = body.get("qfqday") or body.get("day") or []
    if not rows:
        raise RuntimeError("empty qfqday")
    return f"100 bars via proxy, latest {rows[-1][0]}"


@record("tencent", "不复权日K newfqkline(backtest)")
def tencent_newfqkline():
    res = SESSION.get(
        "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
        params={"param": "sh600519,day,2026-09-01,2026-10-10,10,qfq"},
        headers={"User-Agent": UA}, timeout=20,
    )
    res.raise_for_status()
    body = (res.json().get("data") or {}).get("sh600519") or {}
    rows = body.get("qfqday") or body.get("day") or []
    if not rows:
        raise RuntimeError("empty newfqkline")
    return f"{len(rows)} bars, latest {rows[-1][0]}"


# ── 新浪 ──────────────────────────────────────────────────────────────

@record("sina", "全球参考 hq.sinajs.cn")
def sina_hq():
    res = SESSION.get(
        "https://hq.sinajs.cn/list=hf_ES,b_NKY,b_KOSPI,hf_CL,hf_GC,DINIW,fx_susdcnh",
        headers={"Referer": "https://finance.sina.com.cn/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    text = res.content.decode("gbk", "replace")
    rows = re.findall(r'hq_str_([^=]+)="([^"]+)"', text)
    nonempty = [(k, v) for k, v in rows if v]
    if len(nonempty) < 5:
        raise RuntimeError(f"only {len(nonempty)} non-empty quotes")
    return f"{len(nonempty)} quotes, e.g. {nonempty[0][0]}"


@record("sina", "A股实时备援 hq.sinajs.cn")
def sina_cn_quotes():
    res = SESSION.get(
        "https://hq.sinajs.cn/list=sh600519,sh000001",
        headers={"Referer": "https://finance.sina.com.cn/"}, timeout=(2.5, 5),
    )
    res.raise_for_status()
    text = res.content.decode("gbk", "replace")
    rows = re.findall(r'hq_str_((?:sh|sz|bj)\d{6})="([^"]+)"', text)
    if len(rows) < 2:
        raise RuntimeError("fewer than 2 CN quotes")
    fields = rows[0][1].split(",")
    return f"2 quotes, {rows[0][0]}={fields[3]}"


@record("sina", "全球期货日K GlobalFutures")
def sina_futures_daily():
    res = SESSION.get(
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/"
        "GlobalFuturesService.getGlobalFuturesDailyKLine",
        params={"symbol": "ES"},
        headers={"Referer": "https://finance.sina.com.cn/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    text = res.text
    payload = json.loads(text[text.find("(") + 1: text.rfind(")")])
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("empty futures kline")
    return f"{len(payload)} bars, latest {payload[-1].get('date')}"


@record("sina", "7x24直播流 zhibo feed")
def sina_zhibo():
    res = SESSION.get(
        "https://zhibo.sina.com.cn/api/zhibo/feed",
        params={"zhibo_id": "152", "page_size": "20", "dire": "f"},
        headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15,
    )
    res.raise_for_status()
    feed = ((((res.json().get("result") or {}).get("data") or {}).get("feed") or {}).get("list")) or []
    if not feed:
        raise RuntimeError("empty feed")
    return f"{len(feed)} feed items"


@record("sina", "日K交叉核验 getKLineData")
def sina_kline_crosscheck():
    res = SESSION.get(
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
        params={"symbol": "sh600519", "scale": "240", "ma": "no", "datalen": "5"},
        headers={"Referer": "https://finance.sina.com.cn/"}, timeout=20,
    )
    res.raise_for_status()
    rows = res.json()
    if not rows:
        raise RuntimeError("empty kline")
    return f"5 bars, latest {rows[-1].get('day')} close={rows[-1].get('close')}"


@record("sina", "财报三表 getFinanceReport2022")
def sina_finance_report():
    res = SESSION.get(
        "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
        params={"paperCode": "sh600519", "source": "lrb", "type": "0", "page": "1", "num": "4"},
        headers={"User-Agent": UA}, timeout=15,
    )
    res.raise_for_status()
    reports = (((res.json().get("result") or {}).get("data") or {}).get("report_list")) or {}
    if not reports:
        raise RuntimeError("empty report_list")
    return f"{len(reports)} periods: {sorted(reports.keys())[-1]}"


@record("sina", "个股资金流 MoneyFlow.ssi_ssfx_flzjtj")
def sina_money_flow():
    # 新浪个股资金流（2026-10-20 实测可达）：与东财口径的档位定义无法对齐验证，
    # 因此只作为"源可达性"探针，不接入生产资金流链路。
    res = SESSION.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "MoneyFlow.ssi_ssfx_flzjtj",
        params={"daima": "sh600519"},
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"}, timeout=15,
    )
    res.raise_for_status()
    payload = res.json()
    if not isinstance(payload, dict) or "netamount" not in payload:
        raise RuntimeError("unexpected moneyflow payload")
    return f"{payload.get('name')} netamount={payload.get('netamount')}"


# ── 东方财富 ──────────────────────────────────────────────────────────

@record("eastmoney", "日K push2his(klt=101 KOSPI)")
def em_kline_daily():
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "secid=100.KS11&klt=101&fqt=1&lmt=5&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (payload.get("data") or {}).get("klines") or []
    if not rows:
        raise RuntimeError("empty klines")
    return f"5 daily bars, latest {rows[-1].split(',')[0]}"


@record("eastmoney", "5分钟K push2his(klt=5 KOSPI)")
def em_kline_minute():
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        "secid=100.KS11&klt=5&fqt=1&lmt=10&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (payload.get("data") or {}).get("klines") or []
    if not rows:
        raise RuntimeError("empty klines")
    return f"10 m5 bars, latest {rows[-1].split(',')[0]}"


@record("eastmoney", "个股资金流 fflow/daykline")
def em_fund_flow():
    url = (
        "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "lmt=5&klt=101&secid=1.600519&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (payload.get("data") or {}).get("klines") or []
    if not rows:
        raise RuntimeError("empty fflow klines")
    parts = rows[-1].split(",")
    return f"5 rows, latest {parts[0]} main_pct={parts[6]}"


@record("eastmoney", "行业板块 clist(m:90+t:2)")
def em_industry_flow():
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=500&po=1&fid=f62&fs=m:90+t:2&fields=f12,f14,f62,f184"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    diff = ((payload.get("data") or {}).get("diff")) or {}
    if not diff:
        raise RuntimeError("empty diff")
    return f"{len(diff)} boards, top={next(iter(diff.values())).get('f14')}"


@record("eastmoney", "个股资金流备主机 push2his(fflow)")
def em_fund_flow_push2his():
    # push2 主机的 fflow 端点会"200 但 klines 为空"（软失效）；push2his 网关
    # 同一路径可正常返回历史。2026-10-20 实测 10-19 主力净额 314,089,632 与
    # datacenter 的 PRIME_INFLOW 完全一致。
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "lmt=3&klt=101&secid=1.600519&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (payload.get("data") or {}).get("klines") or []
    if not rows:
        raise RuntimeError("empty fflow klines")
    last = rows[-1].split(",")
    return f"{len(rows)} bars, latest {last[0]} main_net={last[1]}"


@record("eastmoney", "行业板块备主机 82.push2his(clist)")
def em_industry_flow_push2his():
    url = (
        "https://82.push2his.eastmoney.com/api/qt/clist/get?"
        "pn=1&pz=500&po=1&fid=f62&fs=m:90+t:2&fields=f12,f14,f62,f184"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    diff = ((payload.get("data") or {}).get("diff")) or {}
    if not diff:
        raise RuntimeError("empty diff")
    return f"{len(diff)} boards, top={next(iter(diff.values())).get('f14')}"


@record("eastmoney", "公告备援 np-anotice-stock")
def em_announcements():
    res = SESSION.get(
        "https://np-anotice-stock.eastmoney.com/api/security/ann",
        params={"page_size": "5", "page_index": "1", "ann_type": "A",
                "client_source": "web", "stock_list": "688545",
                "f_node": "0", "s_node": "0"},
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}, timeout=15,
    )
    res.raise_for_status()
    rows = ((res.json().get("data") or {}).get("list")) or []
    if not rows:
        raise RuntimeError("no announcements")
    return f"{len(rows)} announcements, latest {str(rows[0].get('title'))[:30]}"


@record("eastmoney", "财报主要指标 RPT_F10_FINANCE_MAINFINADATA")
def em_finance_main():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
                "pageSize": "3", "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                "filter": '(SECUCODE="688545.SH")'},
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    rows = (((res.json().get("result") or {}).get("data")) or [])
    if not rows:
        raise RuntimeError("empty finance rows")
    return f"{len(rows)} periods, latest {rows[0].get('REPORT_DATE')}"


@record("eastmoney", "宏观PMI RPT_ECONOMY_PMI")
def em_pmi():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_ECONOMY_PMI", "columns": "ALL",
                "pageSize": "3", "sortColumns": "REPORT_DATE", "sortTypes": "-1"},
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    rows = (((res.json().get("result") or {}).get("data")) or [])
    if not rows:
        raise RuntimeError("empty pmi rows")
    return f"{len(rows)} periods, latest {rows[0].get('TIME')}"


@record("eastmoney", "美债收益率 stock/get(171.US10Y)")
def em_bond_yield():
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        "secid=171.US10Y&fltt=2&fields=f43,f57,f58,f60,f86,f169,f170"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    price = (payload.get("data") or {}).get("f43")
    if not price or price <= 0:
        raise RuntimeError(f"invalid price {price}")
    return f"US10Y={price}"


@record("eastmoney", "当日资金流 datacenter STOCKNEW")
def em_stocknew_flow():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_DMSK_TS_STOCKNEW", "columns": "ALL",
                "pageSize": "1", "sortColumns": "TRADE_DATE", "sortTypes": "-1",
                "filter": '(SECURITY_CODE="600519")'},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("empty (非交易日为正常空)")
    r = data[0]
    main_net = (float(r.get("SUPERDEAL_INFLOW") or 0) - float(r.get("SUPERDEAL_OUTFLOW") or 0)
                + float(r.get("BIGDEAL_INFLOW") or 0) - float(r.get("BIGDEAL_OUTFLOW") or 0))
    return f"{str(r.get('TRADE_DATE'))[:10]} main_net={main_net/1e8:.2f}亿"


@record("eastmoney", "板块资金流 datacenter INDUSTRY")
def em_industry_datacenter():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_INDUSTRY_FUNDFLOW", "columns": "ALL",
                "pageSize": "10", "filter": "(TRADE_DATE='2026-10-09')"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("empty")
    return f"{len(data)} boards, e.g. {data[0].get('BOARD_NAME')}"


@record("eastmoney", "分时 trends2(curl h2)")
def em_trends2():
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
        "secid=1.600519&ndays=1&is_kc=0&fields1=f1,f2,f3,f4,f5&fields2=f51,f53,f56,f58"
    )
    payload = curl_get_json(url, referer="https://quote.eastmoney.com/")
    rows = (payload.get("data") or {}).get("trends") or []
    if not rows:
        raise RuntimeError("empty trends")
    return f"{len(rows)} minute points"


@record("eastmoney", "两融 datacenter RPTA_RZRQ_LSHJ")
def em_margin():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPTA_RZRQ_LSHJ", "columns": "ALL",
                "sortColumns": "DIM_DATE", "sortTypes": "-1",
                "pageSize": "2", "pageNumber": "1"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("empty margin data")
    latest = data[0]
    return f"date={str(latest.get('DIM_DATE'))[:10]} rzye={float(latest.get('RZYE') or 0)/1e8:.0f}亿"


@record("eastmoney", "解禁日历 RPT_LIFT_STAGE")
def em_unlock():
    today = datetime.now(CN_TZ).date()
    end = (today + timedelta(days=30)).isoformat()
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_LIFT_STAGE", "columns": "ALL",
                "filter": f"(FREE_DATE>='{today}')(FREE_DATE<='{end}')",
                "pageNumber": "1", "pageSize": "10",
                "sortColumns": "FREE_DATE", "sortTypes": "1",
                "source": "WEB", "client": "WEB"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    return f"{len(data)} upcoming unlocks in 30d"


@record("eastmoney", "CPI RPT_ECONOMY_CPI")
def em_cpi():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_ECONOMY_CPI", "columns": "ALL",
                "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                "pageSize": "2", "pageNumber": "1"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("empty CPI")
    return f"period={data[0].get('TIME')} yoy={data[0].get('NATIONAL_SAME')}%"


@record("eastmoney", "业绩预告 RPT_PUBLIC_OP_NEWPREDICT")
def em_predict():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
                "filter": '(SECURITY_CODE+in+("600519"))',
                "pageNumber": "1", "pageSize": "10",
                "sortColumns": "NOTICE_DATE", "sortTypes": "-1",
                "source": "WEB", "client": "WEB"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    return f"{len(data)} predict rows for 600519"


@record("eastmoney", "业绩报表 RPT_LICO_FN_CPD")
def em_lico():
    res = SESSION.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={"reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
                "filter": '(SECURITY_CODE+in+("600519"))',
                "pageNumber": "1", "pageSize": "10",
                "sortColumns": "REPORTDATE", "sortTypes": "-1",
                "source": "WEB", "client": "WEB"},
        headers={"Referer": "https://data.eastmoney.com/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    data = ((res.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("empty reports")
    return f"latest report {str(data[0].get('REPORTDATE'))[:10]}"


@record("eastmoney", "个股新闻 search-api jsonp")
def em_search_news():
    inner = json.dumps({
        "uid": "", "keyword": "600519", "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                   "pageIndex": 1, "pageSize": 5, "preTag": "", "postTag": ""}},
    }, separators=(",", ":"))
    res = SESSION.get(
        "https://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": "jQuery_probe", "param": inner},
        headers={"Referer": "https://so.eastmoney.com/"}, timeout=15,
    )
    res.raise_for_status()
    payload = json.loads(res.text[res.text.index("(") + 1: res.text.rindex(")")])
    articles = (payload.get("result") or {}).get("cmsArticleWebOld") or []
    return f"{len(articles)} articles"


@record("eastmoney", "全球快讯 np-weblist getFastNewsList")
def em_fast_news():
    res = SESSION.get(
        "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
        params={"client": "web", "biz": "web_724", "fastColumn": "102",
                "sortEnd": "", "pageSize": "20", "req_trace": str(uuid.uuid4())},
        headers={"Referer": "https://kuaixun.eastmoney.com/"}, timeout=10,
    )
    res.raise_for_status()
    rows = ((res.json().get("data") or {}).get("fastNewsList")) or []
    if not rows:
        raise RuntimeError("empty fastNewsList")
    return f"{len(rows)} items, latest {rows[0].get('showTime')}"


# ── 资讯 ──────────────────────────────────────────────────────────────

@record("news", "财联社 v1/roll/get_roll_list")
def cls_telegraph():
    base = {"appName": "CailianpressWeb", "last_time": "", "os": "web",
            "refresh_type": "1", "rn": "20", "sv": "7.7.5"}
    query = "&".join(f"{k}={base[k]}" for k in sorted(base))
    base["sign"] = hashlib.md5(hashlib.sha1(query.encode()).hexdigest().encode()).hexdigest()
    res = SESSION.get(
        "https://www.cls.cn/v1/roll/get_roll_list", params=base,
        headers={"Referer": "https://www.cls.cn/"}, timeout=10,
    )
    res.raise_for_status()
    payload = res.json()
    errno = payload.get("errno")
    if errno != 0:
        raise RuntimeError(f"errno={errno}")
    roll = (payload.get("data") or {}).get("roll_data") or []
    if not roll:
        raise RuntimeError("empty roll_data")
    return f"{len(roll)} telegraphs"


@record("news", "华尔街见闻 lives")
def wallstreetcn():
    res = SESSION.get(
        "https://api-one-wscn.awtmt.com/apiv1/content/lives",
        params={"channel": "global-channel", "limit": "20"},
        headers={"Referer": "https://wallstreetcn.com/"}, timeout=10,
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("code") != 20000:
        raise RuntimeError(f"code={payload.get('code')}")
    items = (payload.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("empty items")
    return f"{len(items)} lives"


@record("news", "Google News RSS")
def google_news():
    res = SESSION.get(
        "https://news.google.com/rss/search",
        params={"q": "global stock markets", "hl": "en-US", "gl": "US", "ceid": "US:en"},
        headers={"Accept": "application/rss+xml, application/xml"}, timeout=10,
    )
    res.raise_for_status()
    items = re.findall(r"<item>", res.text)
    if not items:
        raise RuntimeError("no rss items")
    return f"{len(items)} items"


@record("news", "MarketWatch RSS(Google备源)")
def marketwatch_rss():
    res = SESSION.get(
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        headers={"Accept": "application/rss+xml, application/xml"}, timeout=10,
    )
    res.raise_for_status()
    items = re.findall(r"<item>", res.text)
    if not items:
        raise RuntimeError("no rss items")
    return f"{len(items)} items"


@record("news", "金十数据快讯 flash-api")
def jin10_flash():
    # 新增备援源（2026-10-20 实测）：该接口校验 x-app-id / x-version 请求头，缺一会直接被拒。
    res = SESSION.get(
        "https://flash-api.jin10.com/get_flash_list",
        params={"channel": "-8200", "vip": "1"},
        headers={
            "User-Agent": UA,
            "Referer": "https://www.jin10.com/",
            "x-app-id": "bVBF4FyRTn5NJF5n",
            "x-version": "1.0.0",
        },
        timeout=10,
    )
    res.raise_for_status()
    payload = res.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"jin10 rejected: status={payload.get('status')}")
    items = payload.get("data") or []
    if not items:
        raise RuntimeError("no flash items")
    return f"{len(items)} items, latest {items[0].get('time')}"


@record("news", "同花顺快讯 tapp/news/push")
def ths_news():
    res = SESSION.get(
        "https://news.10jqka.com.cn/tapp/news/push/stock/",
        params={"page": "1", "tag": "", "track": "website", "pagesize": "20"},
        headers={"User-Agent": UA, "Referer": "https://news.10jqka.com.cn/"}, timeout=10,
    )
    res.raise_for_status()
    payload = res.json()
    if str(payload.get("code")) != "200":
        raise RuntimeError(f"ths rejected: code={payload.get('code')}")
    items = ((payload.get("data") or {}).get("list")) or []
    if not items:
        raise RuntimeError("no news items")
    return f"{len(items)} items, latest {str(items[0].get('title'))[:28]}"


@record("official", "上交所个股快照 query.sse.com.cn")
def sse_snapshot():
    # 交易所官方一手源：行情侧的权威备援，不受财经门户 WAF 影响（2026-10-20 实测）。
    res = SESSION.get(
        "http://query.sse.com.cn/commonQuery.do",
        params={
            "jsonCallBack": "jsonpCallback1",
            "isPagination": "true",
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
            "SEC_CODE": "600519",
            "COMPANY_STATUS": "2",
            "type": "inParams",
            "pageHelp.pageSize": "5",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
        },
        headers={"User-Agent": UA, "Referer": "http://www.sse.com.cn/"}, timeout=(2.5, 8),
    )
    res.raise_for_status()
    body = res.text
    if "result" not in body:
        raise RuntimeError("unexpected sse payload")
    return f"payload {len(body)} bytes"


# ── 官方/机构 ─────────────────────────────────────────────────────────

@record("official", "巨潮 orgId映射 szse_stock.json")
def cninfo_orgid():
    res = SESSION.get(
        "https://www.cninfo.com.cn/new/data/szse_stock.json",
        headers={"User-Agent": UA}, timeout=15,
    )
    res.raise_for_status()
    stocks = res.json().get("stockList") or []
    if not stocks:
        raise RuntimeError("empty stockList")
    org = next((s["orgId"] for s in stocks if s.get("code") == "600519"), "")
    return f"{len(stocks)} stocks, 600519→{org or '?'}"


@record("official", "巨潮公告 hisAnnouncement/query")
def cninfo_announcements():
    org_res = SESSION.get(
        "https://www.cninfo.com.cn/new/data/szse_stock.json",
        headers={"User-Agent": UA}, timeout=15,
    )
    stocks = org_res.json().get("stockList") or []
    org = next((s["orgId"] for s in stocks if s.get("code") == "600519"), "gssh0600519")
    res = SESSION.post(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        data={"stock": f"600519,{org}", "tabName": "fulltext", "pageSize": "10",
              "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
              "searchkey": "", "secid": "", "sortName": "", "sortType": "",
              "isHLtitle": "true"},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://www.cninfo.com.cn/new/disclosure",
                 "Origin": "https://www.cninfo.com.cn"},
        timeout=15,
    )
    res.raise_for_status()
    anns = res.json().get("announcements") or []
    if not anns:
        raise RuntimeError("no announcements")
    return f"{len(anns)} announcements for 600519"


@record("official", "Naver fchart KOSPI日K")
def naver_fchart():
    res = SESSION.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": "KOSPI", "timeframe": "day", "count": "5", "requestType": "0"},
        headers={"Referer": "https://finance.naver.com/"}, timeout=(2.5, 6),
    )
    res.raise_for_status()
    text = res.content.decode("euc-kr", "replace")
    rows = re.findall(r'<item data="([^"]+)"', text)
    if not rows:
        raise RuntimeError("no bars parsed")
    parts = rows[-1].split("|")
    return f"5 bars, latest {parts[0]} close={parts[4]}"


@record("official", "Naver siseJson 个股行情")
def naver_sise_json():
    res = SESSION.get(
        "https://api.finance.naver.com/siseJson.naver",
        params={"symbol": "000660", "requestType": "1", "startTime": "20260928",
                "endTime": "20261010", "timeframe": "day"},
        headers={"Referer": "https://finance.naver.com/"}, timeout=(2.5, 6),
    )
    res.raise_for_status()
    text = res.content.decode("euc-kr", "replace").replace("'", '"')
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("[")]
    if len(rows) < 2:
        raise RuntimeError("no data rows")
    return f"{len(rows) - 1} daily rows for SK Hynix"


@record("official", "美财政部收益率 XML")
def treasury_yields():
    year = datetime.now(CN_TZ).year
    res = SESSION.get(
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
        params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)},
        timeout=20,
    )
    res.raise_for_status()
    # Atom feed：数据行在 <d:NEW_DATE>（d: 命名空间，标签带 m:type 属性）
    entries = res.text.count("<d:NEW_DATE")
    if not entries:
        raise RuntimeError("no yield entries")
    return f"{entries} daily entries in {year}"


@record("official", "美财政部收益率 CSV(备援)")
def treasury_yields_csv():
    # 官方 CSV 端点（指南 1.3b）：当日可取、全曲线一次返回，优于 XML/FRED。
    year = datetime.now(CN_TZ).year
    res = SESSION.get(
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv",
        headers={"Referer": "https://home.treasury.gov/"},
        timeout=20,
    )
    res.raise_for_status()
    import csv as _csv
    import io as _io
    rows = list(_csv.DictReader(_io.StringIO(res.text.lstrip("﻿"))))
    if not rows or "10 Yr" not in rows[0]:
        raise RuntimeError("no 10 Yr column")
    return f"{len(rows)} rows, latest {rows[0].get('Date')} 10Y={rows[0].get('10 Yr')}"


@record("official", "美联储 FOMC 日历页")
def fed_fomc():
    res = SESSION.get(
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        timeout=20,
    )
    res.raise_for_status()
    if "FOMC" not in res.text:
        raise RuntimeError("page content unexpected")
    return f"page {len(res.text)//1024}KB fetched"


@record("official", "国家统计局 PMI发布榜")
def stats_pmi():
    # 页面按发布批次滚动，PMI 文章不一定当日在榜；只验证发布榜可达且非 WAF 页。
    res = SESSION.get(
        "https://www.stats.gov.cn/sj/zxfb/",
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=20,
    )
    res.raise_for_status()
    # 页面为 UTF-8 但响应头不带 charset，requests 会误判为 ISO-8859-1
    res.encoding = "utf-8"
    text = res.text
    if "数据发布" not in text:
        raise RuntimeError("unexpected page (WAF?)")
    return f"page {len(text)//1024}KB, 数据发布 index ok"


@record("official", "人民银行社融目录页")
def pbc_index():
    res = SESSION.get(
        "https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html",
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=20,
    )
    res.raise_for_status()
    text = res.text
    # 目录页为 UTF-8 XHTML；条目按 JS 渲染，仅验证返回正常页面而非反爬壳。
    if len(text) < 5000 or "<html" not in text.lower():
        raise RuntimeError(f"unexpected page ({len(text)} bytes)")
    return f"page {len(text)//1024}KB ok"


def hithink_cli():
    """同花顺官方 CLI：检查安装与密钥（不强制跑行情，避免 keyring 交互）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cli = root / "node_modules" / ".bin" / "hithink-finance"
    node = root / "data" / "runtime" / "node" / "bin" / "node"
    entry = root / "node_modules" / "@hithink-tech" / "hithink-finance-cli" / "dist" / "cli" / "main.js"
    if not cli.is_file() and not entry.is_file():
        raise RuntimeError("hithink-finance CLI 未安装（pnpm install 缺失）")
    cmd = ([str(node), str(entry)] if node.is_file() and entry.is_file() else [str(cli)])
    cmd += ["doctor"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25, cwd=str(root))
    out = (proc.stdout + proc.stderr).strip()
    ok_words = ("ok", "pass", "healthy", "成功")
    if proc.returncode != 0:
        raise RuntimeError(f"doctor exit={proc.returncode}: {out[:200]}")
    return (out[:120] or "doctor ok") + ("…" if len(out) > 120 else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="A股看板数据源健康检查")
    parser.add_argument("--only", help="逗号分组过滤: tencent,sina,eastmoney,news,official")
    parser.add_argument("--json", dest="json_path", help="结果写入 JSON 文件")
    args = parser.parse_args()

    only = {s.strip() for s in args.only.split(",")} if args.only else None

    tests = [
        ("tencent", tencent_quote), ("tencent", tencent_newfqkline_web),
        ("tencent", tencent_newfqkline_ifzq), ("tencent", tencent_newfqkline_proxy),
        ("tencent", tencent_usfqkline),
        ("tencent", tencent_mkline), ("tencent", tencent_minute),
        ("tencent", tencent_minute_proxy),
        ("tencent", tencent_breadth), ("tencent", tencent_newfqkline),
        ("sina", sina_hq), ("sina", sina_cn_quotes),
        ("sina", sina_futures_daily), ("sina", sina_zhibo),
        ("sina", sina_kline_crosscheck), ("sina", sina_finance_report),
        ("sina", sina_money_flow),
        ("eastmoney", em_kline_daily), ("eastmoney", em_kline_minute),
        ("eastmoney", em_fund_flow), ("eastmoney", em_fund_flow_push2his),
        ("eastmoney", em_industry_flow), ("eastmoney", em_industry_flow_push2his),
        ("eastmoney", em_stocknew_flow), ("eastmoney", em_industry_datacenter),
        ("eastmoney", em_trends2), ("eastmoney", em_bond_yield),
        ("eastmoney", em_margin),
        ("eastmoney", em_unlock), ("eastmoney", em_cpi),
        ("eastmoney", em_predict), ("eastmoney", em_lico),
        ("eastmoney", em_announcements), ("eastmoney", em_finance_main),
        ("eastmoney", em_pmi),
        ("eastmoney", em_search_news), ("eastmoney", em_fast_news),
        ("news", cls_telegraph), ("news", wallstreetcn), ("news", google_news),
        ("news", marketwatch_rss), ("news", jin10_flash), ("news", ths_news),
        ("official", cninfo_orgid), ("official", cninfo_announcements),
        ("official", sse_snapshot),
        ("official", naver_fchart), ("official", naver_sise_json),
        ("official", treasury_yields), ("official", treasury_yields_csv),
        ("official", fed_fomc),
        ("official", stats_pmi), ("official", pbc_index),
    ]
    for group, fn in tests:
        if only and group not in only:
            continue
        fn()
        # 东财系节流，其余温和间隔
        sleep_pace(1.3 if group == "eastmoney" else 0.6)

    # 同花顺 CLI 单独处理（本地进程，无网络礼貌问题）
    if not only or "hithink" in only or "official" in only:
        res = Result("官方/机构", "同花顺CLI doctor")
        start = time.monotonic()
        try:
            res.detail = hithink_cli()
            res.ok = True
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"[:300]
        res.latency_ms = int((time.monotonic() - start) * 1000)
        RESULTS.append(res)
        print(f"[{'PASS' if res.ok else 'FAIL'}] {res.group} {res.name}  {res.latency_ms}ms  {res.detail or res.error}")

    failed = [r for r in RESULTS if not r.ok]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        print("失败项：")
        for r in failed:
            hint = ""
            if r.group == "东方财富" and ("Empty reply" in r.error or "HTTP=000" in r.error
                                          or "CalledProcessError" in r.error):
                hint = "  ← 已知 push2 系 IP+主机级临时封禁（连接被掐断），数小时至一天自愈；datacenter 不受影响"
            elif r.group == "资讯" and "news.google.com" in r.error:
                hint = "  ← 本网络不可达 news.google.com（连接超时），MarketWatch 备源已兜底"
            print(f"  - {r.group}/{r.name}: {r.error}{hint}")

    if args.json_path:
        payload = {
            "checked_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "results": [r.as_dict() for r in RESULTS],
        }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"JSON → {args.json_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
