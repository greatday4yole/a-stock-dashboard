"""资金面与事件日历采集：个股主力资金流、两融、行业资金流、解禁日历、宏观快照。

传输纪律
--------
* push2.eastmoney.com 系（资金流）只接受 HTTP/2 → 走系统 curl 子进程
  （与 dashboard_server._curl_get_json 同一约定）。
* 东财 WAF 按 IP+主机 封禁，且端点会"返回 200 但数据为空"这种软失效，因此
  fflow / clist 都配了多主机链（PUSH2_FFLOW_HOSTS / PUSH2_CLIST_HOSTS），
  逐台轮换直到取到非空结果，全部失败才抛错。
* datacenter-web.eastmoney.com 支持 HTTP/1.1 → requests 直连。
* 调用方必须自己做节流（EM 按 IP+主机 封禁突发），本模块只提供单次采集。
* 所有采集失败都抛异常，由调用方决定降级（资金因子置 0、提示不可用）。

数据口径
--------
* 个股主力资金流：fflow/daykline，kline 行 "date,主力,小单,中单,大单,超大单,
  主力占比%,小单占比%,中单占比%,大单占比%,超大单占比%,收盘,涨跌幅%,…"。
  主力净占比（%）由上游按成交额归一，直接可用。
* 两融：RPTA_RZRQ_LSHJ，RZJME=融资净买入（元），RZYE=融资余额。
* 解禁：RPT_LIFT_STAGE，FREE_DATE/FREE_RATIO(占总股本)/FREE_SHARES_TYPE。
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import requests

CN_TZ = timezone(timedelta(hours=8))

PUSH2_KLINE = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 个股资金流日K 的主机链（2026-10-20 实测）：
# push2 主机的 fflow 端点会"返回 200 但 klines 为空"（等价失效，且与 501/断连
# 表现不同，容易被误判为正常空结果）；而 push2his 网关走同一路径可正常返回
# 历史——实测 2026-10-19 主力净额 314,089,632 元，与 datacenter RPT_DMSK_TS_STOCKNEW
# 的 PRIME_INFLOW 完全一致，口径无差异。故把 push2his 及其编号网关纳入轮换，
# 主机级封禁或端点返回空时自动切换（口径相同，无需任何换算）。
PUSH2_FFLOW_HOSTS = (
    PUSH2_KLINE,
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://82.push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://23.push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
)
PUSH2_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
# 行业板块排名的 clist 主机链（2026-10-20 实测 82.push2his 可返回完整 496 板块）：
# clist 与 fflow 是不同端点，WAF 封禁粒度到"主机+端点"，两条链的可用主机不重合，
# 因此 fflow 的 push2his 备机同样要单独给 clist 配一组。
PUSH2_CLIST_HOSTS = (
    PUSH2_CLIST,
    "https://push2his.eastmoney.com/api/qt/clist/get",
    "https://82.push2his.eastmoney.com/api/qt/clist/get",
    "https://23.push2his.eastmoney.com/api/qt/clist/get",
)
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UNLOCK_REPORT = "RPT_LIFT_STAGE"
CPI_REPORT = "RPT_ECONOMY_CPI"
PREDICT_REPORT = "RPT_PUBLIC_OP_NEWPREDICT"
LICO_REPORT = "RPT_LICO_FN_CPD"
# push2 fflow/clist 被封禁期间的 datacenter 备源报表（2026-10-11 实测可用；
# 来源：stock_all_in_1 项目共享指南）。STOCKNEW 只有最近 1 个交易日；
# INDUSTRY_FUNDFLOW 支持历史日期但无法服务端按净额排序，需拉全量本地排序。
STOCKNEW_REPORT = "RPT_DMSK_TS_STOCKNEW"
INDUSTRY_FLOW_REPORT = "RPT_INDUSTRY_FUNDFLOW"
FFLOW_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CURL_TIMEOUT = 10.0


def _curl_json(url: str, *, referer: str, timeout: float = CURL_TIMEOUT) -> dict[str, Any]:
    """EM push2 系只接受 HTTP/2，requests 栈无法协商，走系统 curl。"""
    command = [
        "/usr/bin/curl",
        "--silent", "--show-error", "--fail", "--compressed",
        "--max-time", str(int(timeout)),
        "-H", f"User-Agent: {USER_AGENT}",
        "-H", f"Referer: {referer}",
        url,
    ]
    result = subprocess.run(command, capture_output=True, check=True, timeout=timeout + 4.0)
    payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("EM response is not an object")
    return payload


def _em_secid(code: str) -> str:
    """六位代码 → 东财 secid（沪 1 / 深 0）。"""
    if code.startswith(("6", "5", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None  # NaN 过滤


def fetch_stock_fund_flow_daily(code: str, *, lmt: int = 0) -> dict[str, Any]:
    """个股主力资金流日K历史。

    返回 {"ok": True, "rows": [{"date", "main_pct", "main_net", "close", "pct_change"}]}，
    rows 按日期升序。main_pct = 主力净流入占成交额百分比（上游归一）。

    主机链轮换：东财 WAF 是 IP+主机级封禁，且 fflow 端点在 push2 上会返回
    "200 但 klines 为空"这种软失效——空结果同样触发换主机，全部主机都空才抛错。
    """
    secid = _em_secid(code)
    query = (
        f"lmt={int(lmt)}&klt=101&secid={secid}"
        f"&fields1=f1,f2,f3,f7&fields2={FFLOW_FIELDS}"
    )
    last_error: Exception | None = None
    for host in PUSH2_FFLOW_HOSTS:
        try:
            payload = _curl_json(
                f"{host}?{query}", referer="https://quote.eastmoney.com/", timeout=6.0
            )
            body = payload.get("data") or {}
            rows: list[dict[str, Any]] = []
            for raw in body.get("klines") or []:
                parts = str(raw).split(",")
                if len(parts) < 7:
                    continue
                main_pct = _float(parts[6]) if len(parts) > 6 else None
                main_net = _float(parts[1])
                if main_pct is None:
                    continue
                rows.append({
                    "date": parts[0],
                    "main_pct": main_pct,
                    "main_net": main_net,
                    "close": _float(parts[11]) if len(parts) > 11 else None,
                    "pct_change": _float(parts[12]) if len(parts) > 12 else None,
                })
            if not rows:
                # 软失效（200 但无 klines）：换下一台主机，不写负面缓存。
                last_error = RuntimeError(f"EM fflow daykline empty on {host}")
                continue
            return {"ok": True, "code": code, "rows": rows}
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"EM fflow daykline unavailable for {code}: {last_error}")


def fetch_margin_summary() -> dict[str, Any]:
    """沪深两融最新摘要（融资净买入/余额，含 5 日累计）。"""
    query = (
        "reportName=RPTA_RZRQ_LSHJ&columns=ALL"
        "&sortColumns=DIM_DATE&sortTypes=-1&pageSize=3&pageNumber=1"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    payload = response.json()
    data = ((payload.get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("EM margin report empty")
    latest = data[0]
    return {
        "ok": True,
        "date": str(latest.get("DIM_DATE") or "")[:10],
        "rzye_yi": round(float(latest.get("RZYE") or 0) / 1e8, 1),
        "rzjme_yi": round(float(latest.get("RZJME") or 0) / 1e8, 2),
        "rzjme_5d_yi": round(float(latest.get("RZJME5D") or 0) / 1e8, 1),
        # 融券端：RQYE=融券余额 RQMRE=融券卖出量 RQFSE=融券余量；
        # 融资端：RZMRE=融资买入额 RZCHE=融资偿还额。columns=ALL 已拉回全部。
        "rqye_yi": round(float(latest.get("RQYE") or 0) / 1e8, 1),
        "rqmre_yi": round(float(latest.get("RQMRE") or 0) / 1e8, 2),
        "rqfse_wan": round(float(latest.get("RQFSE") or 0) / 1e4, 1),
        "rzmre_yi": round(float(latest.get("RZMRE") or 0) / 1e8, 1),
        "rzche_yi": round(float(latest.get("RZCHE") or 0) / 1e8, 1),
    }


# ---- 先行指标扩展：北向资金 / 涨跌停 / 货币市场利率 ---------------------------
# 各函数独立 try/except（由调用方包），symbol/endpoint 不可验证时静默返回
# available=False，不影响先行指标区其他因子展示。


def fetch_northbound_flow() -> dict[str, Any]:
    """北向资金（沪深港通）当日净流入：东财 datacenter 沪深港通成交报告。

    取最新一日的沪股通+深股通成交净额（DEAL_AMT，万元）合计。北向数据自
    2024-08 起改为按日披露，日内最新为前一交易日数据；用作盘后/盘前先行参考。
    """
    query = (
        "reportName=RPT_MUTUAL_DEAL_HISTORY&columns=TRADE_DATE,MUTUAL_TYPE,DEAL_AMT"
        "&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=10&pageNumber=1"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    payload = response.json()
    data = ((payload.get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("EM northbound report empty")
    # 最新交易日：同一 TRADE_DATE 下有多行 MUTUAL_TYPE（沪股通/深股通等），
    # 把该日所有行的 DEAL_AMT（成交净额，万元）求和作为北向合计净流入。
    latest_date = str(data[0].get("TRADE_DATE") or "")[:10]
    net_wan = 0.0
    for row in data:
        if str(row.get("TRADE_DATE") or "")[:10] != latest_date:
            break
        amt = _float(row.get("DEAL_AMT"))
        if amt is not None:
            net_wan += amt
    return {
        "ok": True,
        "date": latest_date,
        "net_yi": round(net_wan / 1e4, 2),
        "source": "东财沪深港通",
    }


def fetch_limit_pool_counts() -> dict[str, Any]:
    """涨跌停数量与封板率：东财涨停板/跌停板池接口取 tc 总数。

    封板率 = 涨停数 / (涨停数 + 跌停数)，反映市场赚钱效应强度。

    旧实现走 datacenter 的 ``RPT_ZTSTC_DETAIL`` / ``RPT_DTSTC_DETAIL`` 报表，
    但这两个报表名在东财侧已不存在（实测返回"报表配置不存在"），count 恒为
    0，封板率因此永远是 null。改用东财行情中心的涨停/跌停池接口
    （push2ex 域），其 ``data.tc`` 即当日涨停/跌停总数。
    """

    def _pool_total(kind: str) -> int:
        # 东财涨停/跌停池接口要求 date 与 sort 同时给出，缺任一个都返回
        # rc=102（实测：仅 date → 102，仅 sort → 102，两者齐备 → 0）。
        today = datetime.now(CN_TZ).strftime("%Y%m%d")
        url = (
            f"https://push2ex.eastmoney.com/getTopic{kind}Pool"
            "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
            f"&Pageindex=0&pagesize=1&date={today}&sort=fbt%3Aasc"
        )
        r = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://quote.eastmoney.com/ztb/detail",
            },
            timeout=(2.5, 6.0),
        )
        r.raise_for_status()
        p = r.json()
        if p.get("rc") not in (0, "0"):
            raise RuntimeError(f"东财{kind}池返回异常: rc={p.get('rc')}")
        data = p.get("data") or {}
        total = data.get("tc")
        if total is None:
            raise RuntimeError(f"东财{kind}池缺少 tc 字段")
        return int(total)

    zt = _pool_total("ZT")
    dt = _pool_total("DT")
    denom = zt + dt
    seal_rate = round(zt / denom * 100, 1) if denom > 0 else None
    return {
        "ok": True,
        "limit_up": zt,
        "limit_down": dt,
        "seal_rate": seal_rate,
        "source": "东财涨停板/跌停板池",
    }


def fetch_money_market_rates() -> dict[str, Any]:
    """货币市场利率：SHIBOR 隔夜/1周/1年 + FR007（银行间回购定盘利率）。

    数据源为中国货币网（全国银行间同业拆借中心官方发布）：

    - ``/r/cms/www/chinamoney/data/shibor/shibor.json`` —— SHIBOR 各期限报价
    - ``/r/cms/www/chinamoney/data/currency/frr.json`` —— 回购定盘利率
      （FR001/FR007/FR014），FR007 是 DR007 的定盘基准

    旧实现走东财 push2 globalbd 的 ``171.SHIBORON`` / ``171.DR007``，
    这些 secid 在东财侧并不存在（实测返回空），因此货币市场指标长期
    显示"暂不可用"。中国货币网是这些利率的权威发布方，直接取其静态
    JSON 更可靠。
    """

    base = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.chinamoney.com.cn/chinese/bkshibor/",
    }
    rates: dict[str, Any] = {}

    def _records(path: str) -> list[dict[str, Any]]:
        r = requests.get(f"{base}/{path}", headers=headers, timeout=(2.5, 6.0))
        r.raise_for_status()
        p = r.json()
        records = p.get("records")
        return records if isinstance(records, list) else []

    shibor_terms = {
        "O/N": ("shibor_on", "SHIBOR 隔夜"),
        "1W": ("shibor_1w", "SHIBOR 1周"),
        "1Y": ("shibor_1y", "SHIBOR 1年"),
    }
    try:
        for row in _records("shibor/shibor.json"):
            term = str(row.get("termCode") or "")
            if term not in shibor_terms:
                continue
            value = _float(row.get("shibor"))
            if value is None:
                continue
            key, name = shibor_terms[term]
            rates[key] = {"name": name, "value": round(value, 4)}
    except Exception as exc:
        raise RuntimeError(f"SHIBOR 不可用: {type(exc).__name__}: {exc}") from exc

    try:
        for row in _records("currency/frr.json"):
            code = str(row.get("productCode") or "")
            if code != "FR007":
                continue
            value = _float(row.get("value"))
            if value is None:
                continue
            rates["dr007"] = {"name": "FR007 回购定盘", "value": round(value, 4)}
    except Exception as exc:
        # 回购定盘利率缺失不影响 SHIBOR 展示。
        pass

    if not rates:
        raise RuntimeError("EM money market rates empty")
    return {
        "ok": True,
        "rates": rates,
        "source": "中国货币网（银行间同业拆借中心）",
    }


def fetch_industry_flow_rank(*, top: int = 10) -> dict[str, Any]:
    """行业板块当日主力净流入排名（东财 496 个行业/概念板块）。"""
    query = (
        "pn=1&pz=500&po=1&fid=f62&fs=m:90+t:2"
        "&fields=f12,f14,f62,f184"
    )
    payload: dict[str, Any] | None = None
    last_error: Exception | None = None
    for host in PUSH2_CLIST_HOSTS:
        try:
            candidate = _curl_json(f"{host}?{query}", referer="https://quote.eastmoney.com/", timeout=6.0)
            diff = ((candidate.get("data") or {}).get("diff")) or {}
            if diff:
                payload = candidate
                break
            last_error = RuntimeError(f"EM clist empty on {host}")
        except Exception as exc:
            last_error = exc
            continue
    if payload is None:
        raise RuntimeError(f"EM clist industry flow unavailable: {last_error}")
    diff = ((payload.get("data") or {}).get("diff")) or {}
    rows: list[dict[str, Any]] = []
    for node in diff.values():
        main_net = _float(node.get("f62"))
        name = str(node.get("f14") or "")
        if main_net is None or not name:
            continue
        rows.append({
            "code": str(node.get("f12") or ""),
            "name": name,
            "main_net_yi": round(main_net / 1e8, 2),
            "main_pct": _float(node.get("f184")),
        })
    rows.sort(key=lambda row: row["main_net_yi"], reverse=True)
    if not rows:
        raise RuntimeError("EM industry flow empty")
    return {
        "ok": True,
        "top_inflow": rows[:top],
        "top_outflow": list(reversed(rows[-top:])),
    }


def fetch_unlock_calendar(
    start_date: str,
    end_date: str,
    codes: Sequence[str] | None = None,
    *,
    page_size: int = 200,
) -> dict[str, Any]:
    """解禁日历：按 FREE_DATE 区间拉取，可选过滤到持仓代码。

    返回 {"ok": True, "all_count": n, "rows": [...]（按日期升序，
    已按 codes 过滤或全量前 page_size 条）}。LIFT_MARKET_CAP/
    FREE_SHARES 的单位以 EM 原样透传（未严格校验），展示时标注"东财口径"。
    """
    query = (
        f"reportName={UNLOCK_REPORT}&columns=ALL"
        f"&filter=(FREE_DATE%3E%3D%27{start_date}%27)(FREE_DATE%3C%3D%27{end_date}%27)"
        f"&pageNumber=1&pageSize={int(page_size)}&sortColumns=FREE_DATE&sortTypes=1"
        "&source=WEB&client=WEB"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    payload = response.json()
    data = ((payload.get("result") or {}).get("data")) or []
    rows: list[dict[str, Any]] = []
    for item in data:
        row = {
            "code": str(item.get("SECURITY_CODE") or ""),
            "name": str(item.get("SECURITY_NAME_ABBR") or ""),
            "date": str(item.get("FREE_DATE") or "")[:10],
            "type": str(item.get("FREE_SHARES_TYPE") or ""),
            "free_ratio_pct": round(float(item.get("FREE_RATIO") or 0) * 100.0, 3),
            "market_cap_yi": _float(item.get("LIFT_MARKET_CAP")),
        }
        rows.append(row)
    if codes is not None:
        wanted = {str(code).strip() for code in codes}
        rows = [row for row in rows if row["code"] in wanted]
    rows.sort(key=lambda row: (row["date"], row["code"]))
    return {"ok": True, "all_count": len(data), "rows": rows}


def _datacenter_in_filter(column: str, codes: Sequence[str]) -> str:
    quoted = ",".join(f'"{str(code).strip()}"' for code in codes)
    return f"({column}+in+({quoted}))"


def fetch_earnings_predict(codes: Sequence[str], *, lookback_days: int = 400) -> dict[str, Any]:
    """最新业绩预告 per 代码（超过 lookback 的旧预告视为过期不返回）。"""
    from datetime import date as _date, timedelta as _timedelta

    query = (
        f"reportName={PREDICT_REPORT}&columns=ALL"
        f"&filter={_datacenter_in_filter('SECURITY_CODE', codes)}"
        "&pageNumber=1&pageSize=300&sortColumns=NOTICE_DATE&sortTypes=-1"
        "&source=WEB&client=WEB"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    data = ((response.json().get("result") or {}).get("data")) or []
    cutoff = (_date.today() - _timedelta(days=lookback_days)).isoformat()
    result: dict[str, Any] = {}
    for item in data:
        code = str(item.get("SECURITY_CODE") or "")
        notice = str(item.get("NOTICE_DATE") or "")[:10]
        if not code or code in result or not notice or notice < cutoff:
            continue
        result[code] = {
            "notice_date": notice,
            "report_date": str(item.get("REPORT_DATE") or "")[:10],
            "finance": str(item.get("PREDICT_FINANCE") or ""),
            "add_amp_lower": _float(item.get("ADD_AMP_LOWER")),
            "add_amp_upper": _float(item.get("ADD_AMP_UPPER")),
            "content": str(item.get("PREDICT_CONTENT") or "")[:120],
        }
    return result


def fetch_earnings_reports(codes: Sequence[str]) -> dict[str, Any]:
    """最新已披露业绩报表 per 代码（报告期/净利润同比/营收同比）。"""
    query = (
        f"reportName={LICO_REPORT}&columns=ALL"
        f"&filter={_datacenter_in_filter('SECURITY_CODE', codes)}"
        "&pageNumber=1&pageSize=500&sortColumns=REPORTDATE&sortTypes=-1"
        "&source=WEB&client=WEB"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    data = ((response.json().get("result") or {}).get("data")) or []
    result: dict[str, Any] = {}
    for item in data:
        code = str(item.get("SECURITY_CODE") or "")
        if not code or code in result:
            continue
        result[code] = {
            "report_date": str(item.get("REPORTDATE") or "")[:10],
            "update_date": str(item.get("UPDATE_DATE") or "")[:10],
            "netprofit_yoy_pct": _float(item.get("SJLTZ")),
            "revenue_yoy_pct": _float(item.get("YSTZ")),
            "roe_pct": _float(item.get("WEIGHTAVG_ROE")),
        }
    return result


# (报告期月日, 窗口起, 窗口止, 标签) —— A 股披露期窗口（近似，精确预约日以交易所披露为准）
REPORT_WINDOWS: tuple[tuple[str, str, str, str], ...] = (
    ("09-30", "10-01", "10-31", "三季报"),
    ("03-31", "04-01", "04-30", "一季报"),
    ("06-30", "07-01", "08-31", "中报"),
    ("12-31", "01-01", "04-30", "年报"),
)


def next_report_window(latest_report_date: str, today: str) -> dict[str, Any] | None:
    """由最新已披露报告期推导下一期财报与披露窗口（近似）。"""
    if not latest_report_date or len(latest_report_date) < 10:
        return None
    today_iso = str(today)[:10]
    candidates = []
    year = int(today_iso[:4])
    for _ in range(2):  # 跨年扫描两年
        for period_mmdd, start_mmdd, end_mmdd, label in REPORT_WINDOWS:
            period = f"{year}-{period_mmdd}"
            window_start = f"{year}-{start_mmdd}"
            window_end = f"{year}-{end_mmdd}"
            if period > latest_report_date:
                candidates.append((period, window_start, window_end, label))
        year += 1
    if not candidates:
        return None
    period, window_start, window_end, label = min(candidates)
    in_window = window_start <= today_iso <= window_end
    return {
        "period_end": period,
        "label": label,
        "window_start": window_start,
        "window_end": window_end,
        "in_window": in_window,
    }


def fetch_cpi_snapshot() -> dict[str, Any]:
    """中国 CPI 最新值（宏观快照；PMI/社融可按同一模式扩展）。"""
    query = (
        f"reportName={CPI_REPORT}&columns=ALL"
        "&sortColumns=REPORT_DATE&sortTypes=-1&pageSize=2&pageNumber=1"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    data = ((response.json().get("result") or {}).get("data")) or []
    if not data:
        raise RuntimeError("EM CPI report empty")
    latest = data[0]
    return {
        "ok": True,
        "period": str(latest.get("TIME") or ""),
        "national_yoy_pct": _float(latest.get("NATIONAL_SAME")),
    }


def fetch_stock_fund_flow_today(codes: Sequence[str]) -> dict[str, dict[str, Any]]:
    """当日个股主力净流入（push2 fflow 封禁期的 datacenter 备源，批量 in() 一次请求）。

    返回 {code: {"date": "YYYY-MM-DD", "main_net_yi": float}}，无当日数据的代码
    不出现在结果里。口径（已实测验证）：
      主力净额 = (SUPERDEAL_INFLOW-SUPERDEAL_OUTFLOW) + (BIGDEAL_INFLOW-BIGDEAL_OUTFLOW)
               == PRIME_INFLOW 字段（两者相等）
    ⚠ 该报表只有最近 1 个交易日，不能用于历史归因；main_pct 需调用方用
      腾讯成交额（元）换算：main_pct = main_net / amount * 100。
    查无当日记录时 datacenter 返回 result:null + code 9201（正常空结果，非故障）。
    """

    if not codes:
        return {}
    query = (
        f"reportName={STOCKNEW_REPORT}&columns=ALL"
        f"&filter={_datacenter_in_filter('SECURITY_CODE', codes)}"
        "&pageNumber=1&pageSize=500&sortColumns=TRADE_DATE&sortTypes=-1"
        "&source=WEB&client=WEB"
    )
    response = requests.get(
        f"{DATACENTER}?{query}",
        headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    payload = response.json()
    data = ((payload.get("result") or {}).get("data")) or []
    result: dict[str, dict[str, Any]] = {}
    for item in data:
        code = str(item.get("SECURITY_CODE") or "")
        prime_inflow = _float(item.get("PRIME_INFLOW"))
        trade_date = str(item.get("TRADE_DATE") or "")[:10]
        if not code or prime_inflow is None or not trade_date:
            continue
        result[code] = {
            "date": trade_date,
            "main_net_yi": round(prime_inflow / 1e8, 3),
        }
    return result


def fetch_industry_flow_rank_datacenter(*, top: int = 10) -> dict[str, Any]:
    """行业板块主力净流入排名（push2 clist 封禁期的 datacenter 备源，支持历史日期）。

    与 fetch_industry_flow_rank 返回同构（top_inflow/top_outflow），source 标注
    "东财datacenter(备援)"。⚠ 该报表不支持服务端按 NET_INFLOW 排序，必须拉全量
    后本地排序，否则 top 榜失真。当日无数据（周末/盘前）时自动回看最近 3 天。
    """

    from datetime import date as _date, timedelta as _timedelta

    rows: list[dict[str, Any]] = []
    trade_date = ""
    for offset in range(3):
        day = (_date.today() - _timedelta(days=offset)).isoformat()
        query = (
            f"reportName={INDUSTRY_FLOW_REPORT}&columns=ALL"
            f"&filter=(TRADE_DATE%3D%27{day}%27)"
            "&pageNumber=1&pageSize=300"
            "&source=WEB&client=WEB"
        )
        response = requests.get(
            f"{DATACENTER}?{query}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://data.eastmoney.com/"},
            timeout=(2.5, 10.0),
        )
        response.raise_for_status()
        data = ((response.json().get("result") or {}).get("data")) or []
        if not data:
            time.sleep(1.2)  # datacenter 串行纪律
            continue
        for item in data:
            main_net = _float(item.get("NET_INFLOW"))
            name = str(item.get("BOARD_NAME") or "")
            if main_net is None or not name:
                continue
            rows.append({
                "code": str(item.get("BOARD_CODE") or ""),
                "name": name,
                "main_net_yi": round(main_net / 1e8, 2),
                "main_pct": None,  # datacenter 版无净占比字段
            })
        trade_date = day
        break
    if not rows:
        raise RuntimeError("EM datacenter industry flow empty（近 3 天均无数据）")
    rows.sort(key=lambda row: row["main_net_yi"], reverse=True)
    return {
        "ok": True,
        "source": f"东财datacenter(备援 {trade_date})",
        "trade_date": trade_date,
        "top_inflow": rows[:top],
        "top_outflow": list(reversed(rows[-top:])),
    }


def summarize_intel(
    *,
    fund_flow: Mapping[str, Any] | None,
    margin: Mapping[str, Any] | None,
    industry: Mapping[str, Any] | None,
    unlock: Mapping[str, Any] | None,
    macro: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """打包资金面/事件日历摘要（全部可缺省，调用方按 available 降级）。"""
    return {
        "fund_flow": fund_flow,
        "margin": margin,
        "industry": industry,
        "unlock": unlock,
        "macro": macro,
    }
