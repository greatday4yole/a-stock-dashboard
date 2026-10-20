#!/usr/bin/env python3
"""Local, read-only dashboard for the configured A-share portfolio.

The live route uses the Tencent quote mapping documented by a-stock-data
v3.7.1.  News is refreshed in the background and cached on disk so that the
Eastmoney search endpoint is never polled with the five-second quote loop.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import html
import ipaddress
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date as date_type, datetime, time as daytime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from chinese_calendar import is_holiday as _china_is_holiday
except ImportError:  # Keep the dashboard usable while surfacing a weekday fallback.
    _china_is_holiday = None

try:
    from scripts import market_news as market_news_module
    from scripts.data_retention import load_retention_policy, run_data_retention
    from scripts.daily_pnl_history import (
        DEFAULT_CACHE_PATH as DAILY_PNL_HISTORY_CACHE_PATH,
        DEFAULT_START_DATE as DAILY_PNL_HISTORY_DEFAULT_START_DATE,
        DailyPnlHistoryStore,
        DailyPnlHistoryUnavailable,
        last_fetch_chain_status as daily_pnl_last_fetch_status,
    )
    from scripts.hithink_quote_provider import HithinkQuoteProvider
    from scripts import intraday_pnl_backfill as intraday_backfill_module
    from scripts import agg_t_signal as agg_t_signal_module
    from scripts.intraday_pnl_timeseries import (
        IntradayPnlError,
        IntradayPnlStore,
        build_intraday_pnl_point,
    )
    from scripts.position_advice_store import (
        AdviceIntegrityError,
        load_advice_state,
        portfolio_fingerprint_sha256,
        public_position_advice,
        update_position_advice_store,
    )
    from scripts.portfolio_registry import (
        MAIN_PORTFOLIO_ID,
        MAIN_PORTFOLIO_LABEL,
        PortfolioNotFound,
        PortfolioRegistryError,
        build_portfolio_config,
        create_portfolio,
        delete_portfolio,
        flatten_positions,
        load_extra_portfolios,
        update_portfolio,
    )
    from scripts import technical_indicators as technical_indicators_module
    from scripts import technical_signal_engine as technical_signal_engine_module
    from scripts import school_signal_engine as school_signal_engine_module
    from scripts import auto_paper_trading as auto_paper_module
    from scripts import auto_pilot as auto_pilot_module
    from scripts import options_sentiment as options_sentiment_module
    from scripts import derivatives_regime as derivatives_regime_module
except ModuleNotFoundError:  # Direct execution: python scripts/dashboard_server.py
    # 直接执行时 sys.path[0] 是 scripts/，而 daily_pnl_history 等模块内部使用
    # `from scripts.xxx import …` 的包式导入；把仓库根目录注入 sys.path，
    # 否则服务重启会因 ModuleNotFoundError 起不来。
    _PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)
    import market_news as market_news_module  # type: ignore[no-redef]
    from data_retention import (  # type: ignore[no-redef]
        load_retention_policy,
        run_data_retention,
    )
    from daily_pnl_history import (  # type: ignore[no-redef]
        DEFAULT_CACHE_PATH as DAILY_PNL_HISTORY_CACHE_PATH,
        DEFAULT_START_DATE as DAILY_PNL_HISTORY_DEFAULT_START_DATE,
        DailyPnlHistoryStore,
        DailyPnlHistoryUnavailable,
        last_fetch_chain_status as daily_pnl_last_fetch_status,
    )
    from hithink_quote_provider import HithinkQuoteProvider  # type: ignore[no-redef]
    import intraday_pnl_backfill as intraday_backfill_module  # type: ignore[no-redef]
    import agg_t_signal as agg_t_signal_module  # type: ignore[no-redef]
    from intraday_pnl_timeseries import (  # type: ignore[no-redef]
        IntradayPnlError,
        IntradayPnlStore,
        build_intraday_pnl_point,
    )
    from position_advice_store import (  # type: ignore[no-redef]
        AdviceIntegrityError,
        load_advice_state,
        portfolio_fingerprint_sha256,
        public_position_advice,
        update_position_advice_store,
    )
    from portfolio_registry import (  # type: ignore[no-redef]
        MAIN_PORTFOLIO_ID,
        MAIN_PORTFOLIO_LABEL,
        PortfolioNotFound,
        PortfolioRegistryError,
        build_portfolio_config,
        create_portfolio,
        delete_portfolio,
        flatten_positions,
        load_extra_portfolios,
        update_portfolio,
    )
    import technical_indicators as technical_indicators_module  # type: ignore[no-redef]
    import technical_signal_engine as technical_signal_engine_module  # type: ignore[no-redef]
    import school_signal_engine as school_signal_engine_module  # type: ignore[no-redef]
    import auto_paper_trading as auto_paper_module  # type: ignore[no-redef]
    import auto_pilot as auto_pilot_module  # type: ignore[no-redef]
    import options_sentiment as options_sentiment_module  # type: ignore[no-redef]
    import derivatives_regime as derivatives_regime_module  # type: ignore[no-redef]
try:
    from scripts import nextday_model as nextday_model_module
    from scripts import nextday_backtest as nextday_backtest_module
except ImportError:  # 直跑 scripts/dashboard_server.py 时
    import nextday_model as nextday_model_module  # type: ignore[no-redef]
    import nextday_backtest as nextday_backtest_module  # type: ignore[no-redef]
try:
    from scripts import intraday_forecast as intraday_forecast_module
except ImportError:  # 直跑 scripts/dashboard_server.py 时
    import intraday_forecast as intraday_forecast_module  # type: ignore[no-redef]
try:
    from scripts import news_sentiment as news_sentiment_module
except ImportError:  # 直跑 scripts/dashboard_server.py 时
    import news_sentiment as news_sentiment_module  # type: ignore[no-redef]
try:
    from scripts import market_intel as market_intel_module
except ImportError:  # 直跑 scripts/dashboard_server.py 时
    import market_intel as market_intel_module  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "dashboard"
PORTFOLIO_PATH = ROOT / "config" / "portfolio.json"
EXTRA_PORTFOLIOS_PATH = ROOT / "config" / "portfolios.json"
WATCHLIST_PATH = ROOT / "config" / "watchlist.json"
NEWS_CACHE_PATH = ROOT / "data" / "dashboard-news-cache.json"
MARKET_NEWS_CACHE_PATH = ROOT / "data" / "dashboard-market-news-cache.json"
QUOTE_CACHE_PATH = ROOT / "data" / "dashboard-quote-cache.json"
PORTFOLIO_PERFORMANCE_PATH = ROOT / "data" / "portfolio-performance.json"
MARKET_SNAPSHOT_PATH = ROOT / "data" / "market-snapshot.json"
MACRO_SNAPSHOT_PATH = ROOT / "data" / "macro-snapshot.json"
MARKET_POLICY_PATH = ROOT / "config" / "market-regime-policy.json"
MONITORING_POLICY_PATH = ROOT / "config" / "monitoring-policy.json"
RISK_POLICY_PATH = ROOT / "config" / "risk-policy.json"
FORWARD_VALIDATION_POLICY_PATH = ROOT / "config" / "forward-validation-policy.json"
FORWARD_VALIDATION_LEDGER_PATH = ROOT / "data" / "forward-validation.jsonl"
INTRADAY_PNL_DATA_DIR = ROOT / "data" / "intraday-pnl"
INTRADAY_BACKFILL_DATA_DIR = ROOT / "data" / "intraday-pnl-backfill"
POSITION_ADVICE_STATE_PATH = ROOT / "data" / "position-advice-state.json"
POSITION_ADVICE_AUDIT_PATH = ROOT / "data" / "position-advice-audit.jsonl"
DATA_RETENTION_POLICY_PATH = ROOT / "config" / "data-retention-policy.json"
DATA_RETENTION_STATE_PATH = ROOT / "data" / "data-retention-state.json"

# 三大流派信号面板（趋势跟踪/道氏理论/量价分析Wyckoff）
SCHOOL_SIGNAL_LOG_PATH = ROOT / "data" / "school-signal-log.jsonl"
SCHOOL_SIGNAL_STOCKS = (
    ("sh688545", "688545.SH", "兴福电子"),
    ("sh603881", "603881.SH", "数据港"),
    ("sz000977", "000977.SZ", "浪潮信息"),
    ("sz300042", "300042.SZ", "朗科科技"),
    ("sh603516", "603516.SH", "淳中科技"),
)
SCHOOL_SIGNAL_CACHE_TTL_SECONDS = 300.0  # 5 分钟服务端缓存
SCHOOL_SIGNAL_LOCK = threading.Lock()
SCHOOL_SIGNAL_CACHE: dict[str, Any] = {}
TENCENT_NEWFQ_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get"

# Personal index (fixed snapshot 2026-08-25) — served live so the K-line
# updates together with individual stock quotes during trading hours.
PERSONAL_INDEX_PATH = STATIC_DIR / "personal-index" / "index_data.json"
PERSONAL_INDEX_TTL_SECONDS = 30.0
TOMORROW_PLANS_TTL_SECONDS = 30 * 60.0
TOMORROW_BACKTEST_TTL_SECONDS = 6 * 3600.0
_TOMORROW_PLANS_CACHE: dict[str, Any] = {"payload": None, "monotonic": 0.0}
_TOMORROW_PLANS_BUILD_LOCK = threading.Lock()
_TOMORROW_BACKTEST_CACHE: dict[str, Any] = {"payload": None, "monotonic": 0.0, "signature": ""}
MARKET_INTEL_FLOW_TTL_SECONDS = 6 * 3600.0
MARKET_INTEL_RETRY_SECONDS = 60.0
# datacenter 当日资金流兜底条目的短 TTL：当日值盘中持续变化，不能按 6h 定格。
MARKET_INTEL_TODAY_TTL_SECONDS = 600.0
MARKET_INTEL_FETCH_BUDGET = 6  # 每轮最多补采的标的数（EM 按 IP 封禁突发）
MARKET_INTEL_EM_PACING_SECONDS = 2.5
MARKET_LEVEL_TTL_SECONDS = 2 * 3600.0
_MARKET_INTEL_FLOW_STORE: dict[str, dict[str, Any]] = {}
_MARKET_LEVEL_CACHE: dict[str, Any] = {"monotonic": 0.0, "payload": {}}
PERSONAL_INDEX_BASE_DATE = "2026-08-25"
PERSONAL_INDEX_HOLDINGS: dict[str, tuple[str, int]] = {
    "000977": ("浪潮信息", 4000),
    "603881": ("数据港", 8400),
    "300319": ("麦捷科技", 12000),
    "300042": ("朗科科技", 5000),
    "512930": ("AAI人工智能ETF", 300000),
    "688545": ("兴福电子", 5000),
    "603516": ("淳中科技", 2000),
    "300276": ("三丰智能", 20000),
    "603019": ("中科曙光", 1000),
    "300766": ("每日互动", 3000),
    "002400": ("省广集团", 13000),
    "600475": ("华光环能", 5000),
    "600515": ("海南机场", 20000),
    "159146": ("电力ETF", 106000),
    "688825": ("长鑫科技", 300),
    "601877": ("正泰电器", 1000),
}
PERSONAL_INDEX_BENCHMARKS = {"sh000300": "沪深300", "sh000905": "中证500"}
_PERSONAL_INDEX_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}

CN_TZ = timezone(timedelta(hours=8))
SERVER_PROCESS_STARTED_AT = datetime.now(tz=CN_TZ).isoformat(timespec="seconds")
SERVER_SOURCE_MTIME_NS_AT_START = Path(__file__).stat().st_mtime_ns
SERVER_API_CAPABILITIES = (
    "stock-chart.api.v1",
    "daily-pnl-history.api.v1",
    "multi-portfolio.api.v1",
    "personal-index.api.v1",
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TENCENT_QUOTE = "https://qt.gtimg.cn/q="
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
TENCENT_MINUTE_KLINE = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
TENCENT_USFQ_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
TENCENT_GLOBAL_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
# 港美指数分时主机链（2026-10-20 实测三台均返回 193 个分时点）：
# web.ifzq / 裸 ifzq / proxy.finance.qq.com。此前该能力只挂单一主机，
# 一旦被腾讯 WAF 整主机拦截就没有任何备援，港美分时面板会整块空白。
TENCENT_GLOBAL_MINUTE_HOSTS = (
    TENCENT_GLOBAL_MINUTE,
    "https://ifzq.gtimg.cn/appstock/app/minute/query",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
)

# 腾讯K线主机回退：web.ifzq.gtimg.cn 曾被其 WAF 整主机拦截（返回 501 WAF 页），
# 同一接口在 ifzq.gtimg.cn（无 web. 前缀）一直可用。各用途按"当前主机优先"
# 排列并粘住最近一次成功的主机，WAF/HTTP 错误时轮换到下一个，全部失败才抛错。
#
# 前复权日K走 newfqkline：2026-10-11 实测 fqkline/get 在 web./ifzq 两台主机
# 同时被 WAF 501 拦截（端点级封禁，非主机级），而 newfqkline/get 双主机正常。
# newfqkline 不接受空起止日期（返回 200 但数据为空），必须显式传日期区间。
TENCENT_USFQ_KLINE_HOSTS = (
    TENCENT_USFQ_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/usfqkline/get",
)
TENCENT_MINUTE_KLINE_HOSTS = (
    TENCENT_MINUTE_KLINE,
    "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
)
TENCENT_NEWFQ_KLINE_HOSTS = (
    TENCENT_NEWFQ_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    # 第三主机（2026-10-11 实测可用，来源：stock_all_in_1 项目共享指南）：
    # proxy.finance.qq.com 走独立网关，web./裸 ifzq 被端点级 WAF 拦截时仍可用。
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
TENCENT_KLINE_HOSTS = (
    TENCENT_KLINE,
    "https://ifzq.gtimg.cn/appstock/app/kline/kline",
)
_TENCENT_KLINE_HOST_LOCK = threading.Lock()
_TENCENT_KLINE_STICKY: dict[str, int] = {}
SINA_HQ = "https://hq.sinajs.cn/list="
SINA_FEED = "https://zhibo.sina.com.cn/api/zhibo/feed"
EASTMONEY_SEARCH = "https://search-api-web.eastmoney.com/search/jsonp"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_TRENDS2 = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
CNINFO_ORG_LIST = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_ANNOUNCEMENTS = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
# 巨潮公告的备援源（2026-10-20 实测）：东财公告接口按 stock_list=代码 过滤，
# 不依赖巨潮 orgId 映射表，巨潮整体不可用时补位（同为法定披露口径）。
EASTMONEY_ANNOUNCEMENTS = "https://np-anotice-stock.eastmoney.com/api/security/ann"

QUOTE_TTL_SECONDS = 3.0
QUOTE_MAX_STALE_SECONDS = 300.0
QUOTE_CACHE_SCHEMA_VERSION = "dashboard-quote-cache.v1"
QUOTE_CACHE_MAX_BYTES = 5 * 1024 * 1024
NEWS_TTL_SECONDS = 600.0
MARKET_NEWS_TTL_SECONDS = 120.0
# The scheduler stamps next_due at tick time while freshness resets when the
# worker finishes.  The multi-source collector cycle itself runs ~60s, so the
# margin must exceed it: with threshold TTL-margin, the age seen at the next
# tick (TTL - cycle) still triggers a refresh and the cadence stays at one
# fetch per tick instead of silently slipping to every other tick.
MARKET_NEWS_REFRESH_MARGIN_SECONDS = 90.0
# Cache age legitimately ends each cycle near the TTL while a fresh capture is
# already in flight.  Flag staleness only past this grace window.
MARKET_NEWS_STALE_GRACE_SECONDS = 30.0
MARKET_NEWS_FAILURE_RETRY_SECONDS = 60.0
MARKET_NEWS_RATE_LIMIT_RETRY_SECONDS = 30 * 60.0
DECISION_TTL_SECONDS = 30.0
LIQUIDITY_TTL_SECONDS = 600.0
LIQUIDITY_FAILURE_RETRY_SECONDS = 60.0
EM_MIN_INTERVAL_SECONDS = 1.15
EM_RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60.0
BACKGROUND_MAX_SLEEP_SECONDS = 15.0
DATA_RETENTION_INTERVAL_SECONDS = 3600.0
QUOTE_VALIDATION_MIN_TOLERANCE_PCT = 0.25
QUOTE_VALIDATION_TICK_ALLOWANCE = 0.02
STOCK_CHART_DAILY_COUNT = 500
STOCK_CHART_MINUTE_COUNT = 320
STOCK_CHART_DAILY_TTL_SECONDS = 6 * 60 * 60.0
STOCK_CHART_MAX_CACHE_ENTRIES = 32
POSITION_ADVICE_REFRESH_INTERVAL_SECONDS = 5 * 60.0
POSITION_ADVICE_FAILURE_RETRY_SECONDS = 60.0

INDEXES = [
    ("sh000001", "上证综指"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000852", "中证1000"),
]

TENCENT_GLOBALS = [
    ("hkHSI", "恒生指数"),
    ("usINX", "标普500"),
    ("usIXIC", "纳斯达克"),
    ("usDJI", "道琼斯"),
]

SINA_GLOBALS: dict[str, tuple[str, str, str]] = {
    "hf_ES": ("标普500期货", "future", "点"),
    "hf_NQ": ("纳指期货", "future", "点"),
    "b_NKY": ("日经225", "world_index", "点"),
    "b_KOSPI": ("韩国KOSPI", "world_index", "点"),
    "DINIW": ("美元指数", "fx", "点"),
    "fx_susdcnh": ("USD/CNH", "fx", "人民币"),
}
# 纽约原油/黄金（hf_CL/hf_GC）已并入「隔夜全球标的」的商品组（b_CL/b_GC），
# 全球参考不再展示，避免同屏重复；原 hf_ 符号的图表白名单保留以兼容旧链接。

# 美债收益率：东财全球债券（市场 171），新浪 globalbd 接口无数据。
# fltt=2 直读小数：f43 最新收益率、f60 昨收、f169 变化（百分点）、f170 相对涨跌%。
EM_BOND_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
EM_BOND_YIELDS: dict[str, tuple[str, str]] = {
    "US10Y": ("美债收益率", "%"),
}

# 主备线状态登记（2026-10-13）：每条数据链路记录最近一次实际生效的来源层级，
# 供 /api/health 与页脚展示。只记录事实（tier/source/时间/降级原因），不参与
# 抓取决策；tier 取值 primary=主源 backup=备源 down=主备均失败。
DATA_SOURCE_CHAIN_LOCK = threading.Lock()
DATA_SOURCE_CHAIN_STATUS: dict[str, dict[str, Any]] = {}
DATA_SOURCE_CHAIN_LABELS = {
    "realtime_quote": "实时行情",
    "cn_daily_kline": "个股/指数日K",
    "cn_intraday": "分时(沪深)",
    "us10y_yield": "美债收益率",
    "daily_pnl_history": "每日盈亏历史",
}


def _record_data_source_chain(
    chain: str, tier: str, source: str, detail: str = ""
) -> None:
    with DATA_SOURCE_CHAIN_LOCK:
        DATA_SOURCE_CHAIN_STATUS[chain] = {
            "chain": chain,
            "label": DATA_SOURCE_CHAIN_LABELS.get(chain, chain),
            "tier": tier,
            "source": source,
            "detail": detail,
            "updated_at": _iso_now(),
        }


def _data_source_chains_snapshot() -> dict[str, Any]:
    """全部主备链路的最近一次生效层级；未观测过的链路标 unknown。"""

    with DATA_SOURCE_CHAIN_LOCK:
        entries = copy.deepcopy(DATA_SOURCE_CHAIN_STATUS)
    try:
        pnl_status = daily_pnl_last_fetch_status()
    except Exception:
        pnl_status = {}
    if pnl_status:
        pnl_status["label"] = DATA_SOURCE_CHAIN_LABELS["daily_pnl_history"]
        entries["daily_pnl_history"] = pnl_status
    snapshot: dict[str, Any] = {}
    for key, label in DATA_SOURCE_CHAIN_LABELS.items():
        entry = entries.get(key)
        snapshot[key] = (
            entry
            if entry
            else {"chain": key, "label": label, "tier": "unknown",
                  "source": "", "detail": "", "updated_at": ""}
        )
    return snapshot

# 隔夜全球标的（2026-10-14「全球市场」快讯覆盖的欧股指数、美股个股/中概 ADR，
# 外加商品组：原油/黄金自全球参考并入，COMEX铜为 AI 电网/算力强相关金属）。
# symbol 统一用 b_ 前缀并入 INDEX_CHART_INSTRUMENTS 白名单；
# 东财 ulist 批量行情一次请求取全部（105=纳斯达克，106=纽交所，153=OTC，
# 101=COMEX，100=全球指数）；SK海力士东财不覆盖韩国个股，行情与日K走 Naver。
# SpaceX 未上市无行情代码，纳斯达克中国金龙指数(HXC)东财亦无行情，均不入列。
EM_OVERNIGHT_INSTRUMENTS: list[dict[str, str]] = [
    {"symbol": "b_FTSE", "name": "英国富时100", "em_secid": "100.FTSE", "group": "欧股指数"},
    {"symbol": "b_GDAXI", "name": "德国DAX", "em_secid": "100.GDAXI", "group": "欧股指数"},
    {"symbol": "b_FCHI", "name": "法国CAC40", "em_secid": "100.FCHI", "group": "欧股指数"},
    {"symbol": "b_CL", "name": "纽约原油", "em_secid": "102.CL00Y", "group": "商品"},
    {"symbol": "b_GC", "name": "纽约黄金", "em_secid": "101.GC00Y", "group": "商品"},
    {"symbol": "b_SI", "name": "COMEX白银", "em_secid": "101.SI00Y", "group": "商品"},
    {"symbol": "b_HG", "name": "COMEX铜", "em_secid": "101.HG00Y", "group": "商品"},
    {"symbol": "b_MSFT", "name": "微软", "em_secid": "105.MSFT", "group": "美股科技"},
    {"symbol": "b_META", "name": "Meta", "em_secid": "105.META", "group": "美股科技"},
    {"symbol": "b_NVDA", "name": "英伟达", "em_secid": "105.NVDA", "group": "美股科技"},
    {"symbol": "b_GOOGL", "name": "谷歌-A", "em_secid": "105.GOOGL", "group": "美股科技"},
    {"symbol": "b_AAPL", "name": "苹果", "em_secid": "105.AAPL", "group": "美股科技"},
    {"symbol": "b_AMZN", "name": "亚马逊", "em_secid": "105.AMZN", "group": "美股科技"},
    {"symbol": "b_TSLA", "name": "特斯拉", "em_secid": "105.TSLA", "group": "美股科技"},
    {"symbol": "b_SNDK", "name": "闪迪", "em_secid": "105.SNDK", "group": "存储链"},
    {"symbol": "b_STX", "name": "希捷科技", "em_secid": "105.STX", "group": "存储链"},
    {"symbol": "b_WDC", "name": "西部数据", "em_secid": "105.WDC", "group": "存储链"},
    {"symbol": "b_MU", "name": "美光科技", "em_secid": "105.MU", "group": "存储链"},
    {"symbol": "b_SKHYNIX", "name": "SK海力士", "naver_symbol": "000660", "group": "存储链"},
    {"symbol": "b_RKLB", "name": "Rocket Lab", "em_secid": "105.RKLB", "group": "太空概念"},
    {"symbol": "b_LUNR", "name": "Intuitive Machines", "em_secid": "105.LUNR", "group": "太空概念"},
    {"symbol": "b_ASTS", "name": "AST SpaceMobile", "em_secid": "105.ASTS", "group": "太空概念"},
    {"symbol": "b_COP", "name": "康菲石油", "em_secid": "106.COP", "group": "油气"},
    {"symbol": "b_EOG", "name": "EOG能源", "em_secid": "106.EOG", "group": "油气"},
    {"symbol": "b_CVX", "name": "雪佛龙", "em_secid": "106.CVX", "group": "油气"},
    {"symbol": "b_XOM", "name": "埃克森美孚", "em_secid": "106.XOM", "group": "油气"},
    {"symbol": "b_LI", "name": "理想汽车", "em_secid": "105.LI", "group": "中概股"},
    {"symbol": "b_NIO", "name": "蔚来", "em_secid": "106.NIO", "group": "中概股"},
    {"symbol": "b_PDD", "name": "拼多多", "em_secid": "105.PDD", "group": "中概股"},
    {"symbol": "b_XPEV", "name": "小鹏汽车", "em_secid": "106.XPEV", "group": "中概股"},
    {"symbol": "b_JD", "name": "京东", "em_secid": "105.JD", "group": "中概股"},
    {"symbol": "b_BABA", "name": "阿里巴巴", "em_secid": "106.BABA", "group": "中概股"},
    {"symbol": "b_TCOM", "name": "携程", "em_secid": "105.TCOM", "group": "中概股"},
    {"symbol": "b_TCEHY", "name": "腾讯(ADR)", "em_secid": "153.TCEHY", "group": "中概股"},
    {"symbol": "b_NTES", "name": "网易", "em_secid": "105.NTES", "group": "中概股"},
    {"symbol": "b_BIDU", "name": "百度", "em_secid": "105.BIDU", "group": "中概股"},
    {"symbol": "b_BILI", "name": "哔哩哔哩", "em_secid": "105.BILI", "group": "中概股"},
]

# 主要市场/全球参考卡片上可展示K线与分时图的指数白名单。与持仓股票一样按
# 显式白名单放行，避免本地服务被当作任意代码的行情代理。数据源逐一实测：
# A股指数走腾讯 newfqkline/mkline；恒生走腾讯 newfqkline+minute/query；美国三大
# 指数走腾讯 usfqkline（fqkline 只有最近一日）+minute/query；日经/KOSPI/
# 油金/美元系走东财 push2his（secid 实测）；ES/NQ 外盘期货东财无数据，
# 用新浪全球期货日K（无分时源，前端显示空态）。
INDEX_CHART_INSTRUMENTS: dict[str, dict[str, str]] = {
    "sh000001": {"name": "上证综指", "kind": "tencent"},
    "sz399001": {"name": "深证成指", "kind": "tencent"},
    "sz399006": {"name": "创业板指", "kind": "tencent"},
    "sh000688": {"name": "科创50", "kind": "tencent"},
    "sh000300": {"name": "沪深300", "kind": "tencent"},
    "sh000905": {"name": "中证500", "kind": "tencent"},
    "sh000852": {"name": "中证1000", "kind": "tencent"},
    "b_KOSPI": {"name": "韩国KOSPI", "kind": "eastmoney", "secid": "100.KS11", "naver_symbol": "KOSPI"},
    "b_NKY": {"name": "日经225", "kind": "eastmoney", "secid": "100.N225"},
    "hkHSI": {"name": "恒生指数", "kind": "tencent_hk"},
    "usINX": {"name": "标普500", "kind": "tencent_us"},
    "usIXIC": {"name": "纳斯达克", "kind": "tencent_us"},
    "usDJI": {"name": "道琼斯", "kind": "tencent_us"},
    "hf_ES": {"name": "标普500期货", "kind": "sina_futures", "sina_symbol": "ES"},
    "hf_NQ": {"name": "纳指期货", "kind": "sina_futures", "sina_symbol": "NQ"},
    "hf_CL": {"name": "纽约原油", "kind": "eastmoney", "secid": "102.CL00Y"},
    "hf_GC": {"name": "纽约黄金", "kind": "eastmoney", "secid": "101.GC00Y"},
    "DINIW": {"name": "美元指数", "kind": "eastmoney", "secid": "100.UDI"},
    "fx_susdcnh": {"name": "USD/CNH", "kind": "eastmoney", "secid": "133.USDCNH"},
    "US10Y": {"name": "美债收益率", "kind": "eastmoney", "secid": "171.US10Y"},
}

INDEX_CHART_LOOKUP: dict[str, str] = {
    key.lower(): key for key in INDEX_CHART_INSTRUMENTS
}

# 隔夜全球标的全部可出图：东财 secid 走 push2his 日K + trends2 分时；
# SK海力士走 Naver 日K（无分时源，弹层分时面板显示空态）。
INDEX_CHART_INSTRUMENTS.update(
    {
        item["symbol"]: {
            "name": item["name"],
            "kind": "naver" if item.get("naver_symbol") else "eastmoney",
            **({"secid": item["em_secid"]} if item.get("em_secid") else {}),
            **({"naver_symbol": item["naver_symbol"]} if item.get("naver_symbol") else {}),
        }
        for item in EM_OVERNIGHT_INSTRUMENTS
    }
)

INDEX_CHART_LOOKUP: dict[str, str] = {
    key.lower(): key for key in INDEX_CHART_INSTRUMENTS
}

MATERIAL_TERMS: dict[str, tuple[str, ...]] = {
    "业绩": ("业绩", "年报", "中报", "季报", "净利润", "营业收入", "预告", "快报"),
    "资本": ("回购", "增持", "减持", "定增", "融资", "分红", "解禁", "质押"),
    "交易": ("并购", "重组", "收购", "出售", "控制权", "实控人", "停牌", "复牌"),
    "经营": ("中标", "订单", "合同", "合作", "投产", "扩产", "项目", "产品发布"),
    "监管": ("立案", "调查", "问询", "处罚", "监管", "诉讼", "仲裁", "风险提示"),
}

RETRY = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
    raise_on_status=False,
)

# Eastmoney 403/429 responses are WAF/rate-limit signals.  Retrying them inside
# urllib3 would amplify the block before the application-level circuit breaker can
# react, so this session retries only transient 5xx/connection failures.
EM_RETRY = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
    # We honor Retry-After in _eastmoney_cooldown_seconds.  Leaving urllib3's
    # handling enabled would make it retry 429 even though 429 is not listed above.
    respect_retry_after_header=False,
)

LIVE_RETRY = Retry(
    total=1,
    connect=1,
    read=1,
    status=1,
    backoff_factor=0.2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)


def _session(retry: Retry = RETRY) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


LIVE_SESSION = _session(LIVE_RETRY)
EM_SESSION = _session(EM_RETRY)
NEWS_SESSION = _session()

PORTFOLIO_LOCK = threading.RLock()
QUOTE_LOCK = threading.Lock()
QUOTE_REFRESH_LOCK = threading.Lock()
QUOTE_CACHE: dict[str, Any] = {
    "payload": None,
    "monotonic": 0.0,
    "last_error": "",
    "refreshing": False,
}

# 最近一次行情刷新拿到的原始行情（腾讯口径，含成交量/时间戳）。
# 与 QUOTE_CACHE 分开：QUOTE_CACHE 存的是**面向前端**的载荷（行情被摊平进
# positions/markets），这里留一份原始字典，供"合成当日 bar"复用，避免为了
# 一根 bar 再打一次腾讯接口。
_LIVE_QUOTES_LOCK = threading.Lock()
_LIVE_QUOTES: dict[str, dict[str, Any]] = {}

NEWS_LOCK = threading.Lock()
NEWS_REFRESH_LOCK = threading.Lock()
NEWS_STATE: dict[str, Any] = {
    "payload": None,
    "monotonic": 0.0,
    "refreshing": False,
    "last_error": "",
}

MARKET_NEWS_LOCK = threading.Lock()
MARKET_NEWS_REFRESH_LOCK = threading.Lock()
MARKET_NEWS_STATE: dict[str, Any] = {
    "payload": None,
    "monotonic": 0.0,
    "last_attempt_monotonic": 0.0,
    "next_retry_monotonic": 0.0,
    "refreshing": False,
    "last_error": "",
}

DECISION_LOCK = threading.RLock()
DECISION_BUILD_LOCK = threading.Lock()
DECISION_STATE: dict[str, Any] = {
    "payload": None,
    "monotonic": 0.0,
    "generation": 0,
    "last_error": "",
}

LIQUIDITY_LOCK = threading.RLock()
LIQUIDITY_BUILD_LOCK = threading.Lock()
LIQUIDITY_STATE: dict[str, Any] = {
    "payload": None,
    "input_fingerprint": "",
    "monotonic": 0.0,
    "last_error": "",
}

MAINTENANCE_LOCK = threading.RLock()
MAINTENANCE_STATE: dict[str, dict[str, Any]] = {
    name: {
        "refreshing": False,
        "last_attempt_at": "",
        "last_success_at": "",
        "last_error": "",
        "last_skip_reason": "",
    }
    for name in (
        "daily_snapshot",
        "macro_snapshot",
        "data_retention",
        "decision",
        "forward_validation",
        "tomorrow_plans",
        "index_charts",
    )
}

EM_THROTTLE_LOCK = threading.Lock()
EM_LAST_CALL = 0.0
EM_CIRCUITS: dict[str, dict[str, Any]] = {}
CNINFO_ORG_MAP: dict[str, str] | None = None
HITHINK_QUOTE_PROVIDER = HithinkQuoteProvider(ROOT)
INTRADAY_PNL_STORE = IntradayPnlStore(INTRADAY_PNL_DATA_DIR)
DAILY_PNL_HISTORY_STORE = DailyPnlHistoryStore(DAILY_PNL_HISTORY_CACHE_PATH)

INTRADAY_PNL_LOCK = threading.RLock()
INTRADAY_PNL_STATE: dict[str, Any] = {
    "last_attempt_at": "",
    "last_recorded_at": "",
    "last_result": {},
    "last_error": "",
}

POSITION_ADVICE_LOCK = threading.RLock()
POSITION_ADVICE_STATE: dict[str, Any] = {
    "loaded": False,
    "public": None,
    "refreshing": False,
    "last_attempt_at": "",
    "last_attempt_monotonic": 0.0,
    "last_success_at": "",
    "last_success_monotonic": 0.0,
    "last_error": "",
    "next_retry_monotonic": 0.0,
    "pending_snapshot_id": "",
    "last_snapshot_id": "",
}

STOCK_CHART_LOCK = threading.RLock()
STOCK_CHART_BUILD_LOCK = threading.Lock()
STOCK_CHART_CACHE: dict[str, dict[str, Any]] = {}


class EastmoneyCircuitOpen(RuntimeError):
    """Raised before I/O while an Eastmoney hostname is cooling down."""


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _iso_now() -> str:
    return _now().isoformat(timespec="seconds")


def _a_share_session_date(day: date_type) -> tuple[bool, str]:
    """Classify an A-share session date without making a network request.

    Mainland exchanges stay closed on weekends, including statutory make-up
    workdays.  ``chinese-calendar`` supplies weekday public holidays for the
    current supported years.  If its data is unavailable or the installed
    release has not learned a future year yet, fall back to the weekday rule
    instead of crashing the scheduler or permanently disabling quotes.
    """

    if day.weekday() >= 5:
        return False, "weekend"
    if _china_is_holiday is None:
        return True, "weekday_fallback_missing_dependency"
    try:
        holiday = bool(_china_is_holiday(day))
    except (NotImplementedError, TypeError, ValueError):
        return True, "weekday_fallback_unsupported_year"
    return (False, "public_holiday") if holiday else (True, "public_calendar")


def _background_refresh_profile(at: datetime | None = None) -> dict[str, Any]:
    """Return conservative refresh cadences for the current China session phase.

    Whole-market flashes stay on a two-minute cadence because both sources are 7x24.
    Per-position-news traffic backs off outside the A-share information window.
    Quote callbacks may still run to notice phase transitions, but network quote
    capture is disabled outside auction and continuous trading, including force.
    """

    observed = at or _now()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=CN_TZ)
    observed = observed.astimezone(CN_TZ)
    current = observed.time().replace(tzinfo=None)
    is_session_date, calendar_status = _a_share_session_date(observed.date())
    if not is_session_date:
        phase = "weekend" if calendar_status == "weekend" else "holiday"
        quote_seconds, position_news_seconds = 900.0, 3600.0
    elif daytime(9, 15) <= current < daytime(9, 25):
        phase = "auction"
        quote_seconds, position_news_seconds = 3.0, NEWS_TTL_SECONDS
    elif daytime(9, 30) <= current <= daytime(11, 30) or daytime(13, 0) <= current <= daytime(15, 0):
        phase = "open"
        quote_seconds, position_news_seconds = 3.0, NEWS_TTL_SECONDS
    elif daytime(11, 30) < current < daytime(13, 0):
        phase = "lunch"
        quote_seconds, position_news_seconds = 30.0, NEWS_TTL_SECONDS
    elif (
        daytime(8, 30) <= current < daytime(9, 15)
        or daytime(9, 25) <= current < daytime(9, 30)
    ):
        phase = "pre_open"
        quote_seconds, position_news_seconds = 30.0, NEWS_TTL_SECONDS
    elif daytime(15, 0) < current < daytime(18, 0):
        phase = "post_close"
        quote_seconds, position_news_seconds = 300.0, 900.0
    else:
        phase = "overnight"
        quote_seconds, position_news_seconds = 900.0, 1800.0
    return {
        "phase": phase,
        "trading_day": is_session_date,
        "calendar_status": calendar_status,
        # Quotes are intentionally network-silent outside the live A-share
        # session.  The non-live intervals below only control how quickly the
        # scheduler notices the next phase transition; callbacks still pass
        # through the hard gate in ensure_quote_refresh().
        "quote_refresh_enabled": phase in {"auction", "open"},
        "quote_seconds": quote_seconds,
        "position_news_seconds": position_news_seconds,
        "market_news_seconds": MARKET_NEWS_TTL_SECONDS,
        "daily_snapshot_seconds": (
            180.0 if phase == "post_close" else 1800.0 if phase == "overnight" else 3600.0
        ),
        "macro_snapshot_seconds": 21600.0,
        "data_retention_seconds": DATA_RETENTION_INTERVAL_SECONDS,
        "tomorrow_plans_seconds": TOMORROW_PLANS_TTL_SECONDS,
        "index_charts_seconds": 900.0,
        "decision_seconds": (
            30.0
            if phase in {"open", "auction"}
            else 60.0
            if phase == "lunch"
            else 120.0
            if phase in {"pre_open", "post_close"}
            else 900.0
        ),
    }


def _quote_refresh_enabled(at: datetime | None = None) -> bool:
    """Return whether a new network quote capture may start now.

    Keep this as the shared gate for browser-forced, scheduler, startup and
    decision-context paths.  Falling back to the phase check keeps tests and
    injected profiles written before ``quote_refresh_enabled`` compatible.
    """

    profile = _background_refresh_profile(at)
    phase = str(profile.get("phase") or "").lower()
    return bool(profile.get("quote_refresh_enabled", phase in {"auction", "open"}))


def _position_news_ttl_seconds(at: datetime | None = None) -> float:
    return float(_background_refresh_profile(at)["position_news_seconds"])


class BackgroundRefreshService:
    """Lifecycle-owned scheduler that keeps all dashboard feeds warm without clients."""

    _COMPONENTS = (
        "quote",
        "position_news",
        "market_news",
        "daily_snapshot",
        "macro_snapshot",
        "data_retention",
        "decision",
        "tomorrow_plans",
        "index_charts",
        "paper_t",
        "autopilot",
    )

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_due: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        self._phase = "starting"
        self._last_tick_at = ""
        self._last_errors: dict[str, str] = {}

    @staticmethod
    def _profile_intervals(profile: dict[str, Any]) -> dict[str, float]:
        return {
            "quote": float(profile["quote_seconds"]),
            "position_news": float(profile["position_news_seconds"]),
            "market_news": float(profile["market_news_seconds"]),
            "daily_snapshot": float(profile["daily_snapshot_seconds"]),
            "macro_snapshot": float(profile["macro_snapshot_seconds"]),
            "data_retention": float(profile["data_retention_seconds"]),
            "decision": float(profile["decision_seconds"]),
            "tomorrow_plans": float(profile["tomorrow_plans_seconds"]),
            "index_charts": float(profile["index_charts_seconds"]),
            "paper_t": float(profile["quote_seconds"]),
            "autopilot": float(profile["quote_seconds"]),
        }

    @staticmethod
    def _callbacks() -> dict[str, Any]:
        # Resolve globals at call time so tests can inject collectors and so the
        # class can live above the concrete refresh functions in this module.
        return {
            "quote": ensure_quote_refresh,
            "position_news": ensure_news_refresh,
            "market_news": ensure_market_news_refresh,
            "daily_snapshot": ensure_daily_snapshot_refresh,
            "macro_snapshot": ensure_macro_snapshot_refresh,
            "data_retention": ensure_data_retention_refresh,
            "decision": ensure_decision_refresh,
            "tomorrow_plans": ensure_tomorrow_plans_refresh,
            "index_charts": ensure_index_charts_refresh,
            "paper_t": auto_paper_module.run_all_strategies,
            "autopilot": auto_pilot_module.run_cycle,
        }

    def start(self, *, immediate: bool = False) -> bool:
        """Start one non-daemon scheduler thread; return False if already running."""

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            now_monotonic = time.monotonic()
            profile = _background_refresh_profile()
            intervals = self._profile_intervals(profile)
            self._phase = str(profile["phase"])
            self._intervals = intervals
            self._next_due = {
                name: (
                    now_monotonic
                    if immediate or name in {"data_retention", "tomorrow_plans", "index_charts"}
                    else now_monotonic + intervals[name]
                )
                for name in self._COMPONENTS
            }
            self._last_errors = {}
            thread = threading.Thread(
                target=self._run,
                name="dashboard-background-refresh",
                daemon=False,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
            raise
        return True

    def run_due(self, *, now_monotonic: float | None = None) -> float:
        """Run due scheduling actions once and return seconds until the next check."""

        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        profile = _background_refresh_profile()
        intervals = self._profile_intervals(profile)
        with self._state_lock:
            if not self._next_due:
                self._next_due = {name: now_value for name in self._COMPONENTS}
            for name in self._COMPONENTS:
                previous_interval = self._intervals.get(name)
                if previous_interval is not None and intervals[name] < previous_interval:
                    # Entering the live session must pull a long overnight deadline
                    # forward; otherwise a 15-minute overnight timer could miss open.
                    self._next_due[name] = min(
                        self._next_due.get(name, now_value), now_value + intervals[name]
                    )
                self._next_due.setdefault(name, now_value)
            self._phase = str(profile["phase"])
            self._intervals = intervals
            due = [name for name in self._COMPONENTS if now_value >= self._next_due[name]]
            for name in due:
                self._next_due[name] = now_value + intervals[name]
            if due:
                self._last_tick_at = _iso_now()

        callbacks = self._callbacks()
        for name in due:
            try:
                callbacks[name]()
            except Exception as exc:  # scheduler must survive one collector failure
                error = f"{type(exc).__name__}: {exc}"[:500]
                with self._state_lock:
                    self._last_errors[name] = error
            else:
                with self._state_lock:
                    self._last_errors.pop(name, None)

        with self._state_lock:
            delay = min(self._next_due.values(), default=now_value + BACKGROUND_MAX_SLEEP_SECONDS) - now_value
        return max(0.05, min(BACKGROUND_MAX_SLEEP_SECONDS, delay))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                delay = self.run_due()
            except Exception as exc:  # a profile/clock failure must not kill maintenance
                with self._state_lock:
                    self._last_errors["scheduler"] = f"{type(exc).__name__}: {exc}"[:500]
                delay = 1.0
            self._stop_event.wait(delay)

    def stop(self, *, timeout: float = 5.0) -> bool:
        """Stop promptly and join the scheduler (network workers remain daemonized)."""

        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._state_lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def snapshot(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        with self._state_lock:
            thread = self._thread
            return {
                "running": bool(thread is not None and thread.is_alive()),
                "phase": self._phase,
                "interval_seconds": copy.deepcopy(self._intervals),
                "next_due_seconds": {
                    name: round(max(0.0, due - now_monotonic), 1)
                    for name, due in self._next_due.items()
                },
                "last_tick_at": self._last_tick_at,
                "last_errors": copy.deepcopy(self._last_errors),
            }


BACKGROUND_REFRESH_SERVICE: BackgroundRefreshService | None = None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _safe_round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _set_maintenance_skip(name: str, reason: str) -> None:
    with MAINTENANCE_LOCK:
        MAINTENANCE_STATE[name]["last_skip_reason"] = reason[:500]


def _spawn_maintenance(name: str, worker: Any) -> bool:
    with MAINTENANCE_LOCK:
        state = MAINTENANCE_STATE[name]
        if state.get("refreshing"):
            return False
        state["refreshing"] = True
        state["last_attempt_at"] = _iso_now()
        state["last_skip_reason"] = ""

    def run() -> None:
        try:
            worker()
        except Exception as exc:
            with MAINTENANCE_LOCK:
                state = MAINTENANCE_STATE[name]
                state["refreshing"] = False
                state["last_error"] = f"{type(exc).__name__}: {exc}"[:800]
        else:
            with MAINTENANCE_LOCK:
                state = MAINTENANCE_STATE[name]
                state["refreshing"] = False
                state["last_success_at"] = _iso_now()
                state["last_error"] = ""

    thread = threading.Thread(
        target=run, name=f"dashboard-{name.replace('_', '-')}", daemon=True
    )
    try:
        thread.start()
    except Exception:
        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE[name]["refreshing"] = False
        raise
    return True


def _run_snapshot_command(
    *,
    script_name: str,
    output_path: Path,
    validator: Any,
    timeout_seconds: float,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / script_name),
            "--output",
            str(output_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
        raise RuntimeError(
            f"{script_name} exited {completed.returncode}: {detail[-500:]}"
        )
    payload = _read_json(output_path)
    validator(payload)


def _cached_quote_session() -> tuple[str, str]:
    with QUOTE_LOCK:
        payload = copy.deepcopy(QUOTE_CACHE.get("payload") or {})
    status = payload.get("market_status") if isinstance(payload.get("market_status"), dict) else {}
    quote_time = _parse_quote_datetime(status.get("quote_time"))
    return (
        str(status.get("state") or "unknown").lower(),
        quote_time.date().isoformat() if quote_time else "",
    )


def ensure_daily_snapshot_refresh(force: bool = False) -> bool:
    """Refresh completed-session evidence only outside partial-bar windows."""

    phase = str(_background_refresh_profile().get("phase") or "unknown")
    if not force and phase in {"auction", "open", "lunch", "pre_open"}:
        _set_maintenance_skip(
            "daily_snapshot", f"deferred during {phase}; current daily bar may be partial"
        )
        return False
    quote_phase, expected_session = _cached_quote_session()
    try:
        current = _read_json(MARKET_SNAPSHOT_PATH)
    except Exception:
        current = {}
    current_session = str(current.get("data_date") or "")[:10]
    validated = bool((current.get("validation") or {}).get("passed"))
    if (
        not force
        and validated
        and expected_session
        and current_session == expected_session
    ):
        _set_maintenance_skip(
            "daily_snapshot", f"already current for quote session {expected_session}"
        )
        return False

    def validate(payload: dict[str, Any]) -> None:
        if (payload.get("validation") or {}).get("passed") is not True:
            raise RuntimeError("daily snapshot cross-source validation did not pass")
        actual = str(payload.get("data_date") or "")[:10]
        if expected_session and quote_phase == "closed" and actual != expected_session:
            raise RuntimeError(
                f"daily snapshot session {actual or 'missing'} does not match closed quote session {expected_session}"
            )

    return _spawn_maintenance(
        "daily_snapshot",
        lambda: _run_snapshot_command(
            script_name="market_snapshot.py",
            output_path=MARKET_SNAPSHOT_PATH,
            validator=validate,
            timeout_seconds=240.0,
        ),
    )


def ensure_macro_snapshot_refresh(force: bool = False) -> bool:
    try:
        current = _read_json(MACRO_SNAPSHOT_PATH)
        generated = _parse_quote_datetime(current.get("generated_at"))
    except Exception:
        current, generated = {}, None
    age = (_now() - generated).total_seconds() if generated else None
    if (
        not force
        and generated is not None
        and age is not None
        and -120 <= age < 21600
        and (current.get("validation") or {}).get("passed") is True
    ):
        _set_maintenance_skip(
            "macro_snapshot", f"official macro snapshot checked {round(max(age, 0))} seconds ago"
        )
        return False

    def validate(payload: dict[str, Any]) -> None:
        if payload.get("ok") is not True:
            raise RuntimeError("macro snapshot returned ok=false")
        if (payload.get("validation") or {}).get("passed") is not True:
            raise RuntimeError("macro snapshot official-source validation did not pass")

    return _spawn_maintenance(
        "macro_snapshot",
        lambda: _run_snapshot_command(
            script_name="macro_snapshot.py",
            output_path=MACRO_SNAPSHOT_PATH,
            validator=validate,
            timeout_seconds=300.0,
        ),
    )


def _forward_validation_imports() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        from scripts.forward_validation import (
            DuplicateRegistrationError,
            ForwardValidationLedger,
            document_sha256,
            load_policy,
            register_daily,
            settle_due_outcomes,
        )
    except ModuleNotFoundError as exc:
        if exc.name not in {"scripts", "scripts.forward_validation"}:
            raise
        from forward_validation import (  # type: ignore[no-redef]
            DuplicateRegistrationError,
            ForwardValidationLedger,
            document_sha256,
            load_policy,
            register_daily,
            settle_due_outcomes,
        )
    return (
        ForwardValidationLedger,
        DuplicateRegistrationError,
        document_sha256,
        load_policy,
        register_daily,
        settle_due_outcomes,
    )


def _run_forward_validation_cycle(
    context: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Settle exact-due outcomes, then preregister today's research states."""
    observed = (now or _now()).astimezone(CN_TZ)
    (
        ForwardValidationLedger,
        DuplicateRegistrationError,
        document_sha256,
        load_policy,
        register_daily,
        settle_due_outcomes,
    ) = _forward_validation_imports()
    market_snapshot = _read_json(MARKET_SNAPSHOT_PATH)
    strategy = _read_json(WATCHLIST_PATH)
    policy = load_policy(FORWARD_VALIDATION_POLICY_PATH)
    opportunities = context.get("opportunities")
    if not isinstance(opportunities, dict):
        raise RuntimeError("decision context omitted the opportunity radar")
    ledger = ForwardValidationLedger(FORWARD_VALIDATION_LEDGER_PATH)
    settlement = settle_due_outcomes(
        ledger,
        market_snapshot,
        policy,
        now=observed,
    )
    try:
        registration = register_daily(
            ledger,
            market_snapshot,
            opportunities,
            strategy,
            policy,
            now=observed,
        )
    except DuplicateRegistrationError:
        registrations = [
            record["payload"]
            for record in ledger.records()
            if record.get("event_type") == "RESEARCH_STATE_PREREGISTRATION"
        ]
        latest = registrations[-1] if registrations else {}
        binding = latest.get("binding") if isinstance(latest.get("binding"), dict) else {}
        if (
            latest.get("session_date") != market_snapshot.get("data_date")
            or binding.get("policy_sha256") != document_sha256(policy)
            or binding.get("strategy_sha256") != document_sha256(strategy)
        ):
            raise RuntimeError(
                "same-day forward-validation registration is bound to different inputs"
            )
        registration = {
            "ok": True,
            "status": "ALREADY_REGISTERED",
            "session_date": str(market_snapshot.get("data_date") or ""),
            "research_only": True,
            "full_market_selector": False,
        }
    return {
        "ok": True,
        "status": "UPDATED",
        "generated_at": observed.isoformat(timespec="seconds"),
        "settlement": settlement,
        "registration": registration,
        "ledger_summary": ledger.summary(),
        "research_only": True,
        "trading_instruction": False,
        "full_market_selector": False,
    }


def ensure_forward_validation_refresh(
    force: bool = False, *, context: dict[str, Any] | None = None
) -> bool:
    """Append only after a same-day completed session; otherwise record a skip."""
    observed = _now()
    current = observed.time().replace(tzinfo=None)
    if not force and (observed.weekday() >= 5 or current < daytime(15, 5)):
        _set_maintenance_skip(
            "forward_validation", "not due before the same-day 15:05 completed-session cutoff"
        )
        return False
    try:
        market_snapshot = _read_json(MARKET_SNAPSHOT_PATH)
    except Exception as exc:
        _set_maintenance_skip(
            "forward_validation", f"completed-session snapshot unavailable: {type(exc).__name__}"
        )
        return False
    if (
        str(market_snapshot.get("data_date") or "") != observed.date().isoformat()
        or (market_snapshot.get("validation") or {}).get("passed") is not True
    ):
        _set_maintenance_skip(
            "forward_validation", "waiting for a validated snapshot from the current China session"
        )
        return False
    captured = copy.deepcopy(context) if isinstance(context, dict) else None

    def build() -> None:
        decision = captured or get_decision_context(force=True)
        result = _run_forward_validation_cycle(decision, now=observed)
        if result.get("ok") is not True:
            raise RuntimeError("forward validation cycle returned ok=false")

    return _spawn_maintenance("forward_validation", build)


def ensure_data_retention_refresh(force: bool = False) -> bool:
    """Run the strict allowlisted cleanup without blocking the HTTP thread."""

    try:
        policy = load_retention_policy(DATA_RETENTION_POLICY_PATH)
    except Exception as exc:
        def fail_policy(policy_error: Exception = exc) -> None:
            raise RuntimeError(
                f"data-retention policy invalid: {type(policy_error).__name__}"
            ) from policy_error

        return _spawn_maintenance("data_retention", fail_policy)

    if not force:
        try:
            previous = _read_json(DATA_RETENTION_STATE_PATH)
            completed = _parse_quote_datetime(previous.get("completed_at"))
        except Exception:
            completed = None
        interval = float(policy.get("cleanup_interval_seconds") or DATA_RETENTION_INTERVAL_SECONDS)
        try:
            advice_audit_oversize = (
                POSITION_ADVICE_AUDIT_PATH.stat().st_size
                > int(policy["position_advice_audit"]["max_bytes"])
            )
        except OSError:
            advice_audit_oversize = False
        if (
            not advice_audit_oversize
            and completed is not None
            and -120 <= (_now() - completed).total_seconds() < interval
        ):
            _set_maintenance_skip(
                "data_retention", "last successful allowlisted cleanup is still current"
            )
            return False

    def build() -> None:
        report = run_data_retention(
            project_root=ROOT,
            policy=policy,
            dry_run=False,
            state_path=DATA_RETENTION_STATE_PATH,
        )
        if report.get("ok") is not True:
            raise RuntimeError(
                f"data retention completed with {((report.get('totals') or {}).get('errors') or 0)} errors"
            )

    return _spawn_maintenance("data_retention", build)


def ensure_decision_refresh(force: bool = False) -> bool:
    del force  # the scheduler cadence is the freshness policy for this derived view

    def build() -> None:
        payload = get_decision_context(force=True)
        if not isinstance(payload, dict) or not payload.get("snapshot_id"):
            raise RuntimeError("decision context did not produce an evidence-bound snapshot")
        ensure_forward_validation_refresh(context=payload)

    return _spawn_maintenance("decision", build)


def ensure_tomorrow_plans_refresh(force: bool = False) -> bool:
    """Keep the T-trade plan cache warm so UI polls never wait out a rebuild.

    The cold build takes about a minute (chart dailies + walk-forward backtest
    on first start), which exceeds the browser fetch timeout; the scheduler
    owns the cadence and the rebuild runs off the scheduler thread.
    """

    def build() -> None:
        payload = _build_tomorrow_plans_payload(force=force)
        if not isinstance(payload, dict) or not payload.get("plans"):
            raise RuntimeError("tomorrow plans build produced no plans")

    return _spawn_maintenance("tomorrow_plans", build)


def ensure_index_charts_refresh(force: bool = False) -> bool:
    """预热全部指数图表缓存。

    每个品种按各自交易时段计算 TTL：预热一次后，非交易时段内重复调用
    全部命中内存缓存，不再向上游发起新的查询；临近开盘（TTL 覆盖到开盘
    前 5 分钟）自动恢复刷新，保证开盘即有可用的 K 线与分时。
    """

    def build() -> None:
        failures: list[str] = []
        for key in INDEX_CHART_INSTRUMENTS:
            try:
                get_stock_chart_payload(key)
            except Exception as exc:
                failures.append(f"{key}:{type(exc).__name__}")
        if len(failures) >= max(1, len(INDEX_CHART_INSTRUMENTS) // 2):
            raise RuntimeError("多数指数图表预热失败：" + "; ".join(failures[:6]))

    return _spawn_maintenance("index_charts", build)


def _maintenance_health() -> dict[str, Any]:
    with MAINTENANCE_LOCK:
        result = copy.deepcopy(MAINTENANCE_STATE)
    for name, path in (
        ("daily_snapshot", MARKET_SNAPSHOT_PATH),
        ("macro_snapshot", MACRO_SNAPSHOT_PATH),
    ):
        try:
            payload = _read_json(path)
            result[name]["file_available"] = True
            result[name]["file_generated_at"] = payload.get("generated_at")
            result[name]["data_as_of"] = (
                payload.get("data_date")
                if name == "daily_snapshot"
                else ((payload.get("pmi") or {}).get("period"))
            )
            result[name]["validation_passed"] = bool(
                (payload.get("validation") or {}).get("passed")
            )
        except Exception as exc:
            result[name].update(
                file_available=False,
                validation_passed=False,
                file_error=f"{type(exc).__name__}: {exc}"[:500],
            )
    forward = result["forward_validation"]
    forward["ledger_path"] = str(FORWARD_VALIDATION_LEDGER_PATH)
    forward["ledger_available"] = FORWARD_VALIDATION_LEDGER_PATH.is_file()
    if forward["ledger_available"]:
        try:
            ForwardValidationLedger = _forward_validation_imports()[0]
            summary = ForwardValidationLedger(FORWARD_VALIDATION_LEDGER_PATH).summary()
            forward.update(
                integrity_verified=summary.get("status") == "VERIFIED",
                record_count=summary.get("record_count"),
                registration_count=summary.get("registration_count"),
                outcome_count=summary.get("outcome_count"),
                unresolved_outcome_count=summary.get("unresolved_outcome_count"),
                latest_registration_session=summary.get("latest_registration_session"),
                head_hash=summary.get("head_hash"),
                research_only=True,
                full_market_selector=False,
            )
        except Exception as exc:
            forward.update(
                integrity_verified=False,
                ledger_error=_redact_error(f"{type(exc).__name__}: {exc}"),
            )
    else:
        forward.update(
            integrity_verified=None,
            record_count=0,
            registration_count=0,
            outcome_count=0,
            unresolved_outcome_count=0,
            latest_registration_session=None,
            research_only=True,
            full_market_selector=False,
        )
    retention = result["data_retention"]
    retention["state_available"] = DATA_RETENTION_STATE_PATH.is_file()
    if retention["state_available"]:
        try:
            state = _read_json(DATA_RETENTION_STATE_PATH)
            retention.update(
                report_ok=state.get("ok") is True,
                completed_at=state.get("completed_at"),
                dry_run=state.get("dry_run"),
                totals=copy.deepcopy(state.get("totals") or {}),
            )
        except Exception as exc:
            retention.update(
                report_ok=False,
                state_error=_redact_error(f"{type(exc).__name__}: {exc}"),
            )
    return result


def _symbol_from_thscode(thscode: str) -> str:
    code, exchange = thscode.strip().split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange.upper())
    if not prefix or not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"Unsupported thscode: {thscode!r}")
    return prefix + code


def _load_positions() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    portfolio = _read_json(PORTFOLIO_PATH)
    positions: list[dict[str, Any]] = []
    for account in portfolio.get("accounts") or []:
        for item in account.get("positions") or []:
            record = dict(item)
            record.update(
                {
                    "account_id": account.get("account_id", ""),
                    "account_type": account.get("account_type", ""),
                    "symbol": _symbol_from_thscode(str(item.get("thscode", ""))),
                    "code": str(item.get("thscode", "")).split(".", 1)[0],
                }
            )
            positions.append(record)
    if not positions:
        raise RuntimeError("No positions found in config/portfolio.json")
    return portfolio, positions


def _load_watchlist_items() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    watchlist = _read_json(WATCHLIST_PATH)
    items: list[dict[str, Any]] = []
    for raw in watchlist.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        thscode = str(item.get("thscode") or "")
        item.update(
            {
                "symbol": _symbol_from_thscode(thscode),
                "code": thscode.split(".", 1)[0],
            }
        )
        items.append(item)
    return watchlist, items


PORTFOLIO_CONFIG, CONFIGURED_POSITIONS = _load_positions()
PORTFOLIO_MTIME_NS = PORTFOLIO_PATH.stat().st_mtime_ns
PORTFOLIO_RELOAD_ERROR = ""


def _refresh_portfolio_config_if_changed(force: bool = False) -> bool:
    """Atomically reload a valid portfolio file while preserving the last good copy."""
    global PORTFOLIO_CONFIG, CONFIGURED_POSITIONS
    global PORTFOLIO_MTIME_NS, PORTFOLIO_RELOAD_ERROR

    try:
        candidate_mtime = PORTFOLIO_PATH.stat().st_mtime_ns
    except OSError as exc:
        with PORTFOLIO_LOCK:
            PORTFOLIO_RELOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return False

    with PORTFOLIO_LOCK:
        if not force and candidate_mtime == PORTFOLIO_MTIME_NS:
            return False
        try:
            portfolio, positions = _load_positions()
        except Exception as exc:
            PORTFOLIO_RELOAD_ERROR = f"{type(exc).__name__}: {exc}"
            return False
        PORTFOLIO_CONFIG = portfolio
        CONFIGURED_POSITIONS = positions
        PORTFOLIO_MTIME_NS = candidate_mtime
        PORTFOLIO_RELOAD_ERROR = ""
        return True


def _portfolio_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]], int, str]:
    _refresh_portfolio_config_if_changed()
    with PORTFOLIO_LOCK:
        return (
            copy.deepcopy(PORTFOLIO_CONFIG),
            copy.deepcopy(CONFIGURED_POSITIONS),
            PORTFOLIO_MTIME_NS,
            PORTFOLIO_RELOAD_ERROR,
        )


def _load_extra_portfolio_entries() -> list[dict[str, Any]]:
    """Read user-defined portfolios; a corrupt file raises and keeps the old copy."""

    entries: list[dict[str, Any]] = []
    for config in load_extra_portfolios(EXTRA_PORTFOLIOS_PATH):
        entries.append({"config": config, "positions": flatten_positions(config)})
    return entries


def _extra_portfolios_mtime() -> int:
    try:
        return EXTRA_PORTFOLIOS_PATH.stat().st_mtime_ns
    except OSError:
        return 0


EXTRA_PORTFOLIO_ENTRIES: list[dict[str, Any]] = []
EXTRA_PORTFOLIOS_MTIME_NS = -1
EXTRA_PORTFOLIOS_ERROR = ""


def _refresh_extra_portfolios_if_changed(force: bool = False) -> bool:
    """Hot-reload config/portfolios.json while keeping the last good copy."""

    global EXTRA_PORTFOLIO_ENTRIES, EXTRA_PORTFOLIOS_MTIME_NS, EXTRA_PORTFOLIOS_ERROR

    candidate_mtime = _extra_portfolios_mtime()
    with PORTFOLIO_LOCK:
        if not force and candidate_mtime == EXTRA_PORTFOLIOS_MTIME_NS:
            return False
        try:
            entries = _load_extra_portfolio_entries()
        except Exception as exc:
            EXTRA_PORTFOLIOS_ERROR = f"{type(exc).__name__}: {exc}"
            return False
        EXTRA_PORTFOLIO_ENTRIES = entries
        EXTRA_PORTFOLIOS_MTIME_NS = candidate_mtime
        EXTRA_PORTFOLIOS_ERROR = ""
        return True


def _extra_portfolios_snapshot() -> tuple[list[dict[str, Any]], int, str]:
    _refresh_extra_portfolios_if_changed()
    with PORTFOLIO_LOCK:
        return (
            copy.deepcopy(EXTRA_PORTFOLIO_ENTRIES),
            EXTRA_PORTFOLIOS_MTIME_NS,
            EXTRA_PORTFOLIOS_ERROR,
        )


_refresh_extra_portfolios_if_changed(force=True)


def _main_portfolio_label(config: dict[str, Any]) -> str:
    return str(config.get("label") or MAIN_PORTFOLIO_LABEL)


def _all_portfolio_entries() -> list[dict[str, Any]]:
    """Main portfolio first, then every user-defined portfolio."""

    portfolio, positions, _, _ = _portfolio_snapshot()
    extras, _, _ = _extra_portfolios_snapshot()
    entries = [
        {
            "portfolio_id": MAIN_PORTFOLIO_ID,
            "label": _main_portfolio_label(portfolio),
            "editable": False,
            "base_portfolio_id": "",
            "config": portfolio,
            "positions": positions,
        }
    ]
    for item in extras:
        config = item["config"]
        entries.append(
            {
                "portfolio_id": str(config.get("portfolio_id")),
                "label": str(config.get("label") or config.get("portfolio_id")),
                "editable": True,
                "base_portfolio_id": str(config.get("base_portfolio_id") or MAIN_PORTFOLIO_ID),
                "config": config,
                "positions": item["positions"],
            }
        )
    return entries


def _resolve_portfolio_entry(portfolio_id: str) -> dict[str, Any] | None:
    wanted = str(portfolio_id or MAIN_PORTFOLIO_ID)
    for entry in _all_portfolio_entries():
        if entry["portfolio_id"] == wanted:
            return entry
    return None


def _union_configured_positions() -> list[dict[str, Any]]:
    """All portfolios' positions de-duplicated by thscode for shared fetching."""

    merged: dict[str, dict[str, Any]] = {}
    for entry in _all_portfolio_entries():
        for position in entry["positions"]:
            key = str(position.get("thscode") or "").upper()
            if key and key not in merged:
                merged[key] = position
    return list(merged.values())


def _tag_positions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {**position, "portfolio_id": entry["portfolio_id"]}
        for position in entry["positions"]
    ]


def _parse_tencent_time(raw: str) -> datetime | None:
    value = (raw or "").strip()
    for pattern in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=CN_TZ)
        except ValueError:
            pass
    return None


def _format_quote_time(raw: str) -> str:
    parsed = _parse_tencent_time(raw)
    return parsed.isoformat(timespec="seconds") if parsed else (raw or "")


_CN_SYMBOL_RE = re.compile(r"(?:s[hz]|bj)\d{6}")


def _fetch_sina_cn_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """新浪 A股/ETF/指数实时备援（腾讯 qt.gtimg.cn 失效时补齐价格类字段）。

    字段名与 ``_fetch_tencent`` 输出对齐；新浪没有换手率/PE/PB/市值，这些置
    None（下游一律经 _float 容错）。成交量新浪口径是"股"（腾讯是"手"），不
    下发以免跨源量纲混用。港美代码该端点无覆盖，自动跳过。
    """

    wanted = [s for s in symbols if _CN_SYMBOL_RE.fullmatch(str(s))]
    if not wanted:
        return {}
    response = LIVE_SESSION.get(
        SINA_HQ + ",".join(wanted),
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=(2.5, 4.0),
    )
    response.raise_for_status()
    text = response.content.decode("gbk", "replace")
    result: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        match = re.match(r'var hq_str_([^=]+)="([^"]*)"', line.strip())
        if not match or match.group(1) not in wanted:
            continue
        symbol, raw = match.groups()
        fields = raw.split(",")
        if len(fields) < 32 or _float(fields[3]) is None:
            continue
        price = _float(fields[3])
        previous = _float(fields[2])
        change = None
        change_pct = None
        if price is not None and previous not in (None, 0):
            change = price - previous
            change_pct = change / previous * 100
        amount_yi = _float(fields[9])
        result[symbol] = {
            "symbol": symbol,
            "source_name": fields[0],
            "source_code": symbol[2:],
            "price": price,
            "prev_close": previous,
            "open": _float(fields[1]),
            "change": change,
            "change_pct": change_pct,
            "high": _float(fields[4]),
            "low": _float(fields[5]),
            "amount_yi": None if amount_yi is None else amount_yi / 1e8,
            "turnover_pct": None,
            "pe_ttm": None,
            "amplitude_pct": None,
            "float_mcap_yi": None,
            "mcap_yi": None,
            "pb": None,
            "quote_time_raw": f"{fields[30]} {fields[31]}",
            "quote_time": _format_quote_time(f"{fields[30]} {fields[31]}"),
            "source": "新浪财经(备援)",
        }
    return result


def _fetch_tencent(symbols: list[str]) -> dict[str, dict[str, Any]]:
    response = LIVE_SESSION.get(
        TENCENT_QUOTE + ",".join(symbols),
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=(2.5, 4.0),
    )
    response.raise_for_status()
    raw = response.content.decode("gbk", "replace")
    result: dict[str, dict[str, Any]] = {}
    for line in raw.split(";"):
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].rsplit("_", 1)[-1]
        values = line.split('"', 2)[1].split("~")
        if len(values) < 53 or _float(values[3]) is None:
            continue
        price = _float(values[3])
        previous = _float(values[4])
        change = _float(values[31])
        if change is None and price is not None and previous is not None:
            change = price - previous
        amount_wan = _float(values[37])
        result[key] = {
            "symbol": key,
            "source_name": values[1],
            "source_code": values[2],
            "price": price,
            "prev_close": previous,
            "open": _float(values[5]),
            "change": change,
            "change_pct": _float(values[32]),
            "high": _float(values[33]),
            "low": _float(values[34]),
            # 成交量（手）。腾讯 quote 与 K 线接口的单位一致（个股=手、指数=股），
            # 因此可直接用于合成"当日 bar"，无需换算。
            "volume_lots": _float(values[6]),
            "amount_yi": None if amount_wan is None else amount_wan / 10000.0,
            "turnover_pct": _float(values[38]),
            "pe_ttm": _float(values[39]),
            "amplitude_pct": _float(values[43]),
            "float_mcap_yi": _float(values[44]),
            "mcap_yi": _float(values[45]),
            "pb": _float(values[46]),
            "quote_time_raw": values[30],
            "quote_time": _format_quote_time(values[30]),
        }
    return result


class StockChartSymbolNotHeld(LookupError):
    """Raised when the chart route is asked to proxy an unconfigured symbol."""


def _resolve_portfolio_chart_instrument(raw_symbol: str) -> dict[str, Any]:
    """Resolve one canonical instrument for chart lookups.

    Resolution accepts (in order): the market-index allowlist above, then any
    configured holding.  The dashboard is a portfolio viewer, not an
    unrestricted network proxy — a caller cannot make the local service fetch
    arbitrary tickers.  A stock held by several portfolios resolves through the
    same shared entry.
    """

    candidate = str(raw_symbol or "").strip()
    index_key = INDEX_CHART_LOOKUP.get(candidate) or INDEX_CHART_LOOKUP.get(candidate.lower())
    if index_key:
        entry = INDEX_CHART_INSTRUMENTS[index_key]
        return {
            "thscode": index_key,
            "symbol": index_key,
            "name": str(entry.get("name") or index_key),
            "instrument_type": "index",
            "chart_kind": str(entry.get("kind") or "tencent"),
            "secid": str(entry.get("secid") or ""),
            "sina_symbol": str(entry.get("sina_symbol") or ""),
            "naver_symbol": str(entry.get("naver_symbol") or ""),
        }
    thscode = candidate.upper()
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", thscode):
        raise ValueError("symbol must use the canonical form 600519.SH")
    for item in _union_configured_positions():
        if str(item.get("thscode") or "").upper() == thscode:
            return {
                "thscode": thscode,
                "symbol": str(item["symbol"]),
                "name": str(item.get("name") or thscode),
                "instrument_type": str(item.get("instrument_type") or "stock"),
            }
    raise StockChartSymbolNotHeld(f"{thscode} is not in any configured portfolio")


def _canonical_daily_bar_time(value: Any) -> str | None:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _canonical_intraday_bar_time(
    value: Any, fallback_date: str | None = None
) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    parsed: datetime | None = None
    # ``datetime.strptime`` is permissive for adjacent numeric directives: a
    # 12-digit minute stamp such as 202609020935 can satisfy the 14-digit
    # seconds pattern and become 09:03:05.  Select numeric formats by exact
    # length before parsing so Tencent's five-minute bars retain 09:35.
    if re.fullmatch(r"\d{14}", raw):
        patterns = ("%Y%m%d%H%M%S",)
    elif re.fullmatch(r"\d{12}", raw):
        patterns = ("%Y%m%d%H%M",)
    else:
        patterns = (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        )
    for pattern in patterns:
        try:
            parsed = datetime.strptime(raw, pattern)
            break
        except ValueError:
            pass
    if parsed is None and fallback_date and re.fullmatch(r"\d{4}(?:\d{2})?", raw):
        try:
            clock_pattern = "%H%M%S" if len(raw) == 6 else "%H%M"
            clock = datetime.strptime(raw, clock_pattern).time()
            parsed = datetime.combine(date_type.fromisoformat(fallback_date), clock)
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.isoformat(timespec="seconds"), parsed.date().isoformat()


def _parse_chart_bar(
    raw: Any, *, intraday: bool, fallback_date: str | None = None
) -> tuple[dict[str, Any], str] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 6:
        return None
    if intraday:
        parsed_time = _canonical_intraday_bar_time(raw[0], fallback_date)
        if parsed_time is None:
            return None
        bar_time, trade_date = parsed_time
    else:
        bar_time = _canonical_daily_bar_time(raw[0])
        if bar_time is None:
            return None
        trade_date = bar_time
    open_, close, high, low = (_float(value) for value in raw[1:5])
    volume = _float(raw[5])
    if any(value is None or value <= 0 for value in (open_, close, high, low)):
        return None
    assert open_ is not None and close is not None and high is not None and low is not None
    if high + 1e-9 < max(open_, close) or low - 1e-9 > min(open_, close) or high < low:
        return None
    return (
        {
            "time": bar_time,
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume if volume is not None and volume >= 0 else None,
        },
        trade_date,
    )


_TENCENT_KLINE_RATE_LOCK = threading.Lock()
_TENCENT_KLINE_LAST_CALL = 0.0
_TENCENT_KLINE_MIN_INTERVAL_SECONDS = 0.35


def _tencent_kline_throttle() -> None:
    """腾讯K线全局最小间隔：防止连续悬停/批量刷新触发上游 WAF。"""
    global _TENCENT_KLINE_LAST_CALL
    with _TENCENT_KLINE_RATE_LOCK:
        wait = _TENCENT_KLINE_MIN_INTERVAL_SECONDS - (time.monotonic() - _TENCENT_KLINE_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _TENCENT_KLINE_LAST_CALL = time.monotonic()


def _fetch_tencent_kline(
    purpose: str,
    hosts: tuple[str, ...],
    params: dict[str, Any],
    *,
    referer: str,
    timeout: tuple[float, float],
) -> dict[str, Any]:
    """腾讯K线请求：粘性主机 + WAF/HTTP 错误时按列表轮换，全部失败才抛错。

    web.ifzq.gtimg.cn 曾被腾讯 WAF 整主机拦截（返回 501 WAF 页），同一接口在
    ifzq.gtimg.cn（无 web. 前缀）一直可用。成功的主机会被记住（粘性），下次
    直接复用；失败才轮换到下一个主机，全部失败才抛出最后一个错误。
    """
    with _TENCENT_KLINE_HOST_LOCK:
        start_index = _TENCENT_KLINE_STICKY.get(purpose, 0)
    last_error: Exception | None = None
    for offset in range(len(hosts)):
        index = (start_index + offset) % len(hosts)
        url = hosts[index]
        _tencent_kline_throttle()
        try:
            response = LIVE_SESSION.get(
                url,
                params=params,
                headers={"User-Agent": UA, "Referer": referer},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Tencent kline response is not an object")
            with _TENCENT_KLINE_HOST_LOCK:
                _TENCENT_KLINE_STICKY[purpose] = index
            return payload
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"Tencent kline hosts unavailable: {type(last_error).__name__}: {last_error}"
    )


def _tencent_daily_date_range(count: int) -> tuple[str, str]:
    """newfqkline 需要显式起止日期：按所需 bar 数推算（交易日≈日历日×0.62）。

    end 取"今天"而不是"明天"：腾讯对**指数**类品种在 end=今天 时会多返回一根
    当日实时 bar（实测 2026-10-20 盘中），图表当天就有 bar；个股的前复权序列
    不含当日 bar，由 `_merge_live_daily_bar` 用实时行情补入。
    """

    end_day = date_type.today()
    start_day = end_day - timedelta(days=int(count * 1.6) + 40)
    return start_day.isoformat(), end_day.isoformat()


def _live_quote_ohlcv(symbols: Sequence[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """批量抓取腾讯实时行情的 OHLCV（成交量沿用接口原始单位）。

    腾讯 quote 与 K 线接口对**同一品种**的成交量单位完全一致（个股=手、指数=股），
    实测 2026-10-20：600519 的 quote 量 22689 == kline 当日 bar 量 22689；
    上证的 quote 量 344554213 == newfqkline 当日 bar 量 344554213。
    因此这里的成交量可直接用于合成"当日 bar"，无需任何换算。
    """

    requested = [str(symbol) for symbol in symbols if symbol]
    if not requested:
        return {}, ""
    response = LIVE_SESSION.get(
        TENCENT_QUOTE + ",".join(requested),
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=(2.5, 4.0),
    )
    response.raise_for_status()
    raw = response.content.decode("gbk", "replace")
    rows: dict[str, dict[str, Any]] = {}
    trade_date = ""
    for line in raw.split(";"):
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].rsplit("_", 1)[-1]
        values = line.split('"', 2)[1].split("~")
        if len(values) < 40:
            continue

        def _num(index: int, source: list[str] = values) -> float | None:
            try:
                return float(source[index])
            except (ValueError, IndexError, TypeError):
                return None

        price = _num(3)
        if price is None or price <= 0:
            continue
        open_ = _num(5)
        high = _num(33)
        low = _num(34)
        volume_lots = _num(6)
        stamp = str(values[30])[:8] if len(values) > 30 else ""
        date_str = (
            f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
            if len(stamp) == 8 and stamp.isdigit()
            else ""
        )
        if not trade_date and date_str:
            trade_date = date_str
        rows[key] = {
            "open": open_ if open_ and open_ > 0 else price,
            "close": price,
            "high": high if high and high > 0 else price,
            "low": low if low and low > 0 else price,
            "volume_lots": volume_lots or 0.0,
            "date": date_str,
        }
    return rows, trade_date


def _cached_quote_for_symbol(symbol: str) -> dict[str, Any] | None:
    """取最近一次行情刷新拿到的标的实时行情（不额外打上游）。

    看板行情本来就按 3 秒节拍刷新，合成"当日 bar"直接复用这份数据，避免为了
    一根 bar 再压一次腾讯接口（腾讯 WAF 对突发请求很敏感）。
    """

    with _LIVE_QUOTES_LOCK:
        quote = _LIVE_QUOTES.get(symbol)
        return dict(quote) if isinstance(quote, dict) else None


def _merge_live_daily_bar(symbol: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把"当日 bar"并入日K序列（盘中实时）。

    实测（2026-10-20 盘中）：腾讯 newfqkline 的**个股**前复权序列无论 end 取今日
    还是明日，最后一根都是上一交易日——当日 bar 根本不在序列里；**指数**类品种
    走 day 键，会自带一根当日实时 bar。个股缺 bar 时用实时行情合成补入：

    * **口径一致**：前复权以"今天"为基准，当天原始价即前复权价；
    * **单位一致**：腾讯 quote 与 K 线接口成交量单位相同（个股=手、指数=股）；
    * 行情取自 3 秒节拍的实时行情缓存，不额外打上游；缓存里没有该标的就不合并。

    收盘后同一交易日仍以收盘行情修正，与个人指数的既有做法一致。
    """

    if not items:
        return items
    quote = _cached_quote_for_symbol(symbol)
    if not quote:
        return items
    stamp = str(quote.get("quote_time_raw") or "")[:8]
    if len(stamp) != 8 or not stamp.isdigit():
        return items
    trade_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    if items and str(items[-1].get("time") or "") == trade_date:
        # 序列已含当日 bar（指数类由腾讯自带），保持腾讯原值不动。
        return items
    open_ = _float(quote.get("open"))
    close = _float(quote.get("price"))
    high = _float(quote.get("high"))
    low = _float(quote.get("low"))
    volume = _float(quote.get("volume_lots"))
    if any(value is None or value <= 0 for value in (open_, close, high, low)):
        return items
    if volume is None or volume <= 0:
        # 没有成交量说明不是腾讯口径（新浪备援不下发成交量），不合成半截 bar。
        return items
    merged = [item for item in items if str(item.get("time") or "") != trade_date]
    merged.append(
        {
            "time": trade_date,
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume,
        }
    )
    merged.sort(key=lambda item: str(item.get("time") or ""))
    return merged


def _fetch_tencent_daily_chart(
    symbol: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    start_day, end_day = _tencent_daily_date_range(count)
    payload = _fetch_tencent_kline(
        "daily",
        TENCENT_NEWFQ_KLINE_HOSTS,
        {"param": f"{symbol},day,{start_day},{end_day},{int(count)},qfq"},
        referer="https://gu.qq.com/",
        timeout=(2.5, 5.0),
    )
    body = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = body.get("qfqday") or body.get("day") or []
    parsed: dict[str, dict[str, Any]] = {}
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        bar = _parse_chart_bar(raw, intraday=False)
        if bar is not None:
            parsed[bar[0]["time"]] = bar[0]
    items = [parsed[key] for key in sorted(parsed)][-max(1, int(count)) :]
    if not items:
        raise RuntimeError(f"Tencent returned no valid daily bars for {symbol}")
    # 盘中把"当日 bar"并进来（个股的前复权序列不含当日 bar）。
    items = _merge_live_daily_bar(symbol, items)[-max(1, int(count)) :]
    return {
        "adjustment": "qfq",
        "period": "day",
        "items": items,
        "source": "腾讯财经前复权日K",
    }


def _fetch_tencent_us_index_daily_chart(
    code: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    """美股指数日K（usDJI/usIXIC/usINX）：fqkline 只有最近一日，需走 usfqkline。"""

    payload = _fetch_tencent_kline(
        "daily_us",
        TENCENT_USFQ_KLINE_HOSTS,
        {"param": f"{code},day,,,{int(count)},qfq"},
        referer="https://gu.qq.com/",
        timeout=(2.5, 5.0),
    )
    body = (payload.get("data") or {}).get(code) or {}
    raw_rows = body.get("qfqday") or body.get("day") or []
    parsed: dict[str, dict[str, Any]] = {}
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        bar = _parse_chart_bar(raw, intraday=False)
        if bar is not None:
            parsed[bar[0]["time"]] = bar[0]
    items = [parsed[key] for key in sorted(parsed)][-max(1, int(count)) :]
    if not items:
        raise RuntimeError(f"Tencent returned no valid daily bars for {code}")
    return {
        "adjustment": "none",
        "period": "day",
        "items": items,
        "source": "腾讯财经日K",
    }


def _fetch_tencent_global_intraday_chart(
    code: str,
    *,
    fallback_date: str | None = None,
) -> dict[str, Any]:
    """全球指数当日分时（minute/query）。

    m5 分时端点（kline/mkline）不支持 hk/us 指数，只有 minute/query 可用：
    返回 "HHMM 现价 成交量" 点列，无逐点 OHLC，按单价合成线型 bar
    （open=close=high=low=现价），前端分时图本就只画价格线。

    主机链三台（web.ifzq / 裸 ifzq / proxy.finance.qq.com）走粘性轮换：
    腾讯 WAF 是 IP+主机级封禁，主机被封时自动切下一台，全部失败才抛错。
    """

    payload = _fetch_tencent_kline(
        "global_intraday",
        TENCENT_GLOBAL_MINUTE_HOSTS,
        {"code": code},
        referer="https://gu.qq.com/",
        timeout=(2.5, 5.0),
    )
    body = ((payload.get("data") or {}).get(code) or {})
    data_node = body.get("data") or {}
    raw_points = data_node.get("data") or []
    session_date = _canonical_daily_bar_time(data_node.get("date")) or fallback_date
    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    for point in raw_points if isinstance(raw_points, list) else []:
        parts = str(point).split()
        if len(parts) < 2:
            continue
        price = _float(parts[1])
        volume = _float(parts[2]) if len(parts) > 2 else 0.0
        if price is None or price <= 0:
            continue
        bar = _parse_chart_bar(
            [parts[0], price, price, price, price, volume or 0.0],
            intraday=True,
            fallback_date=session_date or fallback_date,
        )
        if bar is not None:
            parsed[bar[0]["time"]] = bar
    if not parsed:
        raise RuntimeError(f"Tencent returned no intraday points for {code}")
    latest_trade_date = max(trade_date for _, trade_date in parsed.values())
    items = [
        bar
        for key, (bar, trade_date) in sorted(parsed.items())
        if trade_date == latest_trade_date
    ]
    quote_values = ((body.get("qt") or {}).get(code) or []) if isinstance(body, dict) else []
    prev_close = _float(quote_values[4]) if len(quote_values) > 4 else None
    return {
        "trade_date": latest_trade_date,
        "period": "m1",
        "prev_close": prev_close if prev_close is not None and prev_close > 0 else None,
        "items": items,
        "source": "腾讯财经分时",
    }


def _fetch_tencent_intraday_chart(
    symbol: str,
    *,
    fallback_date: str | None = None,
    count: int = STOCK_CHART_MINUTE_COUNT,
) -> dict[str, Any]:
    payload = _fetch_tencent_kline(
        "minute",
        TENCENT_MINUTE_KLINE_HOSTS,
        {"param": f"{symbol},m5,,{int(count)}"},
        referer="https://gu.qq.com/",
        timeout=(2.5, 5.0),
    )
    body = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = body.get("m5") or []
    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        bar = _parse_chart_bar(raw, intraday=True, fallback_date=fallback_date)
        if bar is not None:
            parsed[bar[0]["time"]] = bar
    if not parsed:
        raise RuntimeError(f"Tencent returned no valid five-minute bars for {symbol}")
    latest_trade_date = max(trade_date for _, trade_date in parsed.values())
    items = [
        bar
        for key, (bar, trade_date) in sorted(parsed.items())
        if trade_date == latest_trade_date
    ]
    quote_values = ((body.get("qt") or {}).get(symbol) or []) if isinstance(body, dict) else []
    prev_close = _float(quote_values[4]) if len(quote_values) > 4 else None
    return {
        "trade_date": latest_trade_date,
        "period": "m5",
        "prev_close": prev_close if prev_close is not None and prev_close > 0 else None,
        "items": items,
        "source": "腾讯财经5分钟K线",
    }


def _em_secid_for_symbol(symbol: str) -> str | None:
    """腾讯 sh/sz 六位代码 → 东财 secid（沪 1 / 深 0；A股与境内指数通用）。"""

    if re.fullmatch(r"sh\d{6}", symbol):
        return f"1.{symbol[2:]}"
    if re.fullmatch(r"sz\d{6}", symbol):
        return f"0.{symbol[2:]}"
    return None


def _fetch_em_trends2_intraday(
    symbol: str,
    *,
    fallback_date: str | None = None,
) -> dict[str, Any]:
    """东财 trends2 分时备源（腾讯 mkline 双主机失败时启用）。

    push2his 仅接受 HTTP/2 → 复用 _curl_get_json（自带东财节流）。trends2 返回
    一分钟级点列（fields2=f51 时间,f53 现价,f56 量,f58 均价），无逐点 OHLC，与
    minute/query 同样按单价合成线型 bar。列序按"时间,现价"解析，量列取第三列
    （若上游调整 f56/f58 顺序不影响价格线）。
    """

    secid = _em_secid_for_symbol(symbol)
    if secid is None:
        raise RuntimeError(f"trends2 fallback only covers sh/sz symbols: {symbol}")
    url = (
        f"{EASTMONEY_TRENDS2}?secid={secid}&ndays=1&is_kc=0"
        "&fields1=f1,f2,f3,f4,f5&fields2=f51,f53,f56,f58"
    )
    payload = _curl_get_json(url, referer="https://quote.eastmoney.com/")
    body = payload.get("data") or {}
    raw_rows = body.get("trends") or []
    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        parts = str(raw).split(",")
        if len(parts) < 2:
            continue
        price = _float(parts[1])
        volume = _float(parts[2]) if len(parts) > 2 else None
        if price is None or price <= 0:
            continue
        bar = _parse_chart_bar(
            [parts[0], price, price, price, price, volume or 0.0],
            intraday=True,
            fallback_date=fallback_date,
        )
        if bar is not None:
            parsed[bar[0]["time"]] = bar
    if not parsed:
        raise RuntimeError(f"Eastmoney trends2 returned no intraday points for {symbol}")
    latest_trade_date = max(trade_date for _, trade_date in parsed.values())
    items = [
        bar
        for key, (bar, trade_date) in sorted(parsed.items())
        if trade_date == latest_trade_date
    ]
    prev_close = _float(body.get("preClose"))
    return {
        "trade_date": latest_trade_date,
        "period": "m1",
        "prev_close": prev_close if prev_close is not None and prev_close > 0 else None,
        "items": items,
        "source": "东方财富分时(备援)",
    }


def _curl_get_json(url: str, *, referer: str, timeout: float = 8.0) -> dict[str, Any]:
    """GET one JSON document through the system curl binary.

    push2his.eastmoney.com only completes HTTP/2 (ALPN ``h2``) handshakes; the
    vendored requests/urllib3 stack offers HTTP/1.1 only and gets the
    connection dropped even over plain HTTP.  curl negotiates h2 natively, and
    chart fetches are user-triggered and cached, so a subprocess is an
    acceptable transport for this one host.  The call shares the dashboard-wide
    Eastmoney throttle because its WAF blocks clients that burst requests.
    """

    global EM_LAST_CALL
    with EM_THROTTLE_LOCK:
        wait = EM_MIN_INTERVAL_SECONDS - (time.monotonic() - EM_LAST_CALL)
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.18))
        command = [
            "/usr/bin/curl",
            "--silent",
            "--show-error",
            "--fail",
            "--compressed",
            "--max-time",
            str(int(timeout)),
            "-H",
            f"User-Agent: {UA}",
            "-H",
            f"Referer: {referer}",
            url,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=True,
                timeout=timeout + 4.0,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("curl kline fetch timed out") from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                detail = str(getattr(exc, "stderr", b"") or b"")[:120]
            raise RuntimeError(f"curl kline fetch failed: {type(exc).__name__} {detail}".rstrip()) from exc
        finally:
            EM_LAST_CALL = time.monotonic()
    payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("curl kline response is not an object")
    return payload


# 东财 push2 的编号主机（push2=主站，23/17/48/90=同源镜像）。东财 WAF 会
# 临时封禁单台主机，被封时该主机的请求会以 "Empty reply from server" 失败，
# 因此先行指标这类"一次要打很多台机器"的调用必须轮换主机。
EM_PUSH2_HOSTS = (
    "push2.eastmoney.com",
    "23.push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "90.push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "29.push2.eastmoney.com",
    "19.push2.eastmoney.com",
)


def _curl_get_json_rotating(
    url_template: str,
    *,
    referer: str,
    timeout: float = 6.0,
    hosts: tuple[str, ...] = EM_PUSH2_HOSTS,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """GET one JSON document, rotating Eastmoney hosts on WAF blocks.

    ``url_template`` must contain a ``{host}`` placeholder for the host part.
    Each host is tried in turn; the first successful JSON object wins.  This
    mirrors the Tencent host-rotation helper: a single blocked mirror must not
    make an entire indicator unavailable.

    ``max_attempts`` bounds the total number of hosts tried so that a fully
    rate-limited source fails fast instead of blocking the request for the
    sum of every host timeout.
    """

    last_error: Exception | None = None
    ordered = list(hosts)
    if len(ordered) > 1:
        # Start at a rotating offset so repeated calls spread across mirrors
        # instead of always hammering the first one (which is what trips the
        # Eastmoney WAF in the first place).
        offset = int(time.monotonic()) % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
    for host in ordered[:max_attempts]:
        url = url_template.format(host=host)
        try:
            return _curl_get_json(url, referer=referer, timeout=timeout)
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"Eastmoney hosts unavailable: {type(last_error).__name__}: {last_error}"
    ) from last_error


def _fetch_eastmoney_kline(
    secid: str, *, klt: int, count: int
) -> tuple[list[list[Any]], float | None]:
    """Fetch raw Eastmoney kline rows (CSV strings) plus pre-close if present.

    Rows use the same field order as Tencent: date, open, close, high, low,
    volume, so ``_parse_chart_bar`` can consume them unchanged.  klt=101 is a
    daily bar, klt=5 a five-minute bar.
    """

    query = urlencode(
        {
            "secid": secid,
            "klt": int(klt),
            "fqt": 1,
            "lmt": int(count),
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56",
        }
    )
    payload = _curl_get_json(
        f"{EASTMONEY_KLINE}?{query}", referer="https://quote.eastmoney.com/"
    )
    body = payload.get("data")
    if not isinstance(body, dict):
        raise RuntimeError(f"Eastmoney returned no kline data for {secid}")
    rows = [
        [part.strip() for part in str(row).split(",")]
        for row in body.get("klines") or []
        if isinstance(row, str) and row
    ]
    prev_close = _float(body.get("preKPrice"))
    return rows, prev_close


def _fetch_eastmoney_daily_chart(
    secid: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    raw_rows, _ = _fetch_eastmoney_kline(secid, klt=101, count=count)
    parsed: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        bar = _parse_chart_bar(raw, intraday=False)
        if bar is not None:
            parsed[bar[0]["time"]] = bar[0]
    items = [parsed[key] for key in sorted(parsed)][-max(1, int(count)) :]
    if not items:
        raise RuntimeError(f"Eastmoney returned no valid daily bars for {secid}")
    return {
        "adjustment": "none",
        "period": "day",
        "items": items,
        "source": "东方财富日K",
    }


def _fetch_eastmoney_stock_daily_chart(
    symbol: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    """东财 push2his 日K跨源备援（腾讯 newfqkline 三主机全部失败时启用）。

    fqt=1 为前复权，与腾讯 newfqkline qfq 语义一致，可安全接替个股/指数
    日K；量价字段序与腾讯相同，直接复用 ``_parse_chart_bar``。来源显式
    标注"备援"，便于页面与日志区分。bj 代码东财 secid 未验证，不支持。
    """

    secid = _em_secid_for_symbol(symbol)
    if secid is None:
        raise RuntimeError(f"eastmoney daily fallback only covers sh/sz symbols: {symbol}")
    daily = _fetch_eastmoney_daily_chart(secid, count)
    daily["adjustment"] = "qfq"
    daily["source"] = "东方财富前复权日K(备援)"
    return daily


def _fetch_cn_stock_daily_chart(
    symbol: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    """A股个股/指数日K主备线：腾讯 newfqkline 三主机 → 东财 push2his(fqt=1)。

    两级都是前复权口径；东财 push2his 遇 WAF 连接级封禁时同样失败，此时
    抛错由调用方按"无负面缓存"原则处理（恢复后下一次请求自动成功）。
    """

    try:
        result = _fetch_tencent_daily_chart(symbol, count)
    except Exception as exc:
        if _em_secid_for_symbol(symbol) is None:
            raise
        result = _fetch_eastmoney_stock_daily_chart(symbol, count)
        _record_data_source_chain(
            "cn_daily_kline", "backup",
            str(result.get("source") or "东方财富日K(备援)"), type(exc).__name__,
        )
        return result
    _record_data_source_chain(
        "cn_daily_kline", "primary", str(result.get("source") or "腾讯财经前复权日K")
    )
    return result


def _fetch_eastmoney_intraday_chart(
    secid: str,
    *,
    fallback_date: str | None = None,
    count: int = STOCK_CHART_MINUTE_COUNT,
) -> dict[str, Any]:
    raw_rows, prev_close = _fetch_eastmoney_kline(secid, klt=5, count=count)
    parsed: dict[str, tuple[dict[str, Any], str]] = {}
    for raw in raw_rows:
        bar = _parse_chart_bar(raw, intraday=True, fallback_date=fallback_date)
        if bar is not None:
            parsed[bar[0]["time"]] = bar
    if not parsed:
        raise RuntimeError(f"Eastmoney returned no valid five-minute bars for {secid}")
    latest_trade_date = max(trade_date for _, trade_date in parsed.values())
    items = [
        bar
        for key, (bar, trade_date) in sorted(parsed.items())
        if trade_date == latest_trade_date
    ]
    return {
        "trade_date": latest_trade_date,
        "period": "m5",
        "prev_close": prev_close if prev_close is not None and prev_close > 0 else None,
        "items": items,
        "source": "东方财富5分钟K线",
    }


def _stock_chart_intraday_ttl_seconds(at: datetime | None = None) -> float:
    phase = str(_background_refresh_profile(at).get("phase") or "overnight")
    if phase in {"open", "auction"}:
        return 15.0
    if phase in {"pre_open", "lunch"}:
        return 60.0
    if phase == "post_close":
        return 300.0
    # 休市/隔夜：K线与分时在下一交易时段前不会变化，直接缓存到开盘前。
    seconds = _seconds_until_a_share_open(at)
    return max(900.0, min(seconds - 300.0, 12 * 3600.0))


def _seconds_until_a_share_open(at: datetime | None = None) -> float:
    """距下一个 A 股交易时段（09:15 集合竞价前）的秒数，用本地交易日历。"""

    observed = at or _now()
    for offset in range(0, 10):
        day = observed.date() + timedelta(days=offset)
        is_session, _ = _a_share_session_date(day)
        if not is_session:
            continue
        open_dt = datetime.combine(day, daytime(9, 15), tzinfo=CN_TZ)
        if open_dt <= observed:
            continue
        return (open_dt - observed).total_seconds()
    return 12 * 3600.0


# 全球指数的本地常规交易时段（节假日按周末近似：外来休市日多一次刷新，
# 数据不变、无副作用）。外盘期货/外汇近似 24×5，仅周末闭市。
GLOBAL_CHART_SESSIONS: dict[str, dict[str, Any]] = {
    "hkHSI": {
        "tz": "Asia/Hong_Kong",
        "windows": ((daytime(9, 30), daytime(12, 0)), (daytime(13, 0), daytime(16, 10))),
    },
    "usINX": {"tz": "America/New_York", "windows": ((daytime(9, 30), daytime(16, 10)),)},
    "usIXIC": {"tz": "America/New_York", "windows": ((daytime(9, 30), daytime(16, 10)),)},
    "usDJI": {"tz": "America/New_York", "windows": ((daytime(9, 30), daytime(16, 10)),)},
    "b_NKY": {
        "tz": "Asia/Tokyo",
        "windows": ((daytime(9, 0), daytime(11, 30)), (daytime(12, 30), daytime(15, 10))),
    },
    "b_KOSPI": {"tz": "Asia/Seoul", "windows": ((daytime(9, 0), daytime(15, 30)),)},
}
# 外盘期货/外汇/美债收益率近似 24×5，仅周末闭市。
GLOBAL_CHART_CONTINUOUS = {"hf_ES", "hf_NQ", "hf_CL", "hf_GC", "DINIW", "fx_susdcnh", "US10Y"}

# 隔夜全球标的的图表缓存时段：美/中概股按纽约时段，欧股指数按当地时段，
# SK海力士按首尔时段，COMEX白银近似 24×5。仅影响缓存 TTL，不影响数据。
for _overnight_item in EM_OVERNIGHT_INSTRUMENTS:
    _overnight_symbol = _overnight_item["symbol"]
    if _overnight_item.get("group") == "欧股指数":
        GLOBAL_CHART_SESSIONS[_overnight_symbol] = {
            "tz": "Europe/London" if _overnight_symbol == "b_FTSE" else "Europe/Paris",
            "windows": ((daytime(8, 0), daytime(16, 35)),),
        }
    elif _overnight_symbol == "b_SKHYNIX":
        GLOBAL_CHART_SESSIONS[_overnight_symbol] = {
            "tz": "Asia/Seoul",
            "windows": ((daytime(9, 0), daytime(15, 30)),),
        }
    elif str(_overnight_item.get("em_secid") or "").split(".", 1)[0] in {"105", "106", "153"}:
        GLOBAL_CHART_SESSIONS[_overnight_symbol] = {
            "tz": "America/New_York",
            "windows": ((daytime(9, 30), daytime(16, 10)),),
        }
    else:
        GLOBAL_CHART_CONTINUOUS.add(_overnight_symbol)
CHART_GLOBAL_OPEN_TTL_SECONDS = 60.0
CHART_CLOSED_PRE_OPEN_BUFFER_SECONDS = 300.0
CHART_CLOSED_MIN_TTL_SECONDS = 900.0
CHART_CLOSED_MAX_TTL_SECONDS = 12 * 3600.0


def _global_index_session(
    thscode: str, at: datetime | None = None
) -> tuple[bool, float]:
    """(是否处于本地交易时段, 距下一开盘的秒数；交易时段内为 0)。"""

    session = GLOBAL_CHART_SESSIONS.get(thscode)
    if session is None:
        if thscode in GLOBAL_CHART_CONTINUOUS:
            # 近 24×5 市场：仅周末闭市（CME 周六 05:00 CT 收、周日 17:00 CT 开，
            # 对应北京时间周六 19:00 收、周一 06:00 开，按 CST 近似）。
            now_cn = at or _now()
            weekday = now_cn.weekday()
            if weekday == 5:
                return False, _seconds_until_weekday_at(6, 0, now_cn)
            if weekday == 6:
                return False, _seconds_until_weekday_at(6, 0, now_cn)
            return True, 0.0
        return False, 0.0
    tz = ZoneInfo(str(session["tz"]))
    observed = (at or _now()).astimezone(tz)
    windows = session["windows"]
    first_open = windows[0][0]
    if observed.weekday() >= 5:
        return False, _seconds_until_weekday_at(first_open.hour, first_open.minute, observed, tz)
    for start, end in windows:
        if start <= observed.time() < end:
            return True, 0.0
    candidates = []
    for offset in (0, 1, 2, 3, 4, 5, 6, 7):
        day = observed.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for start, _end in windows:
            open_dt = datetime.combine(day, start, tzinfo=tz)
            if open_dt > observed:
                candidates.append((open_dt - observed).total_seconds())
    return False, (min(candidates) if candidates else CHART_CLOSED_MAX_TTL_SECONDS)


def _seconds_until_weekday_at(
    hour: int, minute: int, observed: datetime, tz: Any = None
) -> float:
    for offset in range(0, 8):
        day = observed.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        target = datetime.combine(day, daytime(hour, minute), tzinfo=tz or CN_TZ)
        if target <= observed:
            continue
        return (target - observed).total_seconds()
    return CHART_CLOSED_MAX_TTL_SECONDS


def _chart_ttl_for_instrument(instrument: dict[str, Any]) -> float:
    """该品种当前应使用的图表缓存 TTL（秒）。

    交易时段内用短 TTL；各自非交易时段用"距下次开盘"的长 TTL——数据在
    闭市期间不会变化，无需向上游发起新的查询。
    """

    thscode = str(instrument.get("thscode") or "")
    if instrument.get("instrument_type") == "index" and (
        thscode in GLOBAL_CHART_SESSIONS or thscode in GLOBAL_CHART_CONTINUOUS
    ):
        in_session, next_open = _global_index_session(thscode)
        if in_session:
            return CHART_GLOBAL_OPEN_TTL_SECONDS
        ttl = next_open - CHART_CLOSED_PRE_OPEN_BUFFER_SECONDS
        return max(CHART_CLOSED_MIN_TTL_SECONDS, min(ttl, CHART_CLOSED_MAX_TTL_SECONDS))
    return _stock_chart_intraday_ttl_seconds()


def _cached_position_prev_close(thscode: str) -> float | None:
    with QUOTE_LOCK:
        payload = QUOTE_CACHE.get("payload")
        positions = payload.get("positions") if isinstance(payload, dict) else []
        for item in positions if isinstance(positions, list) else []:
            if str(item.get("thscode") or "").upper() == thscode:
                value = _float(item.get("prev_close"))
                return value if value is not None and value > 0 else None
    return None


def _infer_chart_prev_close(
    thscode: str, daily: dict[str, Any], intraday: dict[str, Any]
) -> float | None:
    direct = _float(intraday.get("prev_close"))
    if direct is not None and direct > 0:
        return direct
    trade_date = str(intraday.get("trade_date") or "")
    candidates = [
        item
        for item in daily.get("items") or []
        if isinstance(item, dict) and str(item.get("time") or "") < trade_date
    ]
    if candidates:
        value = _float(candidates[-1].get("close"))
        if value is not None and value > 0:
            return value
    return _cached_position_prev_close(thscode)


def _trim_stock_chart_cache_locked() -> None:
    while len(STOCK_CHART_CACHE) > STOCK_CHART_MAX_CACHE_ENTRIES:
        oldest = next(iter(STOCK_CHART_CACHE))
        STOCK_CHART_CACHE.pop(oldest, None)


def _stock_chart_payload_from_entry(
    instrument: dict[str, Any],
    entry: dict[str, Any],
    *,
    now_monotonic: float,
    warnings: list[str] | None = None,
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    daily = copy.deepcopy(entry.get("daily") or {"items": [], "period": "day", "adjustment": "qfq"})
    intraday = copy.deepcopy(entry.get("intraday") or {"items": [], "period": "m5"})
    daily_age = max(0.0, now_monotonic - float(entry.get("daily_monotonic") or 0.0))
    intraday_age = max(0.0, now_monotonic - float(entry.get("intraday_monotonic") or 0.0))
    intraday_ttl = _chart_ttl_for_instrument(instrument)
    daily_stale = not daily.get("items") or daily_age > STOCK_CHART_DAILY_TTL_SECONDS
    intraday_stale = not intraday.get("items") or intraday_age > intraday_ttl
    messages = list(dict.fromkeys(str(value) for value in (warnings or []) if value))
    if daily.get("items") and daily_stale:
        messages.append("日K刷新失败，当前显示最近一次缓存")
    if intraday.get("items") and intraday_stale:
        messages.append("分时刷新失败，当前显示最近一次缓存")
    # 面板从未成功抓取时没有"最近缓存"可展示，把最后一次失败原因透出，
    # 避免空面板在退避窗口内看起来像无解释的空白。
    if not daily.get("items") and entry.get("daily_fail_warning"):
        messages.append(str(entry["daily_fail_warning"]))
    if not intraday.get("items") and entry.get("intraday_fail_warning"):
        messages.append(str(entry["intraday_fail_warning"]))
    intraday["prev_close"] = _infer_chart_prev_close(instrument["thscode"], daily, intraday)
    has_daily = bool(daily.get("items"))
    has_intraday = bool(intraday.get("items"))
    status = "healthy" if has_daily and has_intraday and not (daily_stale or intraday_stale) else "partial" if (has_daily or has_intraday) else "unavailable"

    indicator_payload: dict[str, Any] | None = None
    if has_daily and indicators is not None:
        try:
            indicator_payload = technical_indicators_module.compute_indicators(
                daily["items"], indicators or None
            )
        except Exception as exc:
            messages.append(f"指标计算失败：{type(exc).__name__}")
            indicator_payload = None

    deduped_messages = list(dict.fromkeys(messages))
    # ok=false 时把原因放进 error，前端 fetchJson 会优先展示它而不是"HTTP 503"。
    return {
        "ok": bool(has_daily or has_intraday),
        "schema_version": "stock-chart.api.v1",
        "status": status,
        "symbol": instrument["thscode"],
        "name": instrument["name"],
        "instrument_type": instrument["instrument_type"],
        "generated_at": _iso_now(),
        "source": str(daily.get("source") or "腾讯财经"),
        "kline": daily,
        "intraday": intraday,
        "indicators": indicator_payload,
        "error": "；".join(deduped_messages) if not (has_daily or has_intraday) else None,
        "warnings": deduped_messages,
        "cache": {
            "daily_age_seconds": round(daily_age, 1) if has_daily else None,
            "daily_ttl_seconds": STOCK_CHART_DAILY_TTL_SECONDS,
            "daily_stale": daily_stale,
            "intraday_age_seconds": round(intraday_age, 1) if has_intraday else None,
            "intraday_ttl_seconds": intraday_ttl,
            "intraday_stale": intraday_stale,
            "max_entries": STOCK_CHART_MAX_CACHE_ENTRIES,
        },
    }


def _parse_naver_daily_bars(xml_text: str, count: int) -> dict[str, Any]:
    """解析 Naver fchart 日K（行序 date|open|high|low|close|volume，EUC-KR 已解码）。"""
    parsed: dict[str, dict[str, Any]] = {}
    for raw in re.findall(r'<item data="([^"]+)"', xml_text):
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 6:
            continue
        bar_time = _canonical_daily_bar_time(parts[0])
        if bar_time is None:
            continue
        open_, high, low, close = (_float(value) for value in parts[1:5])
        volume = _float(parts[5])
        if any(value is None or value <= 0 for value in (open_, high, low, close)):
            continue
        if high + 1e-9 < max(open_, close) or low - 1e-9 > min(open_, close) or high < low:
            continue
        parsed[bar_time] = {
            "time": bar_time,
            "open": open_,
            "close": close,
            "high": high,
            "low": low,
            "volume": volume if volume is not None and volume >= 0 else None,
        }
    items = [parsed[key] for key in sorted(parsed)][-max(1, int(count)) :]
    if not items:
        raise RuntimeError("Naver returned no valid daily bars")
    return {
        "adjustment": "none",
        "period": "day",
        "items": items,
        "source": "Naver财经日K",
    }


def _fetch_naver_daily_chart(
    symbol: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    """Naver 财经日K兜底：海外指数（如 b_KOSPI）在东财被 WAF 拦截时启用。

    fchart 接口为普通 HTTP/1.1，无 WAF 问题；仅提供日K（Naver 无指数分时）。
    """
    response = LIVE_SESSION.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={
            "symbol": symbol,
            "timeframe": "day",
            "count": int(count),
            "requestType": "0",
        },
        headers={"User-Agent": UA, "Referer": "https://finance.naver.com/"},
        timeout=(2.5, 6.0),
    )
    response.raise_for_status()
    return _parse_naver_daily_bars(response.content.decode("euc-kr", errors="replace"), count)


SINA_GLOBAL_FUTURES_DAILY = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
    "/var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine"
)


def _fetch_sina_global_futures_daily_chart(
    sina_symbol: str, count: int = STOCK_CHART_DAILY_COUNT
) -> dict[str, Any]:
    """新浪全球期货日K：hf_ES/hf_NQ 等外盘连续合约的图表源。

    东财 push2his 对 CME 小型期货（103.ES00Y/103.NQ00Y）不返回K线，而这些
    品种的实时行情本就来自新浪 hf_*，日K沿用同一符号体系。接口返回
    JSONP 包装的完整历史，按腾讯字段序（date, open, close, high, low,
    volume）截尾返回；无分时源，调用方按"分时暂不可用"降级。
    """

    response = LIVE_SESSION.get(
        SINA_GLOBAL_FUTURES_DAILY,
        params={"symbol": sina_symbol},
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=(2.5, 8.0),
    )
    response.raise_for_status()
    text = response.text
    json_start = text.find("(")
    json_end = text.rfind(")")
    if json_start < 0 or json_end <= json_start:
        raise RuntimeError(f"Sina global futures returned no JSON for {sina_symbol}")
    rows = json.loads(text[json_start + 1 : json_end])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Sina global futures returned no daily bars for {sina_symbol}")
    parsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        trade_date = _canonical_daily_bar_time(row.get("date"))
        open_ = _float(row.get("open"))
        close = _float(row.get("close"))
        high = _float(row.get("high"))
        low = _float(row.get("low"))
        if not trade_date or not close or not open_ or not high or not low:
            continue
        bar = _parse_chart_bar(
            [trade_date, open_, close, high, low, _float(row.get("volume")) or 0],
            intraday=False,
        )
        if bar is not None:
            parsed[bar[0]["time"]] = bar[0]
    items = [parsed[key] for key in sorted(parsed)][-max(1, int(count)) :]
    if not items:
        raise RuntimeError(f"Sina global futures returned no valid daily bars for {sina_symbol}")
    return {
        "adjustment": "none",
        "period": "day",
        "items": items,
        "source": "新浪全球期货日K",
    }


def _fetch_chart_daily(instrument: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the daily chart fetch by instrument kind.

    Whitelisted indices on Tencent reuse the A-share fetchers unchanged (the
    response carries plain ``day`` rows); indices on Eastmoney use their secid.
    Index points have no adjustment concept, so the flag is reset to ``none``
    instead of the stock default ``qfq``.
    """

    if str(instrument.get("chart_kind") or "") == "naver":
        # SK海力士等东财不覆盖品种：Naver fchart 日K（无分时源）。
        daily = _fetch_naver_daily_chart(
            str(instrument.get("naver_symbol") or instrument["symbol"])
        )
    elif str(instrument.get("chart_kind") or "") == "eastmoney":
        try:
            daily = _fetch_eastmoney_daily_chart(instrument["secid"])
        except Exception:
            # 东财被 WAF 拦截等场景：白名单里配置了 naver_symbol 的指数
            # （如 b_KOSPI）回落到 Naver 日K兜底；无兜底配置则照常报错。
            naver_symbol = str(instrument.get("naver_symbol") or "")
            if not naver_symbol:
                raise
            daily = _fetch_naver_daily_chart(naver_symbol)
    elif str(instrument.get("chart_kind") or "") == "sina_futures":
        daily = _fetch_sina_global_futures_daily_chart(str(instrument["sina_symbol"]))
    elif str(instrument.get("chart_kind") or "") == "tencent_us":
        daily = _fetch_tencent_us_index_daily_chart(instrument["symbol"])
    else:
        # A股个股/指数日K主备线：腾讯 newfqkline 三主机 → 东财 push2his(fqt=1)。
        daily = _fetch_cn_stock_daily_chart(instrument["symbol"])
    if instrument.get("instrument_type") == "index":
        daily["adjustment"] = "none"
        daily["source"] = str(daily.get("source") or "腾讯财经").replace("前复权", "")
    return daily


def _fetch_chart_intraday(
    instrument: dict[str, Any], *, fallback_date: str | None
) -> dict[str, Any]:
    kind = str(instrument.get("chart_kind") or "")
    if kind == "naver":
        raise RuntimeError("Naver 无分时数据源")
    if kind == "eastmoney":
        return _fetch_eastmoney_intraday_chart(
            instrument["secid"], fallback_date=fallback_date
        )
    if kind == "sina_futures":
        raise RuntimeError("新浪全球期货无分时数据源")
    if kind in {"tencent_hk", "tencent_us"}:
        return _fetch_tencent_global_intraday_chart(
            instrument["symbol"], fallback_date=fallback_date
        )
    try:
        chart = _fetch_tencent_intraday_chart(instrument["symbol"], fallback_date=fallback_date)
    except Exception as exc:
        # 腾讯 mkline 双主机失败（WAF/限流）→ 东财 trends2 分时备源（仅覆盖 sh/sz）。
        if _em_secid_for_symbol(instrument["symbol"]) is None:
            raise
        chart = _fetch_em_trends2_intraday(instrument["symbol"], fallback_date=fallback_date)
        _record_data_source_chain(
            "cn_intraday", "backup",
            str(chart.get("source") or "东方财富分时(备援)"), type(exc).__name__,
        )
        return chart
    _record_data_source_chain(
        "cn_intraday", "primary", str(chart.get("source") or "腾讯财经5分钟K线")
    )
    return chart


def get_stock_chart_payload(
    raw_symbol: str,
    *,
    force: bool = False,
    indicators: list[str] | None = None,
) -> dict[str, Any]:
    instrument = _resolve_portfolio_chart_instrument(raw_symbol)
    thscode = instrument["thscode"]

    def _ttls(now: float) -> tuple[float, float]:
        intraday_ttl = _chart_ttl_for_instrument(instrument)
        daily_ttl = intraday_ttl if intraday_ttl > 900.0 else STOCK_CHART_DAILY_TTL_SECONDS
        return daily_ttl, intraday_ttl

    def _panel_fresh(entry: dict[str, Any], panel: str, ttl: float, now: float) -> bool:
        if entry.get(panel) is not None:
            return now - float(entry.get(f"{panel}_monotonic") or 0.0) <= ttl
        # 上一次抓取失败的面板按短回退退避，闭市期间同样不再反复请求。
        fail_at = entry.get(f"{panel}_fail_monotonic")
        return fail_at is not None and now - float(fail_at) <= min(ttl, 300.0)

    now_monotonic = time.monotonic()
    daily_ttl, intraday_ttl = _ttls(now_monotonic)

    def _fresh(entry: dict[str, Any]) -> bool:
        return _panel_fresh(entry, "daily", daily_ttl, now_monotonic) and _panel_fresh(
            entry, "intraday", intraday_ttl, now_monotonic
        )

    with STOCK_CHART_LOCK:
        cached = STOCK_CHART_CACHE.get(thscode)
        if cached is not None and not force and _fresh(cached):
            STOCK_CHART_CACHE.pop(thscode, None)
            STOCK_CHART_CACHE[thscode] = cached
            return _stock_chart_payload_from_entry(
                instrument, cached, now_monotonic=now_monotonic, indicators=indicators
            )

    with STOCK_CHART_BUILD_LOCK:
        now_monotonic = time.monotonic()
        daily_ttl, intraday_ttl = _ttls(now_monotonic)
        with STOCK_CHART_LOCK:
            entry = copy.deepcopy(STOCK_CHART_CACHE.get(thscode) or {})
            if entry and not force and _fresh(entry):
                return _stock_chart_payload_from_entry(
                    instrument, entry, now_monotonic=now_monotonic, indicators=indicators
                )

        warnings: list[str] = []
        if force or not _panel_fresh(entry, "daily", daily_ttl, now_monotonic):
            try:
                entry["daily"] = _fetch_chart_daily(instrument)
                entry["daily_monotonic"] = time.monotonic()
                entry["daily_fetched_at"] = _iso_now()
                entry.pop("daily_fail_monotonic", None)
            except Exception as exc:
                entry["daily_fail_monotonic"] = time.monotonic()
                entry["daily_fail_warning"] = f"日K暂不可用：{type(exc).__name__}"
                warnings.append(f"日K暂不可用：{type(exc).__name__}")

        daily_items = (entry.get("daily") or {}).get("items") or []
        fallback_date = str(daily_items[-1].get("time") or "") if daily_items else None
        if force or not _panel_fresh(entry, "intraday", intraday_ttl, now_monotonic):
            try:
                entry["intraday"] = _fetch_chart_intraday(
                    instrument, fallback_date=fallback_date
                )
                entry["intraday_monotonic"] = time.monotonic()
                entry["intraday_fetched_at"] = _iso_now()
                entry.pop("intraday_fail_monotonic", None)
            except Exception as exc:
                entry["intraday_fail_monotonic"] = time.monotonic()
                entry["intraday_fail_warning"] = f"分时暂不可用：{type(exc).__name__}"
                warnings.append(f"分时暂不可用：{type(exc).__name__}")

        entry["last_access_monotonic"] = time.monotonic()
        with STOCK_CHART_LOCK:
            STOCK_CHART_CACHE.pop(thscode, None)
            STOCK_CHART_CACHE[thscode] = copy.deepcopy(entry)
            _trim_stock_chart_cache_locked()
        return _stock_chart_payload_from_entry(
            instrument,
            entry,
            now_monotonic=time.monotonic(),
            warnings=warnings,
            indicators=indicators,
        )


def _suggest_price_lines(daily_bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """系统建议的自设价格线（止损/加仓/止盈），ATR14 波动锚定。

    * 止损 = 现价 − 2.5×ATR14：跌破视为短期波动结构破坏；
    * 加仓 = 现价 − 1.2×ATR14：回撤约一天半波动的低吸参考；
      若 20 日均线落在该位与现价之间，改贴 MA20 下方（回踩均线加仓）；
    * 止盈 = 现价 + 3×ATR14：约 3 天波动幅度的动量目标。
    只使用 ≤ 最新收盘的数据；规则参考，不构成投资建议。
    """

    if len(daily_bars) < 21:
        return None
    closes: list[float] = []
    for bar in daily_bars:
        value = _float(bar.get("close"))
        if value and value > 0:
            closes.append(value)
    if len(closes) < 21:
        return None
    price = closes[-1]
    trs: list[float] = []
    window = daily_bars[-15:]
    for prev, bar in zip(window[:-1], window[1:]):
        high = _float(bar.get("high"))
        low = _float(bar.get("low"))
        prev_close = _float(prev.get("close"))
        if not high or not low or not prev_close:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < 10:
        return None
    atr = sum(trs) / len(trs)
    if atr <= 0:
        return None
    ma20 = sum(closes[-20:]) / 20.0

    stop = price - 2.5 * atr
    add = price - 1.2 * atr
    add_basis = "现价−1.2×ATR14"
    if price * 0.9 <= ma20 < price and ma20 * 0.995 > add:
        add = ma20 * 0.995
        add_basis = f"回踩MA20({ma20:.2f})下方"
    add = min(add, price * 0.99)
    take = price + 3.0 * atr
    if not (0 < stop < add < price < take):
        return None

    return {
        "stop": round(stop, 2),
        "buy": round(add, 2),
        "sell": round(take, 2),
        "atr14": round(atr, 3),
        "ma20": round(ma20, 2),
        "basis": [
            f"止损=现价−2.5×ATR14（ATR14≈{atr:.2f}，跌破视为波动结构破坏）",
            f"加仓={add_basis}（低吸参考）",
            "止盈=现价+3×ATR14（动量目标位）",
        ],
        "generated_at": _iso_now(),
        "disclaimer": "规则建议，不构成投资建议",
    }


def get_technical_signals_payload(raw_symbol: str) -> dict[str, Any]:
    """Evaluate technical-indicator signals for a held/watchlist symbol.

    Reuses the cached daily K-line (or fetches on miss) and runs the signal
    engine on the same data so callers always see signals consistent with the
    chart they are viewing.
    """

    instrument = _resolve_portfolio_chart_instrument(raw_symbol)
    thscode = instrument["thscode"]
    # Pull from the chart cache when warm; otherwise fetch fresh.
    with STOCK_CHART_LOCK:
        cached = STOCK_CHART_CACHE.get(thscode)
        daily_bars = (cached.get("daily") or {}).get("items") if cached else None

    if not daily_bars:
        try:
            daily = _fetch_cn_stock_daily_chart(instrument["symbol"])
            daily_bars = daily.get("items") or []
        except Exception as exc:
            return {
                "ok": False,
                "error": f"日K数据不可用：{type(exc).__name__}",
                "symbol": thscode,
            }

    try:
        result = technical_signal_engine_module.evaluate_signals(daily_bars)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"信号计算失败：{type(exc).__name__}: {exc}",
            "symbol": thscode,
        }

    return {
        "ok": True,
        "schema_version": "technical-signals.api.v1",
        "symbol": thscode,
        "name": instrument["name"],
        "generated_at": _iso_now(),
        "bar_count": result["bar_count"],
        "latest_close": result["latest_close"],
        "signals": result["signals"],
        "summary": result["summary"],
        "engine_version": result["engine_version"],
        "suggested_lines": _suggest_price_lines(daily_bars),
    }


def _tomorrow_plan_instruments() -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """全部组合持仓并集 → (instrument, 日K bars)。优先取图表缓存。"""
    rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for entry in _union_configured_positions():
        thscode = str(entry.get("thscode") or "").upper()
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", thscode):
            continue
        instrument = {
            "thscode": thscode,
            "symbol": str(entry.get("symbol") or ""),
            "name": str(entry.get("name") or thscode),
            "instrument_type": str(entry.get("instrument_type") or "stock"),
        }
        bars: list[dict[str, Any]] | None = None
        with STOCK_CHART_LOCK:
            cached = STOCK_CHART_CACHE.get(thscode)
            if cached:
                bars = (cached.get("daily") or {}).get("items") or None
        if not bars:
            try:
                bars = _fetch_chart_daily(instrument).get("items") or []
            except Exception:
                bars = []
        if bars:
            rows.append((instrument, list(bars)))
    return rows


def _tomorrow_backtest_payload(
    bars_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    force: bool,
    fund_flow_by_symbol: dict[str, dict[str, float]] | None = None,
    market_bars: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """回测汇总（6 小时缓存，按模型版本/标的/条数/最新日期签名失效）。"""
    signature = "|".join(
        f"{nextday_model_module.MODEL_VERSION}:{symbol}:{len(bars)}:{bars[-1].get('time')}"
        for symbol, bars in sorted(bars_by_symbol.items())
    )
    if fund_flow_by_symbol:
        signature += "|flow:" + "|".join(
            f"{symbol}:{len(flow)}"
            for symbol, flow in sorted(fund_flow_by_symbol.items())
        )
    cache = _TOMORROW_BACKTEST_CACHE
    if (
        not force
        and cache["payload"] is not None
        and cache["signature"] == signature
        and time.monotonic() - float(cache["monotonic"]) < TOMORROW_BACKTEST_TTL_SECONDS
    ):
        return cache["payload"]
    payload = nextday_backtest_module.grid_search(
        bars_by_symbol, fund_flow_by_symbol=fund_flow_by_symbol,
        market_bars=market_bars,
    )
    symbol_strategies = {
        symbol: nextday_backtest_module.optimize_symbol(bars)
        for symbol, bars in sorted(bars_by_symbol.items())
    }
    payload["symbol_strategies"] = symbol_strategies
    cache.update(payload=payload, monotonic=time.monotonic(), signature=signature)
    return payload


def _backfill_market_intel_flow_today(failed_thscodes: list[str], now_mono: float) -> None:
    """push2 fflow 封禁期的当日资金流兜底（datacenter 当日报表 + 腾讯成交额换算）。

    datacenter RPT_DMSK_TS_STOCKNEW 只有当日主力净额（元），主力净占比需除以
    当日成交额——用腾讯行情的 amount_yi 换算（腾讯不封 IP，一次批量请求）。
    成功写入的条目带 partial 标记，按 10 分钟短 TTL 过期（当日值盘中会变）。
    """

    code_to_thscode = {
        str(thscode).split(".")[0]: str(thscode) for thscode in failed_thscodes
    }
    try:
        today_flow = market_intel_module.fetch_stock_fund_flow_today(sorted(code_to_thscode))
    except Exception:
        return
    if not today_flow:
        return
    tencent_symbols = [
        ("sh" if code.startswith(("6", "5", "9")) else "sz") + code
        for code in today_flow
    ]
    amounts: dict[str, float | None] = {}
    try:
        amount_quotes = _fetch_tencent(tencent_symbols)
        amounts = {
            str(symbol)[2:]: quote.get("amount_yi")
            for symbol, quote in amount_quotes.items()
        }
    except Exception:
        amounts = {}
    for code, info in today_flow.items():
        thscode = code_to_thscode.get(code)
        amount_yi = amounts.get(code)
        if thscode is None or not amount_yi:
            continue
        main_pct = round(float(info["main_net_yi"]) / float(amount_yi) * 100, 3)
        _MARKET_INTEL_FLOW_STORE[thscode] = {
            "monotonic": now_mono,
            "partial": True,
            "flow": {str(info["date"]): main_pct},
        }


def _market_intel_payload(
    bars_by_symbol: dict[str, list[dict[str, Any]]], *, force: bool
) -> dict[str, Any]:
    """资金面/事件日历采集：渐进式逐股补采 + 大盘项低频缓存。

    EM 按 IP+主机 封禁突发（实测触发阈值很低），因此：
    * 个股资金流每只独立缓存（成功 6 小时，失败 60 秒后重试），
      每轮最多补采 MARKET_INTEL_FETCH_BUDGET 只，覆盖率随轮次自动爬升；
    * 两融/行业/解禁/宏观为大盘项，2 小时缓存；
    * 全部失败都降级为 None，绝不阻塞计划生成。
    """
    now_mono = time.monotonic()
    fetched = 0
    failed_codes: list[str] = []
    for thscode in sorted(bars_by_symbol):
        if fetched >= MARKET_INTEL_FETCH_BUDGET:
            break
        entry = _MARKET_INTEL_FLOW_STORE.get(thscode)
        entry_ttl = (
            MARKET_INTEL_TODAY_TTL_SECONDS
            if isinstance(entry, dict) and entry.get("partial")
            else MARKET_INTEL_FLOW_TTL_SECONDS
        )
        if entry and now_mono - float(entry["monotonic"]) < entry_ttl:
            continue
        code = thscode.split(".")[0]
        try:
            flow = market_intel_module.fetch_stock_fund_flow_daily(code)
            _MARKET_INTEL_FLOW_STORE[thscode] = {
                "monotonic": now_mono,
                "flow": {
                    str(row.get("date")): float(row.get("main_pct"))
                    for row in flow.get("rows") or []
                    if row.get("main_pct") is not None
                },
            }
        except Exception:
            # push2 fflow 失败（封禁/接口异常）：60 秒后重试主源，
            # 本轮先记入失败清单，循环结束后用 datacenter 当日报表兜底。
            _MARKET_INTEL_FLOW_STORE[thscode] = {
                "monotonic": now_mono - MARKET_INTEL_FLOW_TTL_SECONDS + MARKET_INTEL_RETRY_SECONDS,
                "flow": {},
            }
            failed_codes.append(thscode)
        fetched += 1
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)

    if failed_codes:
        _backfill_market_intel_flow_today(failed_codes, now_mono)

    fund_flow_by_symbol = {
        thscode: entry["flow"]
        for thscode, entry in _MARKET_INTEL_FLOW_STORE.items()
        if entry.get("flow") and thscode in bars_by_symbol
    }

    level_cache = _MARKET_LEVEL_CACHE
    if force or not level_cache["payload"] or (
        now_mono - float(level_cache["monotonic"]) >= MARKET_LEVEL_TTL_SECONDS
    ):
        margin: dict[str, Any] | None = None
        industry: dict[str, Any] | None = None
        unlock: dict[str, Any] | None = None
        macro: dict[str, Any] | None = None
        today = datetime.now(CN_TZ).date()
        horizon_end = (today + timedelta(days=30)).isoformat()
        try:
            margin = market_intel_module.fetch_margin_summary()
        except Exception:
            margin = None
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)
        try:
            industry = market_intel_module.fetch_industry_flow_rank(top=8)
        except Exception:
            # push2 clist 失败（封禁期）→ datacenter 板块资金流备源（支持当日）。
            try:
                industry = market_intel_module.fetch_industry_flow_rank_datacenter(top=8)
            except Exception:
                industry = None
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)
        try:
            unlock = market_intel_module.fetch_unlock_calendar(
                today.isoformat(),
                horizon_end,
                codes=[thscode.split(".")[0] for thscode in bars_by_symbol],
            )
        except Exception:
            unlock = None
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)
        try:
            macro = market_intel_module.fetch_cpi_snapshot()
        except Exception:
            macro = None
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)
        holding_codes = [thscode.split(".")[0] for thscode in bars_by_symbol]
        try:
            earnings_predict = market_intel_module.fetch_earnings_predict(holding_codes)
        except Exception:
            earnings_predict = {}
        time.sleep(MARKET_INTEL_EM_PACING_SECONDS)
        try:
            earnings_reports = market_intel_module.fetch_earnings_reports(holding_codes)
        except Exception:
            earnings_reports = {}
        level_cache["payload"] = {
            "margin": margin, "industry": industry, "unlock": unlock, "macro": macro,
            "earnings_predict": earnings_predict,
            "earnings_reports": earnings_reports,
        }
        level_cache["monotonic"] = now_mono
    level_payload = level_cache["payload"]

    return {
        "ok": True,
        "generated_at": _iso_now(),
        "fund_flow_by_symbol": fund_flow_by_symbol,
        "fund_flow_symbols": len(fund_flow_by_symbol),
        "flow_store_size": len(_MARKET_INTEL_FLOW_STORE),
        "margin": level_payload.get("margin"),
        "industry": level_payload.get("industry"),
        "unlock": level_payload.get("unlock"),
        "macro": level_payload.get("macro"),
        "earnings_predict": level_payload.get("earnings_predict"),
        "earnings_reports": level_payload.get("earnings_reports"),
    }


def get_tomorrow_plans_payload(force: bool = False) -> dict[str, Any]:
    """全部持仓的次日做T计划 + 模型回测汇总（30 分钟缓存）。"""
    cache = _TOMORROW_PLANS_CACHE
    if (
        not force
        and cache["payload"] is not None
        and time.monotonic() - float(cache["monotonic"]) < TOMORROW_PLANS_TTL_SECONDS
    ):
        return cache["payload"]
    if not force and cache["payload"] is not None:
        # Stale-while-revalidate: the cold build runs for about a minute, far
        # past the browser fetch timeout, so serving the expired payload and
        # rebuilding in the background keeps the 做T参考 column populated.
        _spawn_maintenance("tomorrow_plans", _build_tomorrow_plans_payload)
        return cache["payload"]
    return _build_tomorrow_plans_payload(force=force)


def _build_tomorrow_plans_payload(force: bool = False) -> dict[str, Any]:
    with _TOMORROW_PLANS_BUILD_LOCK:
        cache = _TOMORROW_PLANS_CACHE
        if (
            cache["payload"] is not None
            and time.monotonic() - float(cache["monotonic"]) < TOMORROW_PLANS_TTL_SECONDS
        ):
            return cache["payload"]

        bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
        instruments: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for instrument, bars in _tomorrow_plan_instruments():
            thscode = instrument["thscode"]
            instruments[thscode] = instrument
            bars_by_symbol[thscode] = bars
        # 大盘预判：上证综指跑同一模型（次日倾向作为全部持仓的风险闸门）
        market_plan: dict[str, Any] | None = None
        market_bars: list[dict[str, Any]] | None = None
        try:
            with STOCK_CHART_LOCK:
                mcached = STOCK_CHART_CACHE.get("sh000001")
                market_bars = ((mcached or {}).get("daily") or {}).get("items") or None
            if not market_bars:
                market_bars = _fetch_chart_daily({
                    "thscode": "sh000001", "symbol": "sh000001",
                    "name": "上证综指", "instrument_type": "index",
                }).get("items") or []
            if market_bars:
                market_plan = nextday_model_module.compute_plan(market_bars)
        except Exception as exc:
            errors.append(f"market-plan: {type(exc).__name__}")
        market_bias = (market_plan or {}).get("bias")

        intel = _market_intel_payload(bars_by_symbol, force=force) if bars_by_symbol else {}
        fund_flow_by_symbol = intel.get("fund_flow_by_symbol") or {}
        backtest = (
            _tomorrow_backtest_payload(
                bars_by_symbol, force=force, fund_flow_by_symbol=fund_flow_by_symbol,
                market_bars=market_bars,
            )
            if bars_by_symbol
            else {}
        )
        # 逐股最优策略（样本内网格寻优；不达标者回落到全局聚合最优参数）
        symbol_strategies = backtest.get("symbol_strategies") or {}
        pooled_config = (backtest.get("best") or {}).get("config") or {}

        # 新闻情绪采集：复用市场新闻流的已有条目（财联社/东财/华尔街见闻/Google），
        # 按持仓标的聚合为情绪因子；每天落账一次供未来回测新闻因子。
        news_summary: dict[str, Any] = {"available": False}
        try:
            news_payload = get_market_news_payload()
            sentiment_now = datetime.now(CN_TZ)
            news_summary = news_sentiment_module.summarize(
                news_payload.get("items") or [],
                symbols=list(bars_by_symbol.keys()),
                now=sentiment_now,
            )
            market_sent = (news_summary.get("market") or {}).get("sentiment")
            log_rows = [{"date": sentiment_now.strftime("%Y-%m-%d"), "symbol": "MARKET",
                         "sentiment": market_sent,
                         "items_scored": news_summary.get("items_scored")}]
            for thscode, own in (news_summary.get("symbols") or {}).items():
                log_rows.append({
                    "date": sentiment_now.strftime("%Y-%m-%d"), "symbol": thscode,
                    "sentiment": own.get("sentiment"), "positive": own.get("positive"),
                    "negative": own.get("negative"),
                    "fallback_to_market": own.get("fallback_to_market"),
                })
            news_sentiment_module.append_sentiment_log(
                ROOT / "data" / "news-sentiment-log.jsonl", log_rows
            )
        except Exception as exc:
            errors.append(f"news-sentiment: {type(exc).__name__}")

        plans: dict[str, Any] = {}
        for thscode, bars in bars_by_symbol.items():
            instrument = instruments[thscode]
            strategy = symbol_strategies.get(thscode) or {}
            try:
                config = strategy.get("config") if strategy.get("ok") else None
                plan = nextday_model_module.compute_plan(
                    bars, config,
                    news=(news_summary.get("symbols") or {}).get(thscode),
                    fund_flow=fund_flow_by_symbol.get(thscode),
                    market_bias=market_bias,
                )
                # 次日预测分时 + 历史双触达最高胜率做T幅度（只读 ≤当日的 K 线）
                try:
                    plan["intraday_forecast"] = (
                        intraday_forecast_module.build_intraday_forecast(
                            bars,
                            bias=str(plan.get("bias") or ""),
                            score=float(plan.get("score") or 0.0),
                            atr14=plan.get("atr14"),
                        )
                    )
                except Exception as forecast_exc:
                    plan["intraday_forecast"] = {
                        "schema_version": intraday_forecast_module.FORECAST_SCHEMA_VERSION,
                        "ok": False,
                        "reason": f"{type(forecast_exc).__name__}",
                    }
                unlock_rows = (intel.get("unlock") or {}).get("rows") or []
                plan["unlock"] = [
                    row for row in unlock_rows if row.get("code") == thscode.split(".")[0]
                ][:2]
                code6 = thscode.split(".")[0]
                latest_report = (intel.get("earnings_reports") or {}).get(code6)
                next_window = (
                    market_intel_module.next_report_window(
                        latest_report.get("report_date", ""), datetime.now(CN_TZ).date().isoformat()
                    )
                    if latest_report else None
                )
                plan["earnings"] = {
                    "predict": (intel.get("earnings_predict") or {}).get(code6),
                    "latest_report": latest_report,
                    "next_window": next_window,
                }
            except Exception as exc:
                errors.append(f"{thscode}: {type(exc).__name__}")
                continue
            plan["thscode"] = thscode
            plan["name"] = instrument["name"]
            plan["strategy"] = {
                "source": "per-symbol" if strategy.get("ok") else "pooled",
                "config": strategy.get("config") or pooled_config,
            }
            plan["symbol_backtest"] = (
                strategy.get("summary") if strategy.get("ok")
                else (backtest.get("per_symbol") or {}).get(thscode)
            )
            plan["recent"] = strategy.get("recent")
            plan["recent_10"] = strategy.get("recent_10") if strategy.get("ok") else None
            plans[thscode] = plan
        payload = {
            "ok": bool(plans),
            "schema_version": "tomorrow-plans.api.v1",
            "model_version": nextday_model_module.MODEL_VERSION,
            "generated_at": _iso_now(),
            "plans": plans,
            "backtest": backtest,
            "news_market": news_summary.get("market"),
            "news_version": news_summary.get("version"),
            "market_plan": market_plan,
            "intel_market": {
                "margin": intel.get("margin"),
                "industry": intel.get("industry"),
                "macro": intel.get("macro"),
                "earnings_predict": intel.get("earnings_predict"),
                "earnings_reports": intel.get("earnings_reports"),
            },
            "intel_version": intel.get("generated_at"),
            "fund_flow_symbols": intel.get("fund_flow_symbols"),
            "errors": errors,
            "disclaimer": "次日做T参考由技术模型+新闻情绪生成，不构成投资建议；做T需自有底仓",
        }
        cache.update(payload=payload, monotonic=time.monotonic())
        return payload


def get_technical_signals_batch_payload(symbols: list[str]) -> dict[str, Any]:
    """Evaluate technical signals for many symbols concurrently.

    Per-symbol failures degrade to ``{"ok": False, "error": ...}`` so one bad
    ticker never blocks the rest of the portfolio.
    """

    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not symbols:
        return {
            "ok": True,
            "schema_version": "technical-signals-batch.api.v1",
            "generated_at": _iso_now(),
            "results": {},
        }

    unique_symbols = list(dict.fromkeys(str(s).strip() for s in symbols if str(s).strip()))
    results: dict[str, Any] = {}

    def _one(symbol: str) -> tuple[str, dict[str, Any]]:
        try:
            return symbol, get_technical_signals_payload(symbol)
        except Exception as exc:
            return symbol, {
                "ok": False,
                "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }

    # Cap workers to avoid hammering Tencent's K-line endpoint.
    with ThreadPoolExecutor(max_workers=min(6, len(unique_symbols)), thread_name_prefix="sig-batch") as pool:
        futures = {pool.submit(_one, sym): sym for sym in unique_symbols}
        for future in as_completed(futures):
            symbol, payload = future.result()
            results[symbol] = payload

    return {
        "ok": True,
        "schema_version": "technical-signals-batch.api.v1",
        "generated_at": _iso_now(),
        "results": results,
    }


def _fetch_school_signal_bars(tencent_symbol: str) -> list[dict[str, Any]]:
    """三大流派信号面板的日K数据：优先与图表同源的 newfqkline 主链路，失败再独立重取。

    上游接口偶发返回 200 但数据为空（限流特征），因此对两条链路各做最多 3 次
    带退避的重试，全部失败才抛错。
    """
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(0.6 * attempt)
        try:
            daily = _fetch_tencent_daily_chart(tencent_symbol)
            items = daily.get("items") or []
            if items:
                return items
        except Exception as exc:
            last_error = exc
        try:
            fallback_start, fallback_end = _tencent_daily_date_range(640)
            payload = _fetch_tencent_kline(
                "school",
                TENCENT_NEWFQ_KLINE_HOSTS,
                {"param": f"{tencent_symbol},day,{fallback_start},{fallback_end},640,qfq"},
                referer="https://gu.qq.com/",
                timeout=(2.5, 5.0),
            )
            body = (payload.get("data") or {}).get(tencent_symbol) or {}
            raw_rows = body.get("qfqday") or body.get("day") or []
            parsed: dict[str, dict[str, Any]] = {}
            for raw in raw_rows if isinstance(raw_rows, list) else []:
                bar = _parse_chart_bar(raw, intraday=False)
                if bar is not None:
                    parsed[bar[0]["time"]] = bar[0]
            items = [parsed[key] for key in sorted(parsed)]
            if items:
                return _merge_live_daily_bar(tencent_symbol, items)
            last_error = RuntimeError(f"empty daily bars for {tencent_symbol}")
        except Exception as exc:
            last_error = exc
    # 腾讯两条链路全部失败 → 东财 push2his(fqt=1) 跨源备援，口径同为前复权。
    try:
        daily = _fetch_eastmoney_stock_daily_chart(tencent_symbol, 640)
        items = daily.get("items") or []
        if items:
            return _merge_live_daily_bar(tencent_symbol, items)
    except Exception as exc:
        last_error = exc
    raise RuntimeError(f"no daily bars for {tencent_symbol}: {last_error}")


def _load_school_signal_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with SCHOOL_SIGNAL_LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("symbol") and obj.get("event"):
                    events.append(obj)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return events


def _append_school_signal_events(events: list[dict[str, Any]]) -> int:
    """把新提示事件追加进 jsonl；按 日期+股票+流派+事件 去重，同日不重复记录。"""
    if not events:
        return 0
    existing = _load_school_signal_events()
    seen = {(e.get("date"), e.get("symbol"), e.get("school"), e.get("event")) for e in existing}
    fresh: list[dict[str, Any]] = []
    for ev in events:
        key = (ev.get("date"), ev.get("symbol"), ev.get("school"), ev.get("event"))
        if key in seen:
            continue
        seen.add(key)
        item = dict(ev)
        item["logged_at"] = _iso_now()
        fresh.append(item)
    if not fresh:
        return 0
    SCHOOL_SIGNAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SCHOOL_SIGNAL_LOG_PATH.open("a", encoding="utf-8") as fh:
        for item in fresh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    return len(fresh)


def get_school_signals_payload(force: bool = False) -> dict[str, Any]:
    """三大流派信号面板主接口：5 分钟缓存，附提示日志与正确率回溯。"""
    now = time.monotonic()
    with SCHOOL_SIGNAL_LOCK:
        cached = SCHOOL_SIGNAL_CACHE.get("payload")
        ts = float(SCHOOL_SIGNAL_CACHE.get("monotonic") or 0.0)
        if cached is not None and not force and now - ts <= SCHOOL_SIGNAL_CACHE_TTL_SECONDS:
            return cached

    stocks: list[dict[str, Any]] = []
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for tencent_symbol, thscode, name in SCHOOL_SIGNAL_STOCKS:
        try:
            bars = _fetch_school_signal_bars(tencent_symbol)
            if len(bars) < 70:
                raise RuntimeError(f"日K数量不足({len(bars)})")
            stocks.append(school_signal_engine_module.evaluate_stock(thscode, name, bars))
            bars_by_symbol[thscode] = bars
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
        # 与前端高频轮询错开节奏，降低被上游限流的概率
        time.sleep(0.35)

    new_events: list[dict[str, Any]] = []
    for stock in stocks:
        new_events.extend(school_signal_engine_module.extract_events(stock))
    appended = _append_school_signal_events(new_events)
    history = _load_school_signal_events()
    scored = school_signal_engine_module.score_events(history, bars_by_symbol)

    payload = {
        "ok": bool(stocks),
        "schema_version": "school-signals.api.v1",
        "engine_version": school_signal_engine_module.ENGINE_VERSION,
        "generated_at": _iso_now(),
        "stocks": stocks,
        "schools_meta": list(school_signal_engine_module.SCHOOLS),
        "events_logged": len(history),
        "events_appended": appended,
        "recent_events": list(reversed(scored["events"][-30:])),
        "accuracy": scored["stats"],
        "errors": errors,
    }
    with SCHOOL_SIGNAL_LOCK:
        SCHOOL_SIGNAL_CACHE["payload"] = payload
        SCHOOL_SIGNAL_CACHE["monotonic"] = time.monotonic()
    return payload


def _fetch_breadth_one(symbol: str) -> dict[str, Any]:
    payload = _fetch_tencent_kline(
        "breadth",
        TENCENT_KLINE_HOSTS,
        {"param": f"{symbol},day,,,5"},
        referer="https://gu.qq.com/",
        timeout=(2.5, 3.5),
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
        "amount_yi": float(quote[37]) / 10000.0,
        "quote_time_raw": str(quote[30]),
        "quote_time": _format_quote_time(str(quote[30])),
    }


def _fetch_breadth() -> dict[str, Any]:
    shanghai = _fetch_breadth_one("sh000001")
    shenzhen = _fetch_breadth_one("sz399001")
    return {
        "up": shanghai["up"] + shenzhen["up"],
        "flat": shanghai["flat"] + shenzhen["flat"],
        "down": shanghai["down"] + shenzhen["down"],
        "total": shanghai["total"] + shenzhen["total"],
        "amount_yi": round(shanghai["amount_yi"] + shenzhen["amount_yi"], 4),
        "quote_time": max(shanghai["quote_time"], shenzhen["quote_time"]),
        "exchanges": {"上海": shanghai, "深圳": shenzhen},
        "source": "腾讯财经",
    }


def _fetch_sina_globals() -> list[dict[str, Any]]:
    response = LIVE_SESSION.get(
        SINA_HQ + ",".join(SINA_GLOBALS),
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=(2.5, 3.5),
    )
    response.raise_for_status()
    text = response.content.decode("gbk", "replace")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r'var hq_str_([^=]+)="(.*)";', line.strip())
        if not match or not match.group(2):
            continue
        symbol, raw = match.groups()
        if symbol not in SINA_GLOBALS:
            continue
        configured_name, kind, unit = SINA_GLOBALS[symbol]
        values = raw.split(",")
        price = previous = change = change_pct = high = low = None
        quote_time = ""
        if kind == "future" and len(values) >= 14:
            price, previous = _float(values[0]), _float(values[7])
            high, low = _float(values[4]), _float(values[5])
            quote_time = f"{values[12]} {values[6]}"
        elif kind == "world_index" and len(values) >= 12:
            price, change, change_pct = _float(values[1]), _float(values[2]), _float(values[3])
            previous, high, low = _float(values[9]), _float(values[10]), _float(values[11])
            quote_time = f"{values[6]} {values[7]}"
        elif kind == "fx" and len(values) >= 10:
            price = _float(values[8]) if _float(values[8]) is not None else _float(values[1])
            previous, high, low = _float(values[3]), _float(values[6]), _float(values[7])
            quote_time = f"{values[-1]} {values[0]}"
        if change is None and price is not None and previous not in (None, 0):
            change = price - previous
        if change_pct is None and change is not None and previous not in (None, 0):
            change_pct = change / previous * 100
        rows.append(
            {
                "symbol": symbol,
                "name": configured_name,
                "price": _safe_round(price, 4),
                "change": _safe_round(change, 4),
                "change_pct": _safe_round(change_pct, 3),
                "high": high,
                "low": low,
                "quote_time": quote_time,
                "unit": unit,
                "source": "新浪财经",
            }
        )
    return rows


# ---------- 先行指标：富时A50期货 + 期权 PCR/IV 恐慌指数 ----------

A50_SINA_SYMBOL = "hf_CHA50CFD"  # 新浪全球期货：富时中国A50期货（SGX 连续）
_LEADING_A50_CACHE: dict[str, Any] = {"at": 0.0, "row": None}
_LEADING_A50_TTL_SEC = 20.0


def _fetch_a50_quote() -> dict[str, Any]:
    """富时A50期货最新行情（新浪 hf_CHA50CFD），20 秒内存缓存。

    字段布局同 _fetch_sina_globals 的 future 分支：
    0=最新价 4=最高 5=最低 6=时间 7=昨结 12=日期 13=名称。
    """
    now = time.monotonic()
    if _LEADING_A50_CACHE["row"] is not None and now - _LEADING_A50_CACHE["at"] < _LEADING_A50_TTL_SEC:
        return _LEADING_A50_CACHE["row"]
    response = LIVE_SESSION.get(
        SINA_HQ + A50_SINA_SYMBOL,
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=(2.5, 3.5),
    )
    response.raise_for_status()
    text = response.content.decode("gbk", "replace")
    match = re.search(r'var hq_str_[^=]+="([^"]*)"', text)
    if not match or not match.group(1):
        raise RuntimeError("A50 期货行情为空")
    values = match.group(1).split(",")
    if len(values) < 14:
        raise RuntimeError("A50 期货行情字段不完整")
    price = _float(values[0])
    previous = _float(values[7])
    if price is None or previous in (None, 0):
        raise RuntimeError("A50 期货价格无效")
    change = price - previous
    row = {
        "symbol": A50_SINA_SYMBOL,
        "name": "富时A50期货",
        "available": True,
        "price": _safe_round(price, 2),
        "change": _safe_round(change, 2),
        "change_pct": _safe_round(change / previous * 100, 3),
        "high": _float(values[4]),
        "low": _float(values[5]),
        "quote_time": f"{values[12]} {values[6]}",
        "unit": "点",
        "source": "新浪财经",
    }
    _LEADING_A50_CACHE["at"] = now
    _LEADING_A50_CACHE["row"] = row
    return row


# CFFEX 股指期货当月连续合约与对应现货指数。
#
# 东财的连续合约代码是 6 位数字 + 3 位类型的合成码，不是 "IF00Y" 这种字母码：
#   040120=IF当月连续  070120=IH  060120=IC  150120=IM
# 这些代码取自 push2 clist 的 m:8（中金所）板块实测返回值：板块内
# "IF当月连续" 的 f12=040120，"IM当月连续" 的 f12=150120，以此类推。
#
# 一次取全：期货走**一次** clist 请求（m:8 板块内按 f12 过滤），现货走**一次**
# 腾讯请求（sh000300 等）。逐个 secid 单查需要 8 次请求，撞上东财按 IP 限流
# 后会退化成 8×N 次重试（实测 79 秒），而前端每 8 秒轮询一次，必然超时。
# 每项含：东财合约代码、腾讯现货指数代码、显示名。
# 统一用 _basis_spec() 拆包，避免调用点直接按位置解包时错位。
_INDEX_FUTURES_BASIS_SPECS = [
    ("040120", "sh000300", "IF 沪深300"),
    ("070120", "sh000016", "IH 上证50"),
    ("060120", "sh000905", "IC 中证500"),
    ("150120", "sh000852", "IM 中证1000"),
]


def _basis_spec(
    spec: tuple[str, str, str],
) -> tuple[str, str, str]:
    """Normalize a spec row to (code, tencent_symbol, label)."""

    code, tencent_symbol, label = spec
    return code, tencent_symbol, label


# 股指期货相对现货的合理偏离上限（百分比）。基差是资金数字，若两个来源
# 相差过大说明某一路取错了标的或字段，宁可丢弃也不能把错误数字挂到看板上。
_BASIS_SANITY_MAX_PCT = 8.0

# 东财 clist 的中金所板块（fs=m:8）与通用 ut 参数：作为 ulist 之外的第二条
# 期货取数路径。
EM_CFFEX_BOARD = "m:8"
EM_CFFEX_UT = "bd1d9ddb04089700cf9c27f6f7426281"


def _tencent_index_prices(symbols: Sequence[str]) -> dict[str, float]:
    """Latest index levels for several Tencent symbols in one request."""

    response = LIVE_SESSION.get(
        TENCENT_QUOTE + ",".join(symbols),
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=(2.5, 4.0),
    )
    response.raise_for_status()
    raw = response.content.decode("gbk", "replace")
    out: dict[str, float] = {}
    for line in raw.split(";"):
        if '="' not in line:
            continue
        key = line.split("=", 1)[0].rsplit("_", 1)[-1]
        values = line.split('"', 2)[1].split("~")
        if len(values) < 4:
            continue
        price = _float(values[3])
        if price is not None:
            out[key] = price
    return out


def _cffex_futures_prices(codes: Sequence[str]) -> dict[str, float]:
    """CFFEX 当月连续合约价格，一次请求取回全部需要的合约。

    走东财 push2 的 ``ulist.np`` 接口（可一次带多个 secid）：期货合约的
    secid 是 ``8.<合约代码>``，返回的 f14 是合约名称（"IF当月连续"）、f2 是
    最新价。名称与代码同时返回，便于确认取到的确实是目标合约。

    逐个 secid 单查需要 8 次请求，撞上东财按 IP 限流后会退化成 N×M 次重试
    （实测 79 秒），而前端每 8 秒轮询一次，必然超时。
    """

    wanted = set(codes)
    last_error: Exception | None = None
    # 两条互为备用的东财路径：ulist（按 secid 直查）与 clist（中金所 m:8 板块）。
    # 东财的 WAF 按端点分别封禁，实测常出现"ulist 通而 clist 断"或反之，
    # 因此两条都试，任一成功即可。
    secids = ",".join(f"8.{code}" for code in codes)
    attempts = (
        "https://{host}/api/qt/ulist.np/get"
        f"?secids={secids}&fltt=2&invt=2&fields=f2,f3,f12,f13,f14,f15,f16,f17,f18",
        "https://{host}/api/qt/clist/get"
        f"?pn=1&pz=200&po=1&np=1&ut={EM_CFFEX_UT}&fltt=2&invt=2&fid=f3"
        f"&fs={EM_CFFEX_BOARD}&fields=f2,f3,f12,f13,f14",
    )
    for template in attempts:
        try:
            payload = _curl_get_json_rotating(
                template, referer="https://quote.eastmoney.com/"
            )
        except Exception as exc:
            last_error = exc
            continue
        rows = ((payload.get("data") or {}).get("diff")) or []
        out: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "")
            if code not in wanted:
                continue
            price = _float(row.get("f2"))
            if price is not None:
                out[code] = price
        if out:
            return out
    if last_error is not None:
        raise RuntimeError(
            f"Eastmoney CFFEX quotes unavailable: {type(last_error).__name__}: {last_error}"
        ) from last_error
    return {}


def fetch_index_futures_basis() -> dict[str, Any]:
    """CFFEX 股指期货连续合约基差：期货价 - 现货指数价。

    基差为正=升水（期货溢价，看多情绪），为负=贴水（看空情绪）。每条独立
    try，失败只标该条不可用，不影响其他。

    数据源（期货腿）：东财 push2 的两条路径——``ulist.np``（一次请求取全部 4
    个合约）与 ``clist`` m:8 板块。东财按端点分别封禁，实测常一个通一个不通，
    因此两条都试。两条都不通时，基差整体降级为"暂不可用"（或展示上一次成功
    值并标记 stale），**不切换到其他口径的来源**——新浪的"连续"与东财的
    "当月连续"经实测其开盘/最高/最低/昨收并不一致（固定日内事实对不上），
    属不同合约序列，混用会让基差凭空偏移约 1%。

    现货腿：腾讯行情（一次请求取全部 4 个指数），与期货腿分属不同域名。
    """

    specs = [_basis_spec(row) for row in _INDEX_FUTURES_BASIS_SPECS]
    futures: dict[str, float] = {}
    futures_error = ""
    try:
        futures = _cffex_futures_prices([code for code, _tencent, _label in specs])
    except Exception as exc:
        futures_error = f"{type(exc).__name__}: {exc}"
    spot: dict[str, float] = {}
    spot_error = ""
    try:
        spot = _tencent_index_prices(
            [tencent for _code, tencent, _label in specs]
        )
    except Exception as exc:
        spot_error = f"{type(exc).__name__}: {exc}"

    rows: list[dict[str, Any]] = []
    for code, tencent_symbol, label in specs:
        fut_price = futures.get(code)
        spot_price = spot.get(tencent_symbol)
        if fut_price is None and futures_error:
            rows.append({
                "name": label, "available": False, "error": futures_error,
            })
            continue
        if spot_price is None and spot_error:
            rows.append({
                "name": label, "available": False, "error": spot_error,
            })
            continue
        if fut_price is None or spot_price is None or spot_price == 0:
            rows.append({
                "name": label, "available": False,
                "error": f"futures or spot unavailable for {label}",
            })
            continue
        basis = round(fut_price - spot_price, 2)
        basis_pct = round(basis / spot_price * 100, 3)
        if abs(basis_pct) > _BASIS_SANITY_MAX_PCT:
            rows.append({
                "name": label, "available": False,
                "error": f"{label} 基差 {basis_pct}% 超出合理区间，疑似取错标的",
            })
            continue
        rows.append({
            "name": label,
            "futures_price": fut_price,
            "spot_price": spot_price,
            "basis": basis,
            "basis_pct": basis_pct,
            "contango": basis > 0,
        })
    if not any(r.get("basis") is not None for r in rows):
        return {"available": False, "rows": rows, "error": "all basis unavailable"}
    return {"available": True, "rows": rows, "source": "东财 push2 CFFEX 连续"}


def _tencent_quote_fields(symbol: str) -> list[str]:
    """Return the ``~``-separated Tencent quote fields for one symbol.

    The response body is GBK-encoded and shaped like
    ``v_sh510300="1~名称~代码~现价~..."``; the order-book columns live at
    fixed indices (9/10 = 买一价/量 … 17/18 = 买五, 19/20 = 卖一价/量 …
    27/28 = 卖五).  Reusing the project's existing Tencent transport keeps
    the request path identical to the live-quote fallback.
    """

    response = LIVE_SESSION.get(
        TENCENT_QUOTE + symbol,
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=(2.5, 4.0),
    )
    response.raise_for_status()
    raw = response.content.decode("gbk", "replace")
    for line in raw.split(";"):
        if '="' not in line:
            continue
        values = line.split('"', 2)[1].split("~")
        if len(values) < 29:
            continue
        return values
    raise RuntimeError(f"Tencent quote returned no fields for {symbol}")


def fetch_order_imbalance() -> dict[str, Any]:
    """5 档盘口近似多空挂单不平衡：取 300ETF(sh510300) 的 5 档买卖盘口。

    走腾讯行情接口 ``qt.gtimg.cn/q=``：返回体以 ``~`` 分隔，字段 9/10 是
    买一价/买一量，11/12 是买二，直到 17/18 是买五；19/20 起是卖一价/卖一量
    直到 27/28 是卖五。东财 push2 的 stock/get **不返回** 5 档挂单（f19/f20
    等在该接口是市值/量比），只有 ulist/clist 才带盘口且板块 fs 极易被 WAF
    拦下；腾讯这条链路稳定且字段含义明确。

    IAI 不平衡度 = (买盘总量-卖盘总量)/(买盘总量+卖盘总量)，>0 偏多。
    标注"单标的近似"——全市场多空挂单需要付费 L2 数据，此处用 300ETF 作代理。
    """
    payload = _tencent_quote_fields("sh510300")
    name = payload[1]
    price = _float(payload[3])
    quote_time = ""
    if len(payload) > 30 and len(payload[30]) >= 14:
        raw_ts = payload[30]
        try:
            quote_time = datetime.strptime(raw_ts, "%Y%m%d%H%M%S").replace(
                tzinfo=CN_TZ
            ).isoformat(timespec="seconds")
        except ValueError:
            quote_time = ""
    buy_vols = [_float(payload[i]) for i in (10, 12, 14, 16, 18)]
    sell_vols = [_float(payload[i]) for i in (20, 22, 24, 26, 28)]
    buy_total = sum(v for v in buy_vols if v is not None)
    sell_total = sum(v for v in sell_vols if v is not None)
    denom = buy_total + sell_total
    if denom == 0:
        raise RuntimeError("300ETF 5 档盘口为空")
    imbalance = round((buy_total - sell_total) / denom, 4)
    return {
        "available": True,
        "symbol": "sh510300",
        "name": name or "300ETF",
        "price": price,
        "buy_total_wan": round(buy_total, 1),
        "sell_total_wan": round(sell_total, 1),
        "imbalance": imbalance,
        "quote_time": quote_time,
        "source": "腾讯行情 5档盘口（单标的近似）",
    }


_LEADING_PAYLOAD_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_LEADING_PAYLOAD_TTL_SEC = 30.0
_LEADING_PAYLOAD_LOCK = threading.Lock()


def _leading_component(
    key: str,
    fetch: Callable[[], dict[str, Any]],
    *,
    unavailable: dict[str, Any],
) -> dict[str, Any]:
    """Run one indicator source, degrading to a cached value on failure."""

    try:
        value = fetch()
    except Exception as exc:
        cached = _LEADING_PAYLOAD_CACHE.get("payload") or {}
        previous = cached.get(key)
        if isinstance(previous, dict) and previous:
            return dict(previous, stale=True, error=f"{type(exc).__name__}: {exc}")
        return dict(unavailable, error=f"{type(exc).__name__}: {exc}")
    if isinstance(value, dict):
        return value
    return unavailable


def get_leading_indicators_payload() -> dict[str, Any]:
    """先行指标聚合：A50 期货 + 期权 PCR/IV + 两融 + 资金流 + 涨跌停 + 股指基差 + 货币市场 + 盘口不平衡。

    每个数据源各自独立容错：任何一个失败只标记该块不可用，不影响其他块。

    前端每 8 秒轮询本接口，而东财 push2 的最小请求间隔是 1.15 秒/次——若
    每次都重新采集（8 个来源、十余次请求），一次刷新就会跑满节流窗口并
    很快触发东财 WAF 的 IP 封禁（表现为大面积 "Empty reply from server"）。
    因此这里按 30 秒 TTL 缓存整份载荷：来源各自带内部缓存，TTL 内的请求
    直接复用上次结果；单个来源失败时回退到上一次成功的值并标记 stale。
    """

    now = time.monotonic()
    with _LEADING_PAYLOAD_LOCK:
        cached_payload = _LEADING_PAYLOAD_CACHE.get("payload")
        if (
            cached_payload is not None
            and now - _LEADING_PAYLOAD_CACHE["at"] < _LEADING_PAYLOAD_TTL_SEC
        ):
            return cached_payload

    def _a50() -> dict[str, Any]:
        return _fetch_a50_quote()

    def _options() -> dict[str, Any]:
        return options_sentiment_module.get_options_sentiment()

    def _margin() -> dict[str, Any]:
        value = market_intel_module.fetch_margin_summary()
        value["available"] = True
        return value

    def _northbound() -> dict[str, Any]:
        value = market_intel_module.fetch_northbound_flow()
        value["available"] = True
        return value

    def _limit_pool() -> dict[str, Any]:
        value = market_intel_module.fetch_limit_pool_counts()
        value["available"] = True
        return value

    def _basis() -> dict[str, Any]:
        return fetch_index_futures_basis()

    def _money_rates() -> dict[str, Any]:
        value = market_intel_module.fetch_money_market_rates()
        value["available"] = True
        return value

    def _imbalance() -> dict[str, Any]:
        return fetch_order_imbalance()

    a50 = _leading_component(
        "a50",
        _a50,
        unavailable={"symbol": A50_SINA_SYMBOL, "name": "富时A50期货", "available": False},
    )
    options = _leading_component(
        "options", _options, unavailable={"available": False, "items": []}
    )
    margin = _leading_component(
        "margin", _margin, unavailable={"available": False}
    )
    northbound = _leading_component(
        "northbound", _northbound, unavailable={"available": False}
    )
    limit_pool = _leading_component(
        "limit_pool", _limit_pool, unavailable={"available": False}
    )
    basis = _leading_component(
        "basis", _basis, unavailable={"available": False, "rows": []}
    )
    money_rates = _leading_component(
        "money_rates", _money_rates, unavailable={"available": False, "rates": {}}
    )
    imbalance = _leading_component(
        "imbalance", _imbalance, unavailable={"available": False}
    )
    payload = {
        "ok": True,
        "generated_at": _iso_now(),
        "a50": a50,
        "options": options,
        "margin": margin,
        "northbound": northbound,
        "limit_pool": limit_pool,
        "basis": basis,
        "money_rates": money_rates,
        "imbalance": imbalance,
    }
    with _LEADING_PAYLOAD_LOCK:
        _LEADING_PAYLOAD_CACHE["at"] = time.monotonic()
        _LEADING_PAYLOAD_CACHE["payload"] = payload
    return payload


def _fetch_em_bond_yields() -> list[dict[str, Any]]:
    """东财全球债券收益率（如 171.US10Y 美债10年），push2 行情接口。

    新浪 globalbd 接口无数据，故走东财；_curl_get_json 自带东财节流与
    主机轮换，行情接口 push2 与 K线 push2his 分属不同 WAF 域。
    """

    rows: list[dict[str, Any]] = []
    for code, (name, unit) in EM_BOND_YIELDS.items():
        payload = _curl_get_json(
            f"{EM_BOND_QUOTE_URL}?secid=171.{code}&fltt=2"
            "&fields=f43,f57,f58,f60,f86,f169,f170",
            referer="https://quote.eastmoney.com/",
        )
        data = payload.get("data") or {}
        price = _float(data.get("f43"))
        previous = _float(data.get("f60"))
        change = _float(data.get("f169"))
        change_pct = _float(data.get("f170"))
        if price is None or price <= 0:
            raise RuntimeError(f"EM bond yield unavailable: {code}")
        if change is None and previous not in (None, 0):
            change = price - previous
        if change_pct is None and change is not None and previous not in (None, 0):
            change_pct = change / previous * 100
        quote_ts = _float(data.get("f86"))
        quote_time = (
            datetime.fromtimestamp(quote_ts, CN_TZ).isoformat(timespec="seconds")
            if quote_ts and quote_ts > 0
            else ""
        )
        rows.append(
            {
                "symbol": code,
                "name": name,
                "price": _safe_round(price, 4),
                "change": _safe_round(change, 4),
                "change_pct": _safe_round(change_pct, 3),
                "high": None,
                "low": None,
                "quote_time": quote_time,
                "unit": unit,
                "source": "东方财富",
            }
        )
    return rows


# 隔夜全球标的行情缓存：数据只做 A 股盘面参考，缓存 5 分钟（约等于美股盘中
# 一次价格变动周期），不跟随 3 秒行情节奏打东财；SK海力士随批次一起刷新。
EM_OVERNIGHT_TTL_SECONDS = 300.0
_EM_OVERNIGHT_CACHE: dict[str, Any] = {"monotonic": 0.0, "rows": []}


def _fetch_em_overnight_quotes() -> list[dict[str, Any]]:
    """隔夜全球标的行情：东财 ulist 批量取美股/欧股/白银，SK海力士走 Naver。

    ulist 单次请求覆盖全部 secid（共享东财节流），f12=代码按 em_secid 尾段
    映射回配置项；停牌/无效报价（f2 非正数）跳过。SK海力士行情由 Naver 日K
    最近两根 bar 推导（当日盘中 bar 为实时快照），失败不阻断其余标的。
    """

    cached_rows = _EM_OVERNIGHT_CACHE["rows"]
    if cached_rows and time.monotonic() - _EM_OVERNIGHT_CACHE["monotonic"] < EM_OVERNIGHT_TTL_SECONDS:
        return [dict(row) for row in cached_rows]

    batched = [item for item in EM_OVERNIGHT_INSTRUMENTS if item.get("em_secid")]
    payload = _curl_get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get?secids="
        + ",".join(str(item["em_secid"]) for item in batched)
        + "&fields=f2,f3,f4,f12,f14,f15,f16,f18,f124&fltt=2",
        referer="https://quote.eastmoney.com/",
    )
    data = payload.get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    by_code = {str(row.get("f12") or ""): row for row in diff if isinstance(row, dict)}

    rows: list[dict[str, Any]] = []
    for item in batched:
        row = by_code.get(str(item["em_secid"]).split(".", 1)[1])
        if not row:
            continue
        price = _float(row.get("f2"))
        if price is None or price <= 0:
            continue
        previous = _float(row.get("f18"))
        change = _float(row.get("f4"))
        change_pct = _float(row.get("f3"))
        if change is None and previous not in (None, 0):
            change = price - previous
        if change_pct is None and change is not None and previous not in (None, 0):
            change_pct = change / previous * 100
        quote_ts = _float(row.get("f124"))
        quote_time = (
            datetime.fromtimestamp(quote_ts, CN_TZ).isoformat(timespec="seconds")
            if quote_ts and quote_ts > 0
            else ""
        )
        rows.append(
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "group": item["group"],
                "price": _safe_round(price, 4),
                "change": _safe_round(change, 4),
                "change_pct": _safe_round(change_pct, 3),
                "high": _safe_round(_float(row.get("f15")), 4),
                "low": _safe_round(_float(row.get("f16")), 4),
                "quote_time": quote_time,
                "unit": "点" if item["group"] == "欧股指数" else "美元",
                "source": "东方财富",
            }
        )

    try:
        naver_daily = _fetch_naver_daily_chart("000660", count=2)
        naver_items = naver_daily.get("items") or []
        if len(naver_items) >= 2:
            latest, prior = naver_items[-1], naver_items[-2]
            latest_close = _float(latest.get("close"))
            prior_close = _float(prior.get("close"))
            if latest_close and prior_close:
                rows.append(
                    {
                        "symbol": "b_SKHYNIX",
                        "name": "SK海力士",
                        "group": "存储链",
                        "price": _safe_round(latest_close, 0),
                        "change": _safe_round(latest_close - prior_close, 0),
                        "change_pct": _safe_round((latest_close / prior_close - 1) * 100, 3),
                        "high": _safe_round(_float(latest.get("high")), 0),
                        "low": _safe_round(_float(latest.get("low")), 0),
                        "quote_time": str(latest.get("time") or ""),
                        "unit": "韩元",
                        "source": "Naver财经(日K推导)",
                    }
                )
    except Exception:
        pass

    if not rows:
        raise RuntimeError("eastmoney ulist returned no overnight quotes")
    # SK海力士等非东财来源的行是后追加的，按配置顺序排序保证分组展示连续。
    _overnight_order = {item["symbol"]: idx for idx, item in enumerate(EM_OVERNIGHT_INSTRUMENTS)}
    rows.sort(key=lambda row: _overnight_order.get(row["symbol"], len(_overnight_order)))
    _EM_OVERNIGHT_CACHE["rows"] = rows
    _EM_OVERNIGHT_CACHE["monotonic"] = time.monotonic()
    return [dict(row) for row in rows]


# 美债收益率官方备援：财政部每日收益率曲线 CSV（stock_all_in_1 共享指南 1.3b，
# 当日可取、免 key、全曲线一次返回）。东财 push2 被 WAF 封禁时启用。
TREASURY_YIELD_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)


def _fetch_treasury_us10y_csv() -> list[dict[str, Any]]:
    """美国财政部官方 10 年期收益率（东财 push2 美债行情封禁时的备援）。

    官方 CSV 当日更新、无滞后，一次返回完整曲线；这里只取 10 Yr 与前一
    交易日差值，输出结构与 ``_fetch_em_bond_yields`` 对齐。文件头带 BOM，
    必须用 utf-8-sig 解码。日期格式为 MM/DD/YYYY。
    """

    year = _now().year
    response = LIVE_SESSION.get(
        TREASURY_YIELD_CSV.format(year=year),
        headers={"User-Agent": UA, "Referer": "https://home.treasury.gov/"},
        timeout=(2.5, 10.0),
    )
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig", "replace"))))
    points: list[tuple[str, float]] = []
    for row in rows:
        raw_date = str(row.get("Date") or "").strip()
        raw_yield = str(row.get("10 Yr") or "").strip()
        try:
            session = datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
            value = float(raw_yield)
        except (ValueError, TypeError):
            continue
        points.append((session, value))
    points.sort()
    if len(points) < 2:
        raise RuntimeError("treasury csv has fewer than 2 valid 10Y points")
    (prev_date, prev_value), (last_date, last_value) = points[-2], points[-1]
    change = last_value - prev_value
    return [
        {
            "symbol": "US10Y",
            "name": "美债收益率",
            "price": _safe_round(last_value, 4),
            "change": _safe_round(change, 4),
            "change_pct": _safe_round(
                change / prev_value * 100 if prev_value else None, 3
            ),
            "high": None,
            "low": None,
            "quote_time": f"{last_date}T16:00:00+08:00",  # 官方只给日期，时间为占位口径
            "unit": "%",
            "source": "美国财政部(备援)",
        }
    ]


def _market_status(freshest: datetime | None) -> dict[str, Any]:
    now = _now()
    current = now.time().replace(tzinfo=None)
    quote_date = freshest.date() if freshest else None
    same_day = quote_date == now.date()
    is_session_date, calendar_status = _a_share_session_date(now.date())
    if not is_session_date:
        label = "周末休市 / 最近交易日" if calendar_status == "weekend" else "法定休市 / 最近交易日"
        state = "closed"
    elif current < daytime(9, 15):
        state, label = "pre_open", "盘前"
    elif daytime(9, 15) <= current < daytime(9, 25):
        state, label = "auction", "集合竞价"
    elif daytime(9, 25) <= current < daytime(9, 30):
        state, label = "pre_open", "开盘前"
    elif daytime(9, 30) <= current <= daytime(11, 30):
        state, label = "open", "连续竞价"
    elif daytime(11, 30) < current < daytime(13, 0):
        state, label = "lunch", "午间休市"
    elif daytime(13, 0) <= current <= daytime(15, 0):
        state, label = "open", "连续竞价"
    else:
        state, label = "closed", "已收盘"
    age = (now - freshest).total_seconds() if freshest else None
    feed_stale = bool(
        state == "open"
        and (not same_day or age is None or age > 90)
    )
    if feed_stale:
        label += " · 行情延迟"
    return {
        "state": state,
        "label": label,
        "cn_time": now.isoformat(timespec="seconds"),
        "quote_time": freshest.isoformat(timespec="seconds") if freshest else "",
        "quote_age_seconds": None if age is None else max(0, round(age, 1)),
        "feed_stale": feed_stale,
        "public_feed": True,
        "trading_day": is_session_date,
        "calendar_status": calendar_status,
    }


def _parse_quote_datetime(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
    except ValueError:
        return _parse_tencent_time(value)


def _is_a_share_stock(item: dict[str, Any]) -> bool:
    """Return whether an instrument belongs in the stock-only HiThink endpoint."""

    instrument_type = str(item.get("instrument_type") or "").strip().lower()
    if instrument_type:
        return instrument_type == "stock"
    thscode = str(item.get("thscode") or "").strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", thscode)
    if not match:
        return False
    code, exchange = match.groups()
    if exchange == "SH":
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if exchange == "SZ":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return code.startswith(("4", "8", "9"))


def _hithink_internal_quote(
    quote: dict[str, Any], provider: dict[str, Any]
) -> dict[str, Any]:
    price = _float(quote.get("price"))
    previous = _float(quote.get("prev_close"))
    change = _float(quote.get("change"))
    if change is None and price is not None and previous is not None:
        change = price - previous
    change_pct = _float(quote.get("change_pct"))
    if change_pct is None and change is not None and previous not in (None, 0):
        change_pct = change / previous * 100
    timestamp = str(provider.get("source_timestamp") or provider.get("last_success_at") or "")
    turnover = _float(quote.get("turnover"))
    return {
        "price": price,
        "prev_close": previous,
        "open": _float(quote.get("open")),
        "change": change,
        "change_pct": change_pct,
        "high": _float(quote.get("high")),
        "low": _float(quote.get("low")),
        "amount_yi": None if turnover is None else turnover / 100_000_000.0,
        "turnover_pct": None,
        "pe_ttm": None,
        "pb": None,
        "mcap_yi": None,
        "quote_time_raw": timestamp,
        "quote_time": timestamp,
    }


def _quote_validation(
    *,
    thscode: str,
    is_stock: bool,
    public_quote: dict[str, Any] | None,
    public_stale: bool,
    official_quote: dict[str, Any] | None,
    provider: dict[str, Any],
) -> dict[str, Any]:
    public_price = _float((public_quote or {}).get("price"))
    official_price = _float((official_quote or {}).get("price"))
    common = {
        "required": is_stock,
        "thscode": thscode,
        "public_source": "腾讯财经",
        "public_price": public_price,
        "public_stale": bool(public_stale),
        "official_source": "同花顺金融数据服务",
        "official_price": official_price,
        "official_source_timestamp": provider.get("source_timestamp") or "",
        "official_age_seconds": provider.get("age_seconds"),
        "official_source_age_seconds": provider.get("source_age_seconds"),
    }
    if not is_stock:
        return {
            **common,
            "state": "excluded_non_stock",
            "passed": True,
            "reason": "该标的不是股票，不送入同花顺 A 股股票快照接口",
        }
    official_ready = bool(
        official_price is not None
        and official_price > 0
        and not provider.get("stale")
        and not provider.get("source_stale")
    )
    if not official_ready:
        return {
            **common,
            "state": "official_unavailable",
            "passed": False,
            "reason": str(provider.get("last_error") or "同花顺授权报价缺失或已过期"),
        }
    if public_price is None or public_price <= 0 or public_stale:
        return {
            **common,
            "state": "official_fallback",
            "passed": True,
            "reason": "腾讯报价缺失或盘中超时，采用明确标注的同花顺备援价",
        }
    tolerance_pct = max(
        QUOTE_VALIDATION_MIN_TOLERANCE_PCT,
        QUOTE_VALIDATION_TICK_ALLOWANCE / official_price * 100.0,
    )
    deviation_pct = abs(public_price / official_price - 1.0) * 100.0
    passed = deviation_pct <= tolerance_pct
    return {
        **common,
        "state": "matched" if passed else "divergent",
        "passed": passed,
        "deviation_pct": round(deviation_pct, 4),
        "tolerance_pct": round(tolerance_pct, 4),
        "reason": (
            "公开报价与授权报价在容差内一致"
            if passed
            else "公开报价与授权报价超出容差，禁止形成可执行决策"
        ),
    }


def _quote_validation_scope(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        validation = row.get("official_validation")
        if not isinstance(validation, dict) or not validation.get("required"):
            continue
        code = str(row.get("thscode") or validation.get("thscode") or "")
        current = by_code.get(code)
        if current is None or (current.get("passed") and not validation.get("passed")):
            by_code[code] = validation
    states: dict[str, int] = {}
    for validation in by_code.values():
        state = str(validation.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1
    failed = sorted(code for code, validation in by_code.items() if not validation.get("passed"))
    return {
        "required_count": len(by_code),
        "passed_count": len(by_code) - len(failed),
        "verified": not failed,
        "failed_codes": failed,
        "states": states,
    }


def _quote_validation_summary(
    positions: list[dict[str, Any]],
    watchlist: list[dict[str, Any]],
    provider: dict[str, Any],
) -> dict[str, Any]:
    position_scope = _quote_validation_scope(positions)
    watchlist_scope = _quote_validation_scope(watchlist)
    provider_meta = {
        key: copy.deepcopy(value)
        for key, value in provider.items()
        if key != "quotes"
    }
    decision_verified = bool(position_scope["verified"] and watchlist_scope["verified"])
    return {
        "status": "healthy" if decision_verified else "critical",
        "required_for_execution": True,
        "decision_verified": decision_verified,
        "positions": position_scope,
        "watchlist": watchlist_scope,
        "provider": provider_meta,
        "note": "授权源用于交叉校验和公开源失效时的显式备援；不同来源口径不静默混合。",
    }


def _build_portfolio_position_block(
    portfolio_config: dict[str, Any],
    configured_positions: list[dict[str, Any]],
    *,
    quotes: dict[str, dict[str, Any]],
    official_quotes: dict[str, Any],
    official_provider: dict[str, Any],
    live_window: bool,
    errors: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build position rows and the portfolio summary for one portfolio.

    Every portfolio shares the same fetched ``quotes``/``official_quotes`` maps,
    so a stock held by several portfolios is fetched and validated exactly once.
    """

    total_cash = sum(float(a.get("cash") or 0) for a in portfolio_config.get("accounts") or [])
    total_debt = sum(float(a.get("debt") or 0) for a in portfolio_config.get("accounts") or [])
    position_rows: list[dict[str, Any]] = []
    quote_coverage = 0
    for configured in configured_positions:
        public_q = quotes.get(configured["symbol"])
        public_dt = _parse_quote_datetime((public_q or {}).get("quote_time_raw"))
        public_stale = bool(
            public_q is None
            or public_q.get("price") is None
            or (
                live_window
                and (public_dt is None or (_now() - public_dt).total_seconds() > 90)
            )
        )
        is_stock = _is_a_share_stock(configured)
        official_q = official_quotes.get(str(configured.get("thscode") or "").upper())
        validation = _quote_validation(
            thscode=str(configured.get("thscode") or ""),
            is_stock=is_stock,
            public_quote=public_q,
            public_stale=public_stale,
            official_quote=official_q if isinstance(official_q, dict) else None,
            provider=official_provider,
        )
        using_official = validation.get("state") == "official_fallback"
        q = (
            _hithink_internal_quote(official_q, official_provider)
            if using_official and isinstance(official_q, dict)
            else public_q
        )
        snapshot_fallback = q is None or q.get("price") is None
        if snapshot_fallback:
            price = float(configured.get("snapshot_price") or 0)
            q = {
                "price": price,
                "prev_close": None,
                "change": None,
                "change_pct": None,
                "open": None,
                "high": None,
                "low": None,
                "amount_yi": None,
                "turnover_pct": None,
                "pe_ttm": None,
                "pb": None,
                "mcap_yi": None,
                "quote_time": portfolio_config.get("as_of", ""),
                "quote_time_raw": "",
            }
            errors.append(f"{configured['name']} 使用持仓快照价")
        else:
            quote_coverage += 1
            price = float(q["price"])
        quantity = int(configured.get("quantity") or 0)
        market_value = quantity * price
        previous = _float(q.get("prev_close"))
        daily_pnl = None if previous is None else quantity * (price - previous)
        quote_dt = _parse_quote_datetime(q.get("quote_time_raw") or q.get("quote_time"))
        stale = bool(
            snapshot_fallback
            or (
                live_window
                and (quote_dt is None or (_now() - quote_dt).total_seconds() > 90)
            )
        )
        position_rows.append(
            {
                "account_id": configured["account_id"],
                "account_type": configured["account_type"],
                "thscode": configured["thscode"],
                "symbol": configured["symbol"],
                "code": configured["code"],
                "name": configured["name"],
                "instrument_type": configured.get("instrument_type", "stock"),
                "industry": configured.get("industry", ""),
                "theme": configured.get("theme", ""),
                "quantity": quantity,
                "available": int(configured.get("available") or 0),
                "holding_as_of": portfolio_config.get("as_of"),
                "quantity_source": "账户快照",
                "price": price,
                "prev_close": previous,
                "change": _float(q.get("change")),
                "change_pct": _float(q.get("change_pct")),
                "open": _float(q.get("open")),
                "high": _float(q.get("high")),
                "low": _float(q.get("low")),
                "amount_yi": _float(q.get("amount_yi")),
                "turnover_pct": _float(q.get("turnover_pct")),
                "pe_ttm": _float(q.get("pe_ttm")),
                "pb": _float(q.get("pb")),
                "mcap_yi": _float(q.get("mcap_yi")),
                "market_value": round(market_value, 2),
                "daily_pnl": _safe_round(daily_pnl, 2),
                "quote_time": q.get("quote_time", ""),
                "is_stale": stale,
                "data_source": (
                    "持仓快照"
                    if snapshot_fallback
                    else "同花顺金融数据服务（备援）"
                    if using_official
                    else "腾讯财经"
                ),
                "official_validation": validation,
            }
        )

    market_value = sum(float(p["market_value"]) for p in position_rows)
    net_equity = market_value + total_cash - total_debt
    for item in position_rows:
        item["weight_pct"] = round(item["market_value"] / net_equity * 100, 3) if net_equity else None
        item["weight_pct_nav"] = item["weight_pct"]
        item["weight_pct_securities"] = (
            round(item["market_value"] / market_value * 100, 3) if market_value else None
        )
        item["quote_status"] = (
            "snapshot"
            if item.get("data_source") == "持仓快照"
            else "official_fallback"
            if item.get("data_source") == "同花顺金融数据服务（备援）"
            else "stale"
            if item.get("is_stale")
            else "live"
        )
    position_rows.sort(key=lambda item: item["market_value"], reverse=True)

    account_rows = []
    for account in portfolio_config.get("accounts") or []:
        account_positions = [p for p in position_rows if p["account_id"] == account.get("account_id")]
        account_market_value = sum(float(p["market_value"]) for p in account_positions)
        cash = float(account.get("cash") or 0)
        debt = float(account.get("debt") or 0)
        account_rows.append(
            {
                "account_id": account.get("account_id"),
                "account_type": account.get("account_type"),
                "market_value": round(account_market_value, 2),
                "cash": cash,
                "debt": debt,
                "net_equity": round(account_market_value + cash - debt, 2),
                **_daily_pnl_summary_fields(account_positions),
                "holding_as_of": portfolio_config.get("as_of"),
                "values_are_estimates": True,
            }
        )

    summary = {
        "as_of": portfolio_config.get("as_of"),
        "position_count": len(position_rows),
        "quote_coverage_count": quote_coverage,
        "market_value": round(market_value, 2),
        **_daily_pnl_summary_fields(position_rows),
        "cash": round(total_cash, 2),
        "debt": round(total_debt, 2),
        "net_equity": round(net_equity, 2),
        "gross_exposure_pct": round(market_value / net_equity * 100, 2) if net_equity else None,
        "accounts": account_rows,
    }
    return position_rows, summary


def _daily_pnl_summary_fields(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Complete-total vs covered-amount semantics for one set of position rows.

    ``daily_pnl`` is the full total and is only non-null when every expected
    row carries a valid finite day P&L; a partial sum must never impersonate
    the total.  ``covered_daily_pnl_cny`` keeps the observed partial amount
    and ``daily_pnl_coverage_count`` the number of contributing rows.
    """

    expected = len(rows)
    valid = [
        float(row["daily_pnl"])
        for row in rows
        if isinstance(row.get("daily_pnl"), (int, float))
        and math.isfinite(float(row["daily_pnl"]))
    ]
    covered = round(sum(valid), 2) if valid else None
    complete = bool(valid) and len(valid) == expected
    return {
        "daily_pnl": covered if complete else None,
        "daily_pnl_coverage_count": len(valid),
        "covered_daily_pnl_cny": covered,
    }


def _build_quotes_payload() -> dict[str, Any]:
    portfolio_config, configured_positions, config_mtime_ns, config_error = _portfolio_snapshot()
    entries = _all_portfolio_entries()
    extra_entries = entries[1:]
    union_positions = [
        position for entry in entries for position in entry["positions"]
    ]
    errors: list[str] = []
    if config_error:
        errors.append(f"持仓配置热重载失败，沿用最近有效版本：{config_error}")
    _extras, _extras_mtime, extras_error = _extra_portfolios_snapshot()
    if extras_error:
        errors.append(f"附加组合配置热重载失败，沿用最近有效版本：{extras_error}")
    try:
        watchlist_config, watchlist_items = _load_watchlist_items()
    except Exception as exc:
        watchlist_config, watchlist_items = {}, []
        errors.append(f"观察名单读取失败：{type(exc).__name__}: {exc}")
    position_symbols = list(dict.fromkeys(p["symbol"] for p in union_positions))
    watchlist_symbols = [p["symbol"] for p in watchlist_items]
    stock_codes = sorted(
        {
            str(item.get("thscode") or "").upper()
            for item in [*union_positions, *watchlist_items]
            if _is_a_share_stock(item)
        }
    )
    refresh_phase = str(_background_refresh_profile().get("phase") or "closed")
    HITHINK_QUOTE_PROVIDER.ensure_refresh(stock_codes, refresh_phase)
    requested = list(
        dict.fromkeys(
            [symbol for symbol, _ in INDEXES]
            + position_symbols
            + watchlist_symbols
            + [s for s, _ in TENCENT_GLOBALS]
        )
    )
    tencent_quote_error = ""
    try:
        quotes = _fetch_tencent(requested)
        if not quotes:
            tencent_quote_error = "腾讯公开行情返回空结果"
            errors.append(tencent_quote_error)
    except Exception as exc:
        quotes = {}
        tencent_quote_error = f"腾讯公开行情暂不可用：{type(exc).__name__}"
        errors.append(tencent_quote_error)
    if not quotes:
        # 免费备援：新浪 A股/ETF/指数实时（港美代码无覆盖，仍由腾讯/官方层负责）。
        try:
            quotes = _fetch_sina_cn_quotes(requested)
            if quotes:
                errors.append("腾讯行情不可用，已切换新浪备援（估值类字段缺失）")
                _record_data_source_chain(
                    "realtime_quote", "backup", "新浪财经(备援)", tencent_quote_error
                )
        except Exception as exc:
            errors.append(f"新浪备援行情失败：{type(exc).__name__}")
        if not quotes:
            _record_data_source_chain(
                "realtime_quote", "down", "", tencent_quote_error or "unknown"
            )
    else:
        _record_data_source_chain("realtime_quote", "primary", "腾讯财经")
    # 留一份原始行情（腾讯口径含成交量/时间戳），供"合成当日 bar"复用：
    # 不必为了一根 bar 再打一次腾讯接口。
    with _LIVE_QUOTES_LOCK:
        if quotes:
            _LIVE_QUOTES.update(quotes)
        else:
            _LIVE_QUOTES.clear()
    # Capture after the public request so the independently started official
    # validator can usually complete during the same refresh without blocking it.
    official_provider = HITHINK_QUOTE_PROVIDER.snapshot(stock_codes, refresh_phase)
    official_quotes = (
        official_provider.get("quotes")
        if isinstance(official_provider.get("quotes"), dict)
        else {}
    )

    quote_datetimes = [
        parsed
        for quote in quotes.values()
        for parsed in (_parse_tencent_time(str(quote.get("quote_time_raw") or "")),)
        if parsed is not None and quote.get("symbol") in position_symbols + [x[0] for x in INDEXES]
    ]
    freshest = max(quote_datetimes) if quote_datetimes else None
    status = _market_status(freshest)
    live_window = status["state"] == "open"

    indices: list[dict[str, Any]] = []
    for symbol, name in INDEXES:
        q = quotes.get(symbol)
        if not q:
            errors.append(f"指数缺失：{name}")
            continue
        item = {**q, "name": name}
        item["is_stale"] = bool(
            live_window
            and (
                _parse_tencent_time(str(q.get("quote_time_raw") or "")) is None
                or (_now() - _parse_tencent_time(str(q.get("quote_time_raw") or ""))).total_seconds() > 90
            )
        )
        indices.append(item)

    try:
        breadth = _fetch_breadth()
    except Exception as exc:  # partial degradation should not hide usable prices
        breadth = {"up": None, "flat": None, "down": None, "total": None, "amount_yi": None,
                   "quote_time": "", "source": "腾讯财经"}
        errors.append(f"市场宽度暂不可用：{type(exc).__name__}")

    globals_: list[dict[str, Any]] = []
    for symbol, name in TENCENT_GLOBALS:
        q = quotes.get(symbol)
        if not q:
            continue
        globals_.append(
            {
                "symbol": symbol,
                "name": name,
                "price": q["price"],
                "change": q["change"],
                "change_pct": q["change_pct"],
                "high": q["high"],
                "low": q["low"],
                "quote_time": q["quote_time"],
                "unit": "点",
                "source": "腾讯财经",
            }
        )
    try:
        globals_.extend(_fetch_sina_globals())
    except Exception as exc:
        errors.append(f"海外市场补充暂不可用：{type(exc).__name__}")
    try:
        globals_.extend(_fetch_em_bond_yields())
        _record_data_source_chain("us10y_yield", "primary", "东方财富")
    except Exception as exc:
        # 东财 push2 美债行情被封 → 美国财政部官方收益率曲线 CSV 备援。
        try:
            treasury_rows = _fetch_treasury_us10y_csv()
            globals_.extend(treasury_rows)
            errors.append(f"东财美债行情不可用，已切换财政部官方备援：{type(exc).__name__}")
            _record_data_source_chain(
                "us10y_yield", "backup", "美国财政部(备援)", type(exc).__name__
            )
        except Exception:
            errors.append(f"美债收益率暂不可用：{type(exc).__name__}")
            _record_data_source_chain("us10y_yield", "down", "", type(exc).__name__)
    try:
        overnight = _fetch_em_overnight_quotes()
    except Exception as exc:
        overnight = []
        errors.append(f"隔夜全球标的暂不可用：{type(exc).__name__}")

    position_rows, main_summary = _build_portfolio_position_block(
        portfolio_config,
        configured_positions,
        quotes=quotes,
        official_quotes=official_quotes,
        official_provider=official_provider,
        live_window=live_window,
        errors=errors,
    )
    quote_coverage = main_summary["quote_coverage_count"]

    portfolio_views: dict[str, Any] = {}
    portfolio_summaries: list[dict[str, Any]] = [
        {
            "portfolio_id": MAIN_PORTFOLIO_ID,
            "label": _main_portfolio_label(portfolio_config),
            "editable": False,
            "position_count": main_summary["position_count"],
            "market_value": main_summary["market_value"],
            "daily_pnl": main_summary["daily_pnl"],
            "net_equity": main_summary["net_equity"],
            "as_of": main_summary["as_of"],
        }
    ]
    for entry in extra_entries:
        extra_rows, extra_summary = _build_portfolio_position_block(
            entry["config"],
            entry["positions"],
            quotes=quotes,
            official_quotes=official_quotes,
            official_provider=official_provider,
            live_window=live_window,
            errors=errors,
        )
        portfolio_views[entry["portfolio_id"]] = {
            "portfolio": {
                "portfolio_id": entry["portfolio_id"],
                "label": entry["label"],
                "account_snapshot_as_of": entry["config"].get("as_of"),
                "holdings_state": "USER_DEFINED_SNAPSHOT_NOT_BROKER_LIVE",
                "valuation_basis": "snapshot_quantities_x_latest_available_prices",
                "config_mtime_ns": _extras_mtime,
                **extra_summary,
            },
            "positions": extra_rows,
        }
        portfolio_summaries.append(
            {
                "portfolio_id": entry["portfolio_id"],
                "label": entry["label"],
                "editable": True,
                "position_count": extra_summary["position_count"],
                "market_value": extra_summary["market_value"],
                "daily_pnl": extra_summary["daily_pnl"],
                "net_equity": extra_summary["net_equity"],
                "as_of": extra_summary["as_of"],
            }
        )

    watchlist_rows: list[dict[str, Any]] = []
    for configured in watchlist_items:
        public_q = quotes.get(configured["symbol"])
        public_dt = _parse_quote_datetime((public_q or {}).get("quote_time_raw"))
        public_stale = bool(
            public_q is None
            or public_q.get("price") is None
            or (
                live_window
                and (public_dt is None or (_now() - public_dt).total_seconds() > 90)
            )
        )
        is_stock = _is_a_share_stock(configured)
        official_q = official_quotes.get(str(configured.get("thscode") or "").upper())
        validation = _quote_validation(
            thscode=str(configured.get("thscode") or ""),
            is_stock=is_stock,
            public_quote=public_q,
            public_stale=public_stale,
            official_quote=official_q if isinstance(official_q, dict) else None,
            provider=official_provider,
        )
        using_official = validation.get("state") == "official_fallback"
        q = (
            _hithink_internal_quote(official_q, official_provider)
            if using_official and isinstance(official_q, dict)
            else public_q
        )
        if not q or q.get("price") is None:
            watchlist_rows.append(
                {
                    "thscode": configured.get("thscode"),
                    "symbol": configured.get("symbol"),
                    "code": configured.get("code"),
                    "name": configured.get("name"),
                    "theme": configured.get("theme"),
                    "price": None,
                    "change_pct": None,
                    "quote_time": "",
                    "is_stale": True,
                    "quote_status": "missing",
                    "data_source": "腾讯财经（缺失）",
                    "official_validation": validation,
                }
            )
            continue
        quote_dt = _parse_quote_datetime(q.get("quote_time_raw") or q.get("quote_time"))
        stale = bool(
            live_window
            and (quote_dt is None or (_now() - quote_dt).total_seconds() > 90)
        )
        watchlist_rows.append(
            {
                "thscode": configured.get("thscode"),
                "symbol": configured.get("symbol"),
                "code": configured.get("code"),
                "name": configured.get("name") or q.get("source_name"),
                "theme": configured.get("theme"),
                "price": _float(q.get("price")),
                "prev_close": _float(q.get("prev_close")),
                "change": _float(q.get("change")),
                "change_pct": _float(q.get("change_pct")),
                "open": _float(q.get("open")),
                "high": _float(q.get("high")),
                "low": _float(q.get("low")),
                "amount_yi": _float(q.get("amount_yi")),
                "turnover_pct": _float(q.get("turnover_pct")),
                "pe_ttm": _float(q.get("pe_ttm")),
                "pb": _float(q.get("pb")),
                "mcap_yi": _float(q.get("mcap_yi")),
                "quote_time": q.get("quote_time", ""),
                "is_stale": stale,
                "quote_status": "official_fallback" if using_official else "stale" if stale else "live",
                "data_source": "同花顺金融数据服务（备援）" if using_official else "腾讯财经",
                "official_validation": validation,
            }
        )

    quote_validation = _quote_validation_summary(
        position_rows, watchlist_rows, official_provider
    )

    return {
        "ok": quote_coverage > 0,
        "generated_at": _iso_now(),
        "market_status": status,
        "portfolio_id": MAIN_PORTFOLIO_ID,
        "portfolio_label": _main_portfolio_label(portfolio_config),
        "portfolio": {
            "portfolio_id": MAIN_PORTFOLIO_ID,
            "source_portfolio_id": portfolio_config.get("portfolio_id"),
            "label": _main_portfolio_label(portfolio_config),
            "as_of": portfolio_config.get("as_of"),
            "account_snapshot_as_of": portfolio_config.get("as_of"),
            "holdings_state": "LOCAL_ACCOUNT_SNAPSHOT_NOT_BROKER_LIVE",
            "valuation_basis": "snapshot_quantities_x_latest_available_prices",
            "config_mtime_ns": config_mtime_ns,
            **{key: main_summary[key] for key in (
                "position_count",
                "quote_coverage_count",
                "market_value",
                "daily_pnl",
                "daily_pnl_coverage_count",
                "cash",
                "debt",
                "net_equity",
                "gross_exposure_pct",
                "accounts",
            )},
        },
        "portfolios": portfolio_summaries,
        "portfolio_views": portfolio_views,
        "extras_config_mtime_ns": _extras_mtime,
        "markets": {"indices": indices, "breadth": breadth, "global": globals_, "overnight": overnight},
        "positions": position_rows,
        "quote_validation": quote_validation,
        "account_snapshot": {
            "as_of": portfolio_config.get("as_of"),
            "broker_live": False,
            "note": "数量、可用量、现金和负债来自本地账户快照；实时价格不等于实时持仓。",
        },
        "watchlist": watchlist_rows,
        "watchlist_meta": {
            "as_of": watchlist_config.get("as_of"),
            "review_by": watchlist_config.get("review_by"),
            "quote_coverage_count": sum(1 for item in watchlist_rows if item.get("price") is not None),
            "item_count": len(watchlist_rows),
        },
        "sources": [
            {"name": "腾讯财经", "role": "A股行情、指数、市场宽度", "refresh_seconds": 5},
            {
                "name": "同花顺金融数据服务",
                "role": "股票行情交叉校验及腾讯失效时的显式备援",
                "refresh_seconds": official_provider.get("ttl_seconds"),
                "status": official_provider.get("status"),
            },
            {"name": "新浪财经", "role": "海外期货、外汇和亚太指数", "refresh_seconds": 15},
            {"name": "持仓配置", "role": "数量、账户现金与负债", "as_of": portfolio_config.get("as_of")},
        ],
        "errors": errors,
    }


def _payload_quote_time(payload: dict[str, Any]) -> datetime | None:
    raw = str((payload.get("market_status") or {}).get("quote_time") or "")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            return parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
        except ValueError:
            pass
    return None


def _row_quote_is_stale(row: dict[str, Any], live_window: bool) -> bool:
    if row.get("data_source") == "持仓快照":
        return True
    raw = str(row.get("quote_time") or "")
    parsed: datetime | None = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            parsed = parsed.replace(tzinfo=CN_TZ) if parsed.tzinfo is None else parsed.astimezone(CN_TZ)
        except ValueError:
            parsed = _parse_tencent_time(raw)
    return bool(live_window and (parsed is None or (_now() - parsed).total_seconds() > 90))


def _recompute_position_block_freshness(
    portfolio_meta: dict[str, Any],
    positions: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    live_window: bool,
) -> None:
    """Re-derive coverage and totals from row-level freshness at read time.

    The cached capture keeps its original row data; only the derived view is
    updated so a row that has gone stale after caching can no longer count as
    valid live evidence in the summary, coverage counts or account subtotals.
    """

    def _row_has_valid_daily_pnl(row: dict[str, Any]) -> bool:
        if row.get("data_source") == "持仓快照" or row.get("is_stale"):
            return False
        value = row.get("daily_pnl")
        return isinstance(value, (int, float)) and math.isfinite(float(value))

    def _row_has_valid_quote(row: dict[str, Any]) -> bool:
        if row.get("data_source") == "持仓快照" or row.get("is_stale"):
            return False
        price = row.get("price")
        return isinstance(price, (int, float)) and math.isfinite(float(price))

    expected = len(positions)
    valid_daily = [float(row["daily_pnl"]) for row in positions if _row_has_valid_daily_pnl(row)]
    covered = round(sum(valid_daily), 2) if valid_daily else None
    complete = bool(valid_daily) and len(valid_daily) == expected
    portfolio_meta["quote_coverage_count"] = sum(1 for row in positions if _row_has_valid_quote(row))
    portfolio_meta["daily_pnl"] = covered if complete else None
    portfolio_meta["daily_pnl_coverage_count"] = len(valid_daily)
    portfolio_meta["covered_daily_pnl_cny"] = covered
    for account in accounts:
        account_id = account.get("account_id")
        account_positions = [
            row for row in positions if str(row.get("account_id") or "") == str(account_id)
        ]
        account_valid = [
            float(row["daily_pnl"])
            for row in account_positions
            if _row_has_valid_daily_pnl(row)
        ]
        account_covered = round(sum(account_valid), 2) if account_valid else None
        account_complete = bool(account_valid) and len(account_valid) == len(account_positions)
        account["daily_pnl"] = account_covered if account_complete else None
        account["daily_pnl_coverage_count"] = len(account_valid)
        account["covered_daily_pnl_cny"] = account_covered


def _cached_quotes_payload(
    cached: dict[str, Any], age: float, last_error: str, refreshing: bool
) -> dict[str, Any]:
    payload = copy.deepcopy(cached)
    status = _market_status(_payload_quote_time(payload))
    payload["market_status"] = status
    live_window = status["state"] == "open"
    for row in payload.get("positions") or []:
        row["is_stale"] = _row_quote_is_stale(row, live_window)
        row["quote_status"] = (
            "snapshot"
            if row.get("data_source") == "持仓快照"
            else "official_fallback"
            if row.get("data_source") == "同花顺金融数据服务（备援）" and not row["is_stale"]
            else "stale"
            if row["is_stale"]
            else "live"
        )
    for row in payload.get("watchlist") or []:
        row["is_stale"] = _row_quote_is_stale(row, live_window)
        row["quote_status"] = (
            "missing"
            if row.get("price") is None
            else "official_fallback"
            if row.get("data_source") == "同花顺金融数据服务（备援）" and not row["is_stale"]
            else "stale"
            if row["is_stale"]
            else "live"
        )
    for row in ((payload.get("markets") or {}).get("indices") or []):
        row["is_stale"] = _row_quote_is_stale(row, live_window)
    for view in (payload.get("portfolio_views") or {}).values():
        if not isinstance(view, dict):
            continue
        for row in view.get("positions") or []:
            row["is_stale"] = _row_quote_is_stale(row, live_window)
            row["quote_status"] = (
                "snapshot"
                if row.get("data_source") == "持仓快照"
                else "official_fallback"
                if row.get("data_source") == "同花顺金融数据服务（备援）" and not row["is_stale"]
                else "stale"
                if row["is_stale"]
                else "live"
            )
        view_portfolio = view.get("portfolio")
        if isinstance(view_portfolio, dict):
            _recompute_position_block_freshness(
                view_portfolio,
                [row for row in view.get("positions") or [] if isinstance(row, dict)],
                view_portfolio.get("accounts") if isinstance(view_portfolio.get("accounts"), list) else [],
                live_window,
            )
    portfolio_meta = payload.get("portfolio")
    if isinstance(portfolio_meta, dict):
        _recompute_position_block_freshness(
            portfolio_meta,
            [row for row in payload.get("positions") or [] if isinstance(row, dict)],
            portfolio_meta.get("accounts") if isinstance(portfolio_meta.get("accounts"), list) else [],
            live_window,
        )
    # Keep the tab-bar summaries consistent with the recomputed views.
    for item in payload.get("portfolios") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("portfolio_id") or "")
        source = (
            portfolio_meta
            if pid == MAIN_PORTFOLIO_ID
            else ((payload.get("portfolio_views") or {}).get(pid) or {}).get("portfolio")
        )
        if isinstance(source, dict):
            for key in ("daily_pnl", "market_value", "net_equity"):
                if key in source:
                    item[key] = source.get(key)

    errors = list(dict.fromkeys([*(payload.get("errors") or []), *([last_error] if last_error else [])]))
    expired = bool(live_window and age > QUOTE_MAX_STALE_SECONDS)
    if expired:
        errors.append("实时行情已超过 5 分钟未成功更新")
    payload["errors"] = list(dict.fromkeys(errors))
    payload["ok"] = bool(payload.get("ok")) and not expired
    payload["cache"] = {
        "age_seconds": round(max(0.0, age), 2),
        "ttl_seconds": QUOTE_TTL_SECONDS,
        "max_stale_seconds": QUOTE_MAX_STALE_SECONDS,
        "source": "memory" if age < QUOTE_TTL_SECONDS else "stale-memory",
        "fresh": age < QUOTE_TTL_SECONDS and not status["feed_stale"],
        "refreshing": refreshing,
        "expired": expired,
        "degraded": bool(payload["errors"]) or status["feed_stale"] or expired,
        "errors": payload["errors"],
    }
    return payload


def _scoped_quotes_view(payload: dict[str, Any], portfolio_id: str) -> dict[str, Any]:
    """Swap the main portfolio section for a user-defined portfolio's view.

    Market modules (indices/breadth/globals), the watchlist and the source list
    stay shared and identical across portfolios.  The bulky per-portfolio views
    map is never served back to clients.
    """

    wanted = str(portfolio_id or MAIN_PORTFOLIO_ID)
    result = copy.deepcopy(payload)
    views = result.pop("portfolio_views", None) or {}
    if wanted == MAIN_PORTFOLIO_ID:
        return result
    view = views.get(wanted)
    if not isinstance(view, dict):
        entry = _resolve_portfolio_entry(wanted)
        if entry is None:
            raise PortfolioNotFound(f"组合不存在或没有行情视图：{wanted}")
        result["portfolio"] = {
            "portfolio_id": wanted,
            "label": entry["label"],
            "as_of": entry["config"].get("as_of"),
            "position_count": len(entry["positions"]),
        }
        result["positions"] = []
        result["ok"] = False
        result["errors"] = list(dict.fromkeys([*(result.get("errors") or []), "该组合的行情视图正在构建中"]))
        return result
    result["portfolio"] = copy.deepcopy(view.get("portfolio") or {})
    result["positions"] = copy.deepcopy(view.get("positions") or [])
    result["portfolio_id"] = wanted
    result["portfolio_label"] = str(result["portfolio"].get("label") or wanted)
    result["account_snapshot"] = {
        "as_of": (view.get("portfolio") or {}).get("as_of"),
        "broker_live": False,
        "note": "数量、现金和负债来自用户自定义组合快照；实时价格不等于实时持仓。",
    }
    # Position advice is bound to the primary portfolio only; never leak its
    # actions into a user-defined portfolio view.
    result.pop("position_advice", None)
    for row in result.get("positions") or []:
        if isinstance(row, dict):
            row.pop("current_advice", None)
            row.pop("advice_time", None)
            row.pop("advice", None)
    return result


def _record_intraday_pnl_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Best-effort append of one verified live-quote snapshot.

    The quote cache remains usable when the chart ledger cannot be written.  The
    chart endpoint independently fails closed on an invalid/tampered ledger.
    """

    attempted_at = _now()
    with INTRADAY_PNL_LOCK:
        INTRADAY_PNL_STATE["last_attempt_at"] = attempted_at.isoformat(timespec="seconds")
    try:
        portfolio, _, config_mtime_ns, config_error = _portfolio_snapshot()
        payload_mtime_ns = int(
            ((payload.get("portfolio") or {}).get("config_mtime_ns") or 0)
        )
        if config_error or payload_mtime_ns != config_mtime_ns:
            raise RuntimeError("持仓配置与行情快照版本不一致")
        result = INTRADAY_PNL_STORE.record(
            portfolio,
            payload,
            observed_at=attempted_at,
        )
        with INTRADAY_PNL_LOCK:
            INTRADAY_PNL_STATE["last_result"] = {
                key: copy.deepcopy(result.get(key))
                for key in ("ok", "recorded", "status", "reason", "trade_date", "observed_at")
            }
            INTRADAY_PNL_STATE["last_error"] = ""
            if result.get("recorded"):
                INTRADAY_PNL_STATE["last_recorded_at"] = str(
                    result.get("observed_at") or attempted_at.isoformat(timespec="seconds")
                )
        return result
    except Exception as exc:
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        with INTRADAY_PNL_LOCK:
            INTRADAY_PNL_STATE["last_error"] = error
            INTRADAY_PNL_STATE["last_result"] = {
                "ok": False,
                "recorded": False,
                "status": "error",
                "reason": type(exc).__name__,
            }
        return {
            "ok": False,
            "recorded": False,
            "status": "error",
            "reason": type(exc).__name__,
        }


EXTRA_INTRADAY_STORES: dict[str, Any] = {}
EXTRA_DAILY_PNL_STORES: dict[str, Any] = {}
PORTFOLIO_STORES_LOCK = threading.Lock()


def _intraday_store_for(portfolio_id: str) -> Any:
    """One integrity-chained ledger directory per portfolio; main keeps its own."""

    if portfolio_id == MAIN_PORTFOLIO_ID:
        return INTRADAY_PNL_STORE
    with PORTFOLIO_STORES_LOCK:
        store = EXTRA_INTRADAY_STORES.get(portfolio_id)
        if store is None:
            store = IntradayPnlStore(INTRADAY_PNL_DATA_DIR / "portfolios" / portfolio_id)
            EXTRA_INTRADAY_STORES[portfolio_id] = store
        return store


def _daily_pnl_store_for(portfolio_id: str) -> Any:
    """One bounded, fingerprint-bound daily P&L cache file per portfolio."""

    if portfolio_id == MAIN_PORTFOLIO_ID:
        return DAILY_PNL_HISTORY_STORE
    with PORTFOLIO_STORES_LOCK:
        store = EXTRA_DAILY_PNL_STORES.get(portfolio_id)
        if store is None:
            store = DailyPnlHistoryStore(
                DAILY_PNL_HISTORY_CACHE_PATH.with_name(
                    f"daily-pnl-history-{portfolio_id}.json"
                )
            )
            EXTRA_DAILY_PNL_STORES[portfolio_id] = store
        return store


def _record_extra_intraday_snapshots(payload: dict[str, Any]) -> None:
    """Record one intraday point per user-defined portfolio from the same quotes."""

    views = payload.get("portfolio_views") or {}
    if not views:
        return
    _, extras_mtime, extras_error = _extra_portfolios_snapshot()
    if extras_error:
        return
    for entry in _all_portfolio_entries()[1:]:
        pid = entry["portfolio_id"]
        view = views.get(pid)
        if not isinstance(view, dict):
            continue
        try:
            view_mtime = int(((view.get("portfolio") or {}).get("config_mtime_ns") or 0))
            if view_mtime != extras_mtime:
                raise RuntimeError("附加组合配置与行情快照版本不一致")
            scoped = {
                **payload,
                "portfolio": view.get("portfolio") or {},
                "positions": view.get("positions") or [],
            }
            _intraday_store_for(pid).record(
                entry["config"], scoped, observed_at=_now()
            )
        except Exception:
            # Extra-portfolio ledgers are best-effort and must never block the
            # primary portfolio's recording or the quote publish path.
            continue


PERFORMANCE_LEDGER_MAX_DAYS = 400
PERFORMANCE_LEDGER_LOCK = threading.Lock()


def _read_performance_ledger() -> dict[str, Any]:
    try:
        document = _read_json(PORTFOLIO_PERFORMANCE_PATH)
    except Exception:
        return {"schema_version": 1, "records": {}}
    records = document.get("records")
    if not isinstance(records, dict):
        return {"schema_version": 1, "records": {}}
    return {"schema_version": 1, "records": records}


def _record_portfolio_performance(payload: dict[str, Any]) -> None:
    """Keep one daily performance row per portfolio for cross-portfolio ranking."""

    views: dict[str, Any] = {MAIN_PORTFOLIO_ID: payload.get("portfolio") or {}}
    for pid, view in (payload.get("portfolio_views") or {}).items():
        if isinstance(view, dict):
            views[pid] = view.get("portfolio") or {}
    today = _now().date().isoformat()
    dirty = False
    with PERFORMANCE_LEDGER_LOCK:
        document = _read_performance_ledger()
        records = document["records"]
        labels = {MAIN_PORTFOLIO_ID: str(payload.get("portfolio_label") or MAIN_PORTFOLIO_LABEL)}
        for pid, view_summary in views.items():
            market_value = _float(view_summary.get("market_value"))
            if market_value is None:
                continue
            net_equity = _float(view_summary.get("net_equity"))
            daily_pnl = _float(view_summary.get("daily_pnl"))
            previous_value = (
                market_value - daily_pnl if daily_pnl is not None else None
            )
            row = {
                "date": today,
                "label": str(view_summary.get("label") or pid),
                "market_value": round(market_value, 2),
                "net_equity": None if net_equity is None else round(net_equity, 2),
                "daily_pnl": None if daily_pnl is None else round(daily_pnl, 2),
                "daily_pnl_pct": (
                    round(daily_pnl / previous_value * 100, 4)
                    if daily_pnl is not None and previous_value
                    else None
                ),
                "position_count": int(view_summary.get("position_count") or 0),
            }
            series = [
                item
                for item in (records.get(pid) or [])
                if isinstance(item, dict) and item.get("date") != today
            ]
            if not series or series[-1].get("date") != today:
                series.append(row)
            series = series[-PERFORMANCE_LEDGER_MAX_DAYS:]
            if records.get(pid) != series:
                records[pid] = series
                dirty = True
            labels[pid] = row["label"]
        if dirty:
            document["labels"] = labels
            document["updated_at"] = _iso_now()
            try:
                _atomic_write_json(PORTFOLIO_PERFORMANCE_PATH, document)
            except Exception:
                pass


def _parse_intraday_trade_date(value: str | date_type | None) -> date_type:
    if value is None or value == "":
        return _now().date()
    if isinstance(value, datetime):
        raise ValueError("date must use YYYY-MM-DD")
    if isinstance(value, date_type):
        result = value
    else:
        raw = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise ValueError("date must use YYYY-MM-DD")
        try:
            result = date_type.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("date must be a valid calendar date") from exc
    if result > _now().date():
        raise ValueError("date cannot be in the future")
    return result


def _quotes_holdings_prices() -> tuple[list[dict[str, Any]], dict[str, float], float]:
    """从行情快照持仓行提取（持仓列表, 最新价表, 组合市值）；过滤无效行。"""

    quotes = get_quotes_payload()
    holdings: list[dict[str, Any]] = []
    prices: dict[str, float] = {}
    mv = 0.0
    for row in quotes.get("positions") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or row.get("symbol") or "")
        quantity = int(row.get("quantity") or 0)
        price = _float(row.get("price"))
        if not code or quantity <= 0 or not price or price <= 0:
            continue
        holdings.append(
            {"symbol": code, "name": str(row.get("name") or code), "quantity": quantity}
        )
        prices[code] = price
        mv += quantity * price
    return holdings, prices, mv


def _agg_t_signal_payload(payload: dict[str, Any], now: datetime) -> dict[str, Any]:
    """组合反T信号：聚合曲线状态机 + 示例仓位委托清单（估算，非投资建议）。"""

    holdings, prices, mv = _quotes_holdings_prices()

    th_bp = _agg_t_threshold_bp(mv)
    signal = agg_t_signal_module.compute_signal(
        payload.get("total_series") or [], mv, th_bp, now=now
    )
    signal["mv"] = round(mv, 0) if mv > 0 else None
    signal["th_source"] = "回测校准：0.75×(0.6×近15个有效交易日振幅中位)，夹[4,30]bp"

    action = None
    if signal.get("phase") == "triggered":
        action = "sell"
    elif signal.get("phase") == "closed" and signal.get("completed"):
        action = "buy"
    orders = None
    if action and holdings:
        orders = agg_t_signal_module.suggested_orders(holdings, prices, action)
    signal["order_action"] = action
    signal["orders"] = orders
    signal["note"] = (
        "组合反T信号为估算回放引擎（回测校准见 scripts/agg_t_backtest.py）；"
        "委托数量为示例仓位规则，非投资建议，成交以券商实际委托为准。"
    )
    return signal


_AGG_AMP_CACHE: dict[str, tuple[str, float, bool]] = {}
_AGG_AMP_CACHE_LOCK = threading.Lock()
_AGG_T_HISTORY_DAYS = 15


def _agg_day_amplitude(trade_date: date_type) -> tuple[float, bool] | None:
    """(当日聚合曲线振幅CNY, 开盘参考是否可信)。

    阈值扫描只回溯历史日（当日不参与），历史账本 append-only 不可变，
    因此先查缓存再读文件；否则每次轮询都要全量重读校验十几天的账本。
    """

    key = trade_date.isoformat()
    with _AGG_AMP_CACHE_LOCK:
        cached = _AGG_AMP_CACHE.get(key)
    if cached is not None:
        return cached[1], cached[2]
    try:
        stored = INTRADAY_PNL_STORE.read_day(trade_date)
    except Exception:
        return None
    active = str((stored.get("latest_point") or {}).get("series_id") or "")
    points = [
        (str(p.get("observed_at") or ""), float(p["daily_pnl_cny"]))
        for p in (stored.get("total") or {}).get("points") or []
        if p.get("daily_pnl_cny") is not None
        and (not active or str(p.get("series_id") or "") == active)
    ]
    if len(points) < 2:
        return None
    try:
        first_dt = datetime.fromisoformat(points[0][0]).astimezone(CN_TZ)
    except ValueError:
        return None
    open_ok = first_dt.time() <= daytime(9, 35)
    values = [pnl for _, pnl in points]
    amplitude = max(values) - min(values)
    with _AGG_AMP_CACHE_LOCK:
        if len(_AGG_AMP_CACHE) > 4096:
            _AGG_AMP_CACHE.clear()
        _AGG_AMP_CACHE[key] = ("", amplitude, open_ok)
    return amplitude, open_ok


def _agg_t_threshold_bp(mv: float) -> float:
    """回测校准阈值：0.75 × clamp(0.6×近15个有效交易日振幅中位, [4,30]bp)。"""

    if mv <= 0:
        return agg_t_signal_module.DEFAULT_TH_BP
    today = _now().date()
    amplitudes: list[float] = []
    for offset in range(1, 40):
        rec = _agg_day_amplitude(today - timedelta(days=offset))
        if rec and rec[1]:
            amplitudes.append(rec[0] / mv * 1e4)
        if len(amplitudes) >= _AGG_T_HISTORY_DAYS:
            break
    if len(amplitudes) < 5:
        return agg_t_signal_module.DEFAULT_TH_BP
    base = min(30.0, max(4.0, 0.6 * sorted(amplitudes)[len(amplitudes) // 2]))
    return round(0.75 * base, 1)


# ---------------------------------------------------------------------------
# 自动模拟炒股（独立模块 scripts/auto_paper_trading.py）：
# 多策略各自在独立模拟账户按信号自动买卖；此处仅做依赖注入与路由转接。
# ---------------------------------------------------------------------------

PAPER_T_ACTION_LOCK = threading.Lock()


def _configure_auto_paper() -> None:
    """把行情账本/持仓行情/时钟/锁注入自动模拟交易模块（main() 中调用）。"""

    auto_paper_module.configure(
        intraday_store=INTRADAY_PNL_STORE,
        quotes_holdings_fn=_quotes_holdings_prices,
        now_fn=_now,
        store_root=ROOT / "data" / "paper-trading",
        lock=PAPER_T_ACTION_LOCK,
    )


# ---------------------------------------------------------------------------
# 自动领航模拟盘（独立模块 scripts/auto_pilot.py）：
# 镜像实盘组合的波段级自动决策账户，每日收盘前决策窗口自动调仓并全程留痕。
# ---------------------------------------------------------------------------

AUTO_PILOT_LOCK = threading.Lock()


def _main_portfolio_cash_debt() -> dict[str, float]:
    """主组合现金与负债合计（仅用于模拟账户初始化镜像）。"""

    try:
        raw = _read_json(PORTFOLIO_PATH)
    except Exception:
        return {"cash": 0.0, "debt": 0.0}
    cash = 0.0
    debt = 0.0
    for account in raw.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        cash += float(account.get("cash") or 0.0)
        debt += float(account.get("debt") or 0.0)
    return {"cash": cash, "debt": debt}


def _configure_auto_pilot() -> None:
    """把行情/流派信号/统一决策快照/组合现金/时钟注入自动领航模块。"""

    auto_pilot_module.configure(
        quotes_fn=_quotes_holdings_prices,
        school_fn=get_school_signals_payload,
        decision_fn=get_decision_context,
        portfolio_fn=_main_portfolio_cash_debt,
        now_fn=_now,
        store_root=ROOT / "data" / "autopilot",
        lock=AUTO_PILOT_LOCK,
        log_fn=lambda message: None,
    )


def get_intraday_pnl_payload(
    trade_date: str | date_type | None = None,
    *,
    refresh: bool = False,
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    """Return a small chart contract without exposing local ledger paths."""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise ValueError(f"未知组合：{portfolio_id}")
    store = _intraday_store_for(portfolio_id)
    target = _parse_intraday_trade_date(trade_date)
    now = _now()
    if refresh and target == now.date():
        # The synchronous quote refresh calls the same recorder before returning.
        get_quotes_payload(force=True)
    try:
        stored = store.read_day(target)
    except Exception as exc:
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        with INTRADAY_PNL_LOCK:
            INTRADAY_PNL_STATE["last_error"] = error
        integrity_failure = isinstance(exc, (IntradayPnlError, AdviceIntegrityError))
        return {
            "schema_version": "intraday-pnl.api.v1",
            "ok": False,
            "available": False,
            "generated_at": now.isoformat(timespec="seconds"),
            "as_of": target.isoformat(),
            "market_phase": (
                str(_background_refresh_profile(now).get("phase") or "")
                if target == now.date()
                else "closed"
            ),
            "account_snapshot": {},
            "total_series": [],
            "positions": [],
            "stale": True,
            "refreshing": False,
            "error_code": "LEDGER_INTEGRITY_ERROR" if integrity_failure else "INTRADAY_PNL_UNAVAILABLE",
            "message": (
                "盘中盈亏轨迹完整性校验失败，已停止展示该轨迹。"
                if integrity_failure
                else "盘中盈亏轨迹暂不可用。"
            ),
        }

    active_series_id = str(((stored.get("latest_point") or {}).get("series_id") or ""))
    total_points = [
        {
            "time": point.get("observed_at"),
            "pnl": point.get("daily_pnl_cny"),
        }
        for point in ((stored.get("total") or {}).get("points") or [])
        if isinstance(point, dict)
        and (not active_series_id or str(point.get("series_id") or "") == active_series_id)
    ]
    positions = []
    for raw_position in stored.get("positions") or []:
        if not isinstance(raw_position, dict):
            continue
        series = [
            {
                "time": point.get("observed_at"),
                "pnl": point.get("daily_pnl_cny"),
            }
            for point in raw_position.get("points") or []
            if isinstance(point, dict)
            and (not active_series_id or str(point.get("series_id") or "") == active_series_id)
        ]
        positions.append(
            {
                "symbol": raw_position.get("thscode"),
                "name": raw_position.get("name"),
                "series": series,
            }
        )

    latest_time = str(total_points[-1].get("time") or "") if total_points else ""
    if not latest_time:
        candidate_times = [
            str(series[-1].get("time") or "")
            for item in positions
            for series in (item.get("series") or [],)
            if series
        ]
        latest_time = max(candidate_times, default="")
    latest_at = None
    if latest_time:
        try:
            latest_at = datetime.fromisoformat(latest_time).astimezone(CN_TZ)
        except (TypeError, ValueError):
            latest_at = None
    phase = (
        str(_background_refresh_profile(now).get("phase") or "")
        if target == now.date()
        else "closed"
    )
    stale = bool(
        latest_at is not None
        and phase in {"open", "auction"}
        and (now - latest_at).total_seconds() > QUOTE_MAX_STALE_SECONDS
    )
    with QUOTE_LOCK:
        refreshing = bool(QUOTE_CACHE.get("refreshing"))
    account = copy.deepcopy(stored.get("account_snapshot") or {})
    account["current_verified"] = account.get("verified") is True
    account.setdefault("broker_live", False)
    account.setdefault("values_are_estimates", True)
    ledger = {
        key: copy.deepcopy(value)
        for key, value in (stored.get("ledger") or {}).items()
        if key != "path"
    }
    available = bool(stored.get("available"))
    message = ""
    if not available:
        message = (
            "正在等待首个完整有效的盘中行情点。"
            if target == now.date() and phase in {"open", "auction"}
            else "该日期没有已记录的连续交易时段盈亏轨迹。"
        )
    elif not total_points and positions:
        message = "合计曲线因报价覆盖不完整暂不展示，可切换个股查看已覆盖轨迹。"
    agg_t_signal = None
    if target == now.date():
        try:
            agg_t_signal = _agg_t_signal_payload({"total_series": total_points}, now)
        except Exception as exc:
            agg_t_signal = {
                "ok": False,
                "phase": "error",
                "reason": _redact_error(f"{type(exc).__name__}: {exc}"),
            }
    return {
        "schema_version": "intraday-pnl.api.v1",
        "ok": True,
        "available": available,
        "portfolio_id": portfolio_id,
        "portfolio_label": entry["label"],
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": target.isoformat(),
        "series_id": active_series_id,
        "market_phase": phase,
        "account_snapshot": account,
        "total_series": total_points,
        "positions": positions,
        "latest_coverage": copy.deepcopy(stored.get("latest_coverage") or {}),
        "ledger": ledger,
        "stale": stale,
        "refreshing": refreshing,
        "message": message,
        "agg_t_signal": agg_t_signal,
        "values_are_estimates": True,
        "valuation_basis": "account_snapshot_quantity_x_(live_price_previous_close)",
        "note": "数量来自账户快照；曲线是估算值，不等同券商实时持仓或结算盈亏。",
    }


def _intraday_retention_days() -> int:
    try:
        document = _read_json(ROOT / "config" / "data-retention-policy.json")
        days = int(
            ((document.get("policy") or {}).get("intraday_pnl") or {}).get(
                "retention_days"
            )
            or 0
        )
        if days > 0:
            return days
    except Exception:
        pass
    return 45


def _intraday_ledger_dates(data_dir: Path) -> list[date_type]:
    """Trade dates that have a local intraday P&L ledger file."""

    result: list[date_type] = []
    try:
        entries = list(data_dir.iterdir())
    except OSError:
        return result
    for path in entries:
        match = re.fullmatch(r"intraday-pnl-(\d{4}-\d{2}-\d{2})\.jsonl", path.name)
        if not match:
            continue
        try:
            result.append(date_type.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return sorted(set(result))


def get_intraday_pnl_dates_payload(
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    """List trade dates whose intraday P&L ledger can be charted."""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise ValueError(f"未知组合：{portfolio_id}")
    store = _intraday_store_for(portfolio_id)
    dates = _intraday_ledger_dates(store.data_dir)
    return {
        "schema_version": "intraday-pnl-dates.api.v1",
        "ok": True,
        "portfolio_id": portfolio_id,
        "portfolio_label": entry["label"],
        "generated_at": _now().isoformat(timespec="seconds"),
        "dates": [item.isoformat() for item in dates],
        "retention_days": _intraday_retention_days(),
        "note": "日期为本地盘中盈亏台账已记录的交易日；超过保留期的台账会被定期清理。",
    }


INTRADAY_BACKFILL_LOCK = threading.Lock()


def _intraday_backfill_ledger_holdings(
    portfolio_id: str, trade_date: date_type
) -> list[dict[str, Any]] | None:
    """该日台账最后一条记录的持仓（数量口径与所展示曲线一致）；无记录返回 None。"""

    try:
        stored = _intraday_store_for(portfolio_id).read_day(trade_date)
    except Exception:
        return None
    latest = stored.get("latest_point") if isinstance(stored, dict) else None
    positions = latest.get("positions") if isinstance(latest, dict) else None
    if not isinstance(positions, list):
        return None
    holdings: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        thscode = str(position.get("thscode") or "")
        try:
            quantity = int(position.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if not thscode or quantity <= 0:
            continue
        holdings.append(
            {
                "thscode": thscode,
                "name": str(position.get("name") or thscode),
                "quantity": quantity,
            }
        )
    return holdings or None


def _intraday_backfill_config_holdings(portfolio_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """缺失日按当前组合配置静态回算（与"每日总持仓盈亏"同口径）。"""

    if portfolio_id == MAIN_PORTFOLIO_ID:
        portfolio, _positions, _mtime_ns, config_error = _portfolio_snapshot()
        if config_error:
            raise RuntimeError("当前组合配置不可用，无法静态回算缺失交易日")
    else:
        portfolio = entry["config"]
    return intraday_backfill_module.aggregate_holdings_current(portfolio)


def _intraday_backfill_market_value(backfill_doc: dict[str, Any]) -> float | None:
    total = 0.0
    any_value = False
    for item in (backfill_doc.get("per_symbol") or {}).values():
        last_close = item.get("last_close")
        quantity = item.get("quantity") or 0
        if isinstance(last_close, (int, float)) and last_close > 0 and quantity:
            total += float(last_close) * int(quantity)
            any_value = True
    return total if any_value else None


def _backfill_payload_meta(
    backfill_doc: dict[str, Any] | None,
    check: dict[str, Any] | None,
    *,
    points_added: int = 0,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "available": backfill_doc is not None,
        "source": (backfill_doc or {}).get("source") or "腾讯财经5分钟K线(未复权)+前复权日线",
        "window": (backfill_doc or {}).get("window") or {},
        "fetched_at": (backfill_doc or {}).get("fetched_at") or "",
        "points_added": points_added,
        "values_are_estimates": True,
        "check": check or {},
        "reason": reason,
        "note": "缺失时段由腾讯5分钟K线按当日持仓静态回算补齐（估算值）；可疑点位仅在展示层按重建值矫正，原始账本未改动。",
    }


def _backfill_only_payload(
    entry: dict[str, Any],
    portfolio_id: str,
    target: date_type,
    series_doc: dict[str, Any],
    now: datetime,
    windows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    positions = []
    for symbol, item in sorted((series_doc.get("per_symbol") or {}).items()):
        points = item.get("points") or []
        if not points:
            continue
        position = {
            "symbol": symbol,
            "name": str(item.get("name") or symbol),
            "series": [
                {"time": point["time"], "pnl": point["pnl"], "source": "backfill"}
                for point in points
            ],
        }
        window_doc = (windows or {}).get(symbol)
        if window_doc is not None:
            position["t_plan"] = _intraday_t_plan_for(
                symbol,
                window_doc,
                int(item.get("quantity") or 0),
                target.isoformat(),
            )
        positions.append(position)
    total_points = [
        {"time": point["time"], "pnl": point["pnl"], "source": "backfill"}
        for point in (series_doc.get("total") or {}).get("points") or []
    ]
    return {
        "schema_version": "intraday-pnl.api.v1",
        "ok": True,
        "available": bool(total_points),
        "portfolio_id": portfolio_id,
        "portfolio_label": entry["label"],
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": target.isoformat(),
        "series_id": "",
        "market_phase": "closed",
        "account_snapshot": {
            "current_verified": None,
            "broker_live": False,
            "values_are_estimates": True,
        },
        "total_series": total_points,
        "positions": positions,
        "latest_coverage": {},
        "ledger": {
            "record_count": 0,
            "head_hash": "",
            "integrity_verified": True,
            "recorded": False,
        },
        "stale": False,
        "refreshing": False,
        "message": "该日无盘中台账记录；曲线由腾讯5分钟K线静态回算重建，为估算值。",
        "values_are_estimates": True,
        "data_origin": "backfill",
        "backfill": _backfill_payload_meta(None, None, reason="backfill_only"),
    }


def get_intraday_pnl_history_payload(
    trade_date: str | date_type | None = None,
    *,
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    """历史分时读取：已记录轨迹 + 本地回补窗口合并（不触发上游网络）。"""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise ValueError(f"未知组合：{portfolio_id}")
    target = _parse_intraday_trade_date(trade_date)
    payload = get_intraday_pnl_payload(target, refresh=False, portfolio_id=portfolio_id)
    if payload.get("ok") is False or payload.get("error_code") == "LEDGER_INTEGRITY_ERROR":
        return payload

    try:
        holdings = _intraday_backfill_ledger_holdings(portfolio_id, target)
        source_of_holdings = "ledger"
        if holdings is None:
            holdings = _intraday_backfill_config_holdings(portfolio_id, entry)
            source_of_holdings = "config"
        windows: dict[str, Any] = {}
        for thscode in sorted({item["thscode"] for item in holdings}):
            cached = intraday_backfill_module.load_symbol_window(INTRADAY_BACKFILL_DATA_DIR, thscode)
            if cached is not None:
                windows[thscode] = cached
        series_doc = intraday_backfill_module.compose_day_series(
            windows, holdings, target.isoformat()
        )
    except Exception:
        payload["backfill"] = _backfill_payload_meta(None, None, reason="backfill_error")
        return payload

    if not series_doc.get("buildable"):
        reason = "no_window" if not windows else "no_bars_for_date"
        payload["backfill"] = _backfill_payload_meta(None, None, reason=reason)
        if not payload.get("available"):
            if not windows:
                payload["message"] = "本地尚无回补数据；点击「补全缺失时段」可从腾讯5分钟K线重建该日曲线。"
            else:
                payload["message"] = "该日在回补窗口之外或无5分钟数据，无法重建。"
        return payload

    backfill_series: dict[str, list[dict[str, Any]]] = {
        "total": (series_doc.get("total") or {}).get("points") or []
    }
    for symbol, item in (series_doc.get("per_symbol") or {}).items():
        backfill_series[symbol] = item.get("points") or []

    if payload.get("available"):
        recorded_series: dict[str, list[dict[str, Any]]] = {
            "total": list(payload.get("total_series") or [])
        }
        for position in payload.get("positions") or []:
            recorded_series[str(position.get("symbol") or "")] = list(position.get("series") or [])
        check = intraday_backfill_module.compare_and_correct(
            recorded_series,
            backfill_series,
            market_value_estimate=_intraday_backfill_market_value(series_doc),
        )
        merged = intraday_backfill_module.apply_backfill_merge(recorded_series, backfill_series, check)
        payload["total_series"] = merged.get("total") or payload.get("total_series")
        date_iso = target.isoformat()
        for position in payload.get("positions") or []:
            symbol = str(position.get("symbol") or "")
            series = merged.get(symbol)
            if series is not None:
                position["series"] = series
            window_doc = windows.get(symbol)
            per_symbol_doc = (series_doc.get("per_symbol") or {}).get(symbol) or {}
            if window_doc is not None:
                position["t_plan"] = _intraday_t_plan_for(
                    symbol,
                    window_doc,
                    int(per_symbol_doc.get("quantity") or 0),
                    date_iso,
                )
        points_added = sum(
            1
            for points in merged.values()
            for point in points
            if point.get("source") == "backfill"
        )
        payload["backfill"] = _backfill_payload_meta(
            {"source": "腾讯财经5分钟K线(未复权)+前复权日线", "window": _backfill_window_span(windows), "fetched_at": max(
                (str(doc.get("fetched_at") or "") for doc in windows.values()), default=""
            )},
            check,
            points_added=points_added,
        )
        payload["holdings_source"] = source_of_holdings
        return payload

    backfill_only = _backfill_only_payload(entry, portfolio_id, target, series_doc, _now(), windows)
    backfill_only["backfill"] = _backfill_payload_meta(
        {"source": "腾讯财经5分钟K线(未复权)+前复权日线", "window": _backfill_window_span(windows), "fetched_at": max(
            (str(doc.get("fetched_at") or "") for doc in windows.values()), default=""
        )},
        None,
    )
    backfill_only["holdings_source"] = source_of_holdings
    return backfill_only


def _backfill_window_span(windows: dict[str, Any]) -> dict[str, Any]:
    starts = [str((doc.get("window") or {}).get("start") or "") for doc in windows.values()]
    ends = [str((doc.get("window") or {}).get("end") or "") for doc in windows.values()]
    starts = [value for value in starts if value]
    ends = [value for value in ends if value]
    return {
        "start": min(starts) if starts else "",
        "end": max(ends) if ends else "",
        "symbols": len(windows),
    }


_T_SHARE_MAX_NOTIONAL = 20000.0


def _t_plan_shares(quantity: int, price: Any, thscode: str) -> int:
    """示例仓位规则：单笔 ≤2万元 且 ≤1/3 持仓，按整手取整（科创板 200 股起）。"""

    if quantity <= 0 or not isinstance(price, (int, float)) or price <= 0:
        return 0
    lot = 200 if thscode.startswith("688") else 100
    by_notional = int(_T_SHARE_MAX_NOTIONAL / float(price)) // lot * lot
    by_position = quantity // 3 // lot * lot
    shares = max(0, min(by_notional, by_position))
    if shares == 0 and quantity >= lot:
        shares = lot
    return min(shares, quantity)


def _intraday_t_plan_for(
    thscode: str,
    window: dict[str, Any],
    quantity: int,
    trade_date: str,
) -> dict[str, Any] | None:
    """该股该日的做T标记数据：适格结论、方向、买卖点与建议股数。"""

    try:
        profile = intraday_backfill_module.t_profile(window)
    except Exception:
        return None
    if not profile.get("eligible"):
        return {
            "suitable": False,
            "reason": profile.get("reason") or "样本不足",
            "days": profile.get("days", 0),
        }
    direction = str(profile.get("direction") or "long")
    trades = intraday_backfill_module.t_trades_for_date(
        window, trade_date, float(profile.get("th_pct") or 1.0), direction
    )
    for trade in trades:
        shares = _t_plan_shares(quantity, trade.get("entry_price"), thscode)
        trade["shares"] = shares
        trade["notional"] = round(shares * float(trade.get("entry_price") or 0), 0)
        trade["action_entry"] = "buy" if direction == "long" else "sell"
        trade["action_exit"] = "sell" if direction == "long" else "buy"
    return {
        "suitable": bool(profile.get("suitable")),
        "reason": profile.get("reason") or "",
        "direction": direction,
        "direction_label": "正T（先买后卖）" if direction == "long" else "反T（先卖后买）",
        "th_pct": profile.get("th_pct"),
        "amp_med_pct": profile.get("amp_med_pct"),
        "ac1": profile.get("ac1"),
        "vr6": profile.get("vr6"),
        "double_sided_pct": profile.get("double_sided_pct"),
        "sim": {
            "long": {k: profile["sim_long"].get(k) for k in ("tries", "win_rate", "avg_net_bps", "per_day_net")},
            "short": {k: profile["sim_short"].get(k) for k in ("tries", "win_rate", "avg_net_bps", "per_day_net")},
        },
        "trades": trades,
        "sizing": "示例仓位：单笔不超过2万元且不超过持仓1/3，整手取整；非投资建议",
    }


def post_intraday_pnl_backfill(
    portfolio_id: str,
    dates: list[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """拉取/刷新个股5分钟窗口并逐日生成回补数据（写派生缓存，不动台账）。"""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise ValueError(f"未知组合：{portfolio_id}")
    if not isinstance(dates, list) or not dates:
        raise ValueError("dates 必须是非空日期数组")
    if len(dates) > intraday_backfill_module.MAX_BACKFILL_DATES:
        raise ValueError(f"单次最多回补 {intraday_backfill_module.MAX_BACKFILL_DATES} 个交易日")
    parsed_dates = sorted(set(_parse_intraday_trade_date(value) for value in dates))

    per_date_holdings: dict[str, list[dict[str, Any]]] = {}
    symbols: set[str] = set()
    for trade_date in parsed_dates:
        holdings = _intraday_backfill_ledger_holdings(portfolio_id, trade_date)
        if holdings is None:
            holdings = _intraday_backfill_config_holdings(portfolio_id, entry)
        per_date_holdings[trade_date.isoformat()] = holdings
        symbols.update(item["thscode"] for item in holdings)

    if not INTRADAY_BACKFILL_LOCK.acquire(blocking=False):
        return {
            "schema_version": "intraday-pnl-backfill.api.v1",
            "ok": False,
            "error_code": "BACKFILL_BUSY",
            "error": "已有回补任务在进行中，请稍后再试。",
        }
    try:
        windows = intraday_backfill_module.ensure_symbol_windows(
            INTRADAY_BACKFILL_DATA_DIR,
            sorted(symbols),
            [value.isoformat() for value in parsed_dates],
            force=force,
        )
    except intraday_backfill_module.IntradayPnlBackfillError as exc:
        return {
            "schema_version": "intraday-pnl-backfill.api.v1",
            "ok": False,
            "error_code": getattr(exc, "code", "INTRADAY_PNL_BACKFILL_ERROR"),
            "error": f"回补数据源不可用：{exc}",
        }
    finally:
        INTRADAY_BACKFILL_LOCK.release()
    window_span = _backfill_window_span(windows)
    results = []
    now = _now()
    for trade_date in parsed_dates:
        date_iso = trade_date.isoformat()
        holdings = per_date_holdings[date_iso]
        in_window = bool(window_span.get("start")) and window_span["start"] <= date_iso <= window_span["end"]
        if not in_window:
            results.append(
                {
                    "date": date_iso,
                    "ok": False,
                    "status": "out_of_window",
                    "total_points": 0,
                    "covered_symbols": 0,
                    "required_symbols": len(holdings),
                    "message": f"外部数据窗口为 {window_span.get('start')} ~ {window_span.get('end')}，该日在窗口之外。",
                }
            )
            continue
        series_doc = intraday_backfill_module.compose_day_series(windows, holdings, date_iso)
        built = bool(series_doc.get("buildable"))
        results.append(
            {
                "date": date_iso,
                "ok": built,
                "status": "built" if built else "no_bars",
                "total_points": len((series_doc.get("total") or {}).get("points") or []),
                "covered_symbols": (series_doc.get("total") or {}).get("covered_symbols", 0),
                "required_symbols": (series_doc.get("total") or {}).get("required_symbols", 0),
                "message": "" if built else "该日无5分钟数据（可能休市或停牌）。",
            }
        )
    return {
        "schema_version": "intraday-pnl-backfill.api.v1",
        "ok": any(item["ok"] for item in results),
        "portfolio_id": portfolio_id,
        "portfolio_label": entry["label"],
        "generated_at": now.isoformat(timespec="seconds"),
        "results": results,
        "window": window_span,
        "source": "腾讯财经5分钟K线(未复权)+前复权日线",
        "note": "回补为静态估算：数量取该日台账持仓（无台账日取当前配置），价格取5分钟收盘价；原始台账保持不变。",
    }


def get_daily_pnl_history_payload(
    start: str | date_type | None = None,
    *,
    refresh: bool = False,
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    """Return portfolio-bound static daily price P&L from the bounded cache."""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise ValueError(f"未知组合：{portfolio_id}")
    if portfolio_id == MAIN_PORTFOLIO_ID:
        portfolio, _positions, _mtime_ns, config_error = _portfolio_snapshot()
        if config_error:
            raise DailyPnlHistoryUnavailable("current portfolio config is unavailable")
    else:
        portfolio = entry["config"]
    configured_start = str(portfolio.get("history_start") or "").strip()
    requested_start = (
        start.isoformat() if hasattr(start, "isoformat") else str(start or "").strip()
    )
    effective_start = (
        requested_start or configured_start or DAILY_PNL_HISTORY_DEFAULT_START_DATE
    )
    if configured_start and str(effective_start) < configured_start:
        # history_start 声明该组合自该日起才有记录；任何更早的显式起点
        # （例如前端固定的全局默认）都收紧到组合自身的起点。
        effective_start = configured_start
    payload = _daily_pnl_store_for(portfolio_id).get(
        portfolio,
        start=effective_start,
        force=refresh,
    )
    result = copy.deepcopy(payload)
    result["portfolio_id"] = portfolio_id
    result["portfolio_label"] = entry["label"]
    return result


def _position_advice_failure_payload(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "payload_type": "portfolio-position-advice.v1",
        "ok": False,
        "positions": [],
        "message": message,
    }


def _portfolio_advice_symbols(portfolio: dict[str, Any]) -> set[str]:
    return {
        str(row.get("thscode") or "")
        for account in portfolio.get("accounts") or []
        if isinstance(account, dict)
        for row in account.get("positions") or []
        if isinstance(row, dict) and row.get("thscode")
    }


def _position_advice_matches_portfolio(
    payload: dict[str, Any],
    portfolio: dict[str, Any],
    *,
    config_error: str = "",
) -> bool:
    """Return whether a successful public envelope belongs to this portfolio."""

    if payload.get("ok") is not True:
        return True
    portfolio_id = str(portfolio.get("portfolio_id") or "")
    portfolio_as_of = str(portfolio.get("as_of") or "")
    portfolio_fingerprint = portfolio_fingerprint_sha256(portfolio)
    advice_symbols = {
        str(row.get("thscode") or "")
        for row in payload.get("positions") or []
        if isinstance(row, dict) and row.get("thscode")
    }
    return bool(
        not config_error
        and str(payload.get("portfolio_id") or "") == portfolio_id
        and str(payload.get("portfolio_as_of") or "") == portfolio_as_of
        and str(payload.get("portfolio_fingerprint_sha256") or "")
        == portfolio_fingerprint
        and advice_symbols == _portfolio_advice_symbols(portfolio)
    )


def _cached_position_advice_payload(
    portfolio: dict[str, Any], *, config_error: str = ""
) -> dict[str, Any]:
    """Read only the already verified in-memory advice envelope.

    Loading the hash-chained audit ledger can take far longer than an HTTP
    request budget, so request handlers never perform that validation inline.
    The background refresher below owns all disk validation and publication.
    """

    with POSITION_ADVICE_LOCK:
        loaded = bool(POSITION_ADVICE_STATE.get("loaded"))
        refreshing = bool(POSITION_ADVICE_STATE.get("refreshing"))
        cached = copy.deepcopy(POSITION_ADVICE_STATE.get("public"))
    if not loaded or not isinstance(cached, dict):
        return _position_advice_failure_payload(
            "建议审计状态正在后台校验，暂不发布建议。"
            if refreshing
            else "建议审计状态尚未校验，等待统一快照后台刷新。"
        )
    if not _position_advice_matches_portfolio(
        cached, portfolio, config_error=config_error
    ):
        return _position_advice_failure_payload(
            "建议状态不属于当前持仓日期、数量或证券集合，等待重新生成。"
        )
    return cached


def _get_position_advice_payload(*, force_reload: bool = False) -> dict[str, Any]:
    portfolio, _, _, config_error = _portfolio_snapshot()
    if not force_reload:
        return _cached_position_advice_payload(portfolio, config_error=config_error)

    # Explicit maintenance/tests may request a synchronous reload.  Production
    # HTTP paths use the memory-only branch above and therefore never scan the
    # potentially large audit ledger.
    try:
        state = load_advice_state(
            POSITION_ADVICE_STATE_PATH,
            POSITION_ADVICE_AUDIT_PATH,
            recover=True,
        )
        payload = public_position_advice(state)
        if payload.get("ok") is not True:
            payload = _position_advice_failure_payload(
                "尚无已核验的持仓建议，等待统一快照后台刷新。"
            )
        elif not _position_advice_matches_portfolio(
            payload, portfolio, config_error=config_error
        ):
            payload = _position_advice_failure_payload(
                "建议状态不属于当前持仓日期、数量或证券集合，等待重新生成。"
            )
        error = ""
    except Exception as exc:
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        payload = _position_advice_failure_payload(
            "建议审计完整性校验失败，已停止发布旧建议。"
            if isinstance(exc, AdviceIntegrityError)
            else "当前操作建议暂不可用。"
        )
    with POSITION_ADVICE_LOCK:
        POSITION_ADVICE_STATE["loaded"] = True
        POSITION_ADVICE_STATE["public"] = copy.deepcopy(payload)
        POSITION_ADVICE_STATE["last_error"] = error
    return payload


def _update_position_advice(
    portfolio: dict[str, Any],
    *,
    quotes: dict[str, Any],
    position_news: dict[str, Any],
    market_news: dict[str, Any],
    market_state: dict[str, Any],
    risk: dict[str, Any],
    rebalance: dict[str, Any],
    input_errors: list[str],
    now: datetime,
    publish: bool = True,
    raise_errors: bool = False,
) -> dict[str, Any]:
    attempted_at = now.isoformat(timespec="seconds")
    attempted_monotonic = time.monotonic()
    with POSITION_ADVICE_LOCK:
        POSITION_ADVICE_STATE.update(
            last_attempt_at=attempted_at,
            last_attempt_monotonic=attempted_monotonic,
        )
    try:
        result = update_position_advice_store(
            portfolio,
            quotes=quotes,
            position_news=position_news,
            market_news=market_news,
            market_state=market_state,
            risk=risk,
            rebalance=rebalance,
            state_path=POSITION_ADVICE_STATE_PATH,
            audit_path=POSITION_ADVICE_AUDIT_PATH,
            input_errors=input_errors,
            now=now,
        )
        payload = public_position_advice(result.get("state"))
        if payload.get("ok") is not True:
            raise RuntimeError("持仓建议存储未返回可发布的完整结果")
        if publish:
            with POSITION_ADVICE_LOCK:
                POSITION_ADVICE_STATE.update(
                    loaded=True,
                    public=copy.deepcopy(payload),
                    last_success_at=attempted_at,
                    last_success_monotonic=time.monotonic(),
                    last_error="",
                    next_retry_monotonic=0.0,
                )
        return payload
    except Exception as exc:
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        payload = _position_advice_failure_payload(
            "建议审计完整性校验失败，已停止发布建议。"
            if isinstance(exc, AdviceIntegrityError)
            else "当前操作建议生成失败，保持暂停新增并等待人工复核。"
        )
        with POSITION_ADVICE_LOCK:
            updates: dict[str, Any] = {"last_error": error}
            if publish:
                updates.update(
                    loaded=True,
                    public=copy.deepcopy(payload),
                    next_retry_monotonic=(
                        time.monotonic() + POSITION_ADVICE_FAILURE_RETRY_SECONDS
                    ),
                )
            POSITION_ADVICE_STATE.update(updates)
        if raise_errors:
            raise
        return payload


def _position_advice_binding_is_current(
    portfolio: dict[str, Any], config_mtime_ns: int
) -> bool:
    current, _, current_mtime_ns, config_error = _portfolio_snapshot()
    return bool(
        not config_error
        and current_mtime_ns == config_mtime_ns
        and str(current.get("portfolio_id") or "")
        == str(portfolio.get("portfolio_id") or "")
        and str(current.get("as_of") or "") == str(portfolio.get("as_of") or "")
        and portfolio_fingerprint_sha256(current)
        == portfolio_fingerprint_sha256(portfolio)
    )


def _position_advice_refresh_worker(
    *,
    snapshot_id: str,
    portfolio: dict[str, Any],
    config_mtime_ns: int,
    quotes: dict[str, Any],
    position_news: dict[str, Any],
    market_news: dict[str, Any],
    market_state: dict[str, Any],
    risk: dict[str, Any],
    rebalance: dict[str, Any],
    input_errors: list[str],
) -> None:
    failure: dict[str, Any] | None = None
    try:
        if not _position_advice_binding_is_current(portfolio, config_mtime_ns):
            raise RuntimeError("持仓配置在建议刷新开始前发生变化，放弃旧快照")
        payload = _update_position_advice(
            portfolio,
            quotes=quotes,
            position_news=position_news,
            market_news=market_news,
            market_state=market_state,
            risk=risk,
            rebalance=rebalance,
            input_errors=input_errors,
            now=_now(),
            publish=False,
            raise_errors=True,
        )
        if not _position_advice_binding_is_current(portfolio, config_mtime_ns):
            raise RuntimeError("持仓配置在建议刷新期间发生变化，拒绝发布旧建议")
        if not _position_advice_matches_portfolio(payload, portfolio):
            raise RuntimeError("建议结果与调度时的持仓绑定不一致")
        completed_at = _iso_now()
        completed_monotonic = time.monotonic()
        with POSITION_ADVICE_LOCK:
            if POSITION_ADVICE_STATE.get("pending_snapshot_id") != snapshot_id:
                raise RuntimeError("建议刷新任务已被更新快照取代")
            POSITION_ADVICE_STATE.update(
                loaded=True,
                public=copy.deepcopy(payload),
                last_success_at=completed_at,
                last_success_monotonic=completed_monotonic,
                last_error="",
                next_retry_monotonic=0.0,
                last_snapshot_id=snapshot_id,
            )
    except Exception as exc:
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        failure = _position_advice_failure_payload(
            "建议审计完整性校验失败，已停止发布建议。"
            if isinstance(exc, AdviceIntegrityError)
            else "当前操作建议后台刷新失败，保持暂停新增并等待复核。"
        )
        with POSITION_ADVICE_LOCK:
            if POSITION_ADVICE_STATE.get("pending_snapshot_id") == snapshot_id:
                POSITION_ADVICE_STATE.update(
                    loaded=True,
                    public=copy.deepcopy(failure),
                    last_error=error,
                    next_retry_monotonic=(
                        time.monotonic() + POSITION_ADVICE_FAILURE_RETRY_SECONDS
                    ),
                )
    finally:
        with POSITION_ADVICE_LOCK:
            if POSITION_ADVICE_STATE.get("pending_snapshot_id") == snapshot_id:
                POSITION_ADVICE_STATE.update(
                    refreshing=False,
                    pending_snapshot_id="",
                )


def ensure_position_advice_refresh(
    snapshot_id: str,
    portfolio: dict[str, Any],
    *,
    config_mtime_ns: int,
    quotes: dict[str, Any],
    position_news: dict[str, Any],
    market_news: dict[str, Any],
    market_state: dict[str, Any],
    risk: dict[str, Any],
    rebalance: dict[str, Any],
    input_errors: list[str],
) -> bool:
    """Schedule one bound advice refresh without blocking the HTTP request."""

    pending_inputs = {
        "snapshot_id": str(snapshot_id),
        "portfolio": copy.deepcopy(portfolio),
        "config_mtime_ns": int(config_mtime_ns),
        "quotes": copy.deepcopy(quotes),
        "position_news": copy.deepcopy(position_news),
        "market_news": copy.deepcopy(market_news),
        "market_state": copy.deepcopy(market_state),
        "risk": copy.deepcopy(risk),
        "rebalance": copy.deepcopy(rebalance),
        "input_errors": copy.deepcopy(input_errors),
    }
    monotonic_now = time.monotonic()
    with POSITION_ADVICE_LOCK:
        last_attempt = float(
            POSITION_ADVICE_STATE.get("last_attempt_monotonic") or 0.0
        )
        if POSITION_ADVICE_STATE.get("refreshing"):
            return False
        if str(POSITION_ADVICE_STATE.get("last_snapshot_id") or "") == str(
            snapshot_id
        ):
            return False
        if monotonic_now < float(
            POSITION_ADVICE_STATE.get("next_retry_monotonic") or 0.0
        ):
            return False
        if (
            last_attempt > 0.0
            and monotonic_now - last_attempt
            < POSITION_ADVICE_REFRESH_INTERVAL_SECONDS
        ):
            return False
        POSITION_ADVICE_STATE.update(
            refreshing=True,
            pending_snapshot_id=str(snapshot_id),
            last_attempt_at=_iso_now(),
            last_attempt_monotonic=monotonic_now,
        )

    thread = threading.Thread(
        target=_position_advice_refresh_worker,
        kwargs=pending_inputs,
        name="dashboard-position-advice",
        daemon=True,
    )
    try:
        thread.start()
    except Exception as exc:
        with POSITION_ADVICE_LOCK:
            POSITION_ADVICE_STATE.update(
                refreshing=False,
                pending_snapshot_id="",
                last_error=_redact_error(f"{type(exc).__name__}: {exc}"),
                next_retry_monotonic=(
                    time.monotonic() + POSITION_ADVICE_FAILURE_RETRY_SECONDS
                ),
            )
        return False
    return True


def _attach_position_advice(
    payload: dict[str, Any],
    advice_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    advice = copy.deepcopy(
        advice_payload if advice_payload is not None else _get_position_advice_payload()
    )
    by_symbol = {
        str(row.get("thscode") or ""): row
        for row in advice.get("positions") or []
        if isinstance(row, dict) and row.get("thscode")
    }
    for row in result.get("positions") or []:
        if not isinstance(row, dict):
            continue
        # The quote payload may itself come from a cache that was decorated by
        # an earlier decision.  Clear it before attaching the current envelope
        # so a failed/mismatched advice refresh can never leak an old action.
        row.pop("current_advice", None)
        row.pop("advice_time", None)
        row.pop("advice", None)
        current = by_symbol.get(str(row.get("thscode") or ""))
        if not current:
            continue
        row["current_advice"] = current.get("current_advice")
        row["advice_time"] = current.get("advice_time")
        row["advice"] = {
            "text": current.get("current_advice"),
            "as_of": current.get("advice_time"),
        }
    result["position_advice"] = advice
    return result


def _quote_cache_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded, portfolio-bound last-success quote cache document."""

    if payload.get("ok") is not True:
        raise ValueError("only successful quote payloads may be persisted")
    payload_portfolio = payload.get("portfolio")
    if not isinstance(payload_portfolio, dict):
        raise ValueError("quote payload portfolio metadata is missing")
    payload_mtime = int(payload_portfolio.get("config_mtime_ns") or 0)
    portfolio, _, current_mtime, config_error = _portfolio_snapshot()
    if config_error or payload_mtime <= 0 or payload_mtime != current_mtime:
        raise ValueError("quote payload is not bound to the current portfolio config")
    quote_time = str((payload.get("market_status") or {}).get("quote_time") or "")
    quote_at = _parse_quote_datetime(quote_time)
    generated_at = str(payload.get("generated_at") or "")
    generated = _parse_quote_datetime(generated_at)
    now = _now()
    if quote_at is None or quote_at > now + timedelta(minutes=5):
        raise ValueError("quote payload quote_time is invalid")
    if not _cacheable_quote_instant(quote_at):
        raise ValueError("quote payload quote_time is outside an A-share quote session")
    if generated is None or generated > now + timedelta(minutes=5):
        raise ValueError("quote payload generated_at is invalid")
    envelope = {
        "schema_version": QUOTE_CACHE_SCHEMA_VERSION,
        "saved_at": now.isoformat(timespec="seconds"),
        "portfolio_config_mtime_ns": current_mtime,
        "portfolio_fingerprint": portfolio_fingerprint_sha256(portfolio),
        "quote_time": quote_at.isoformat(timespec="seconds"),
        "payload": copy.deepcopy(payload),
    }
    encoded_size = len(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if encoded_size > QUOTE_CACHE_MAX_BYTES:
        raise ValueError(
            f"quote cache exceeds bounded size: {encoded_size}>{QUOTE_CACHE_MAX_BYTES}"
        )
    return envelope


def _persist_quote_cache(payload: dict[str, Any]) -> None:
    """Atomically replace the single last-success quote cache file."""

    _atomic_write_json(QUOTE_CACHE_PATH, _quote_cache_envelope(payload))


def _load_persisted_quote_cache() -> bool:
    """Warm memory from a valid last-success cache without any network I/O."""

    if not QUOTE_CACHE_PATH.is_file():
        return False
    try:
        if QUOTE_CACHE_PATH.stat().st_size > QUOTE_CACHE_MAX_BYTES:
            raise ValueError("quote cache file exceeds the size limit")
        envelope = _read_json(QUOTE_CACHE_PATH)
        if envelope.get("schema_version") != QUOTE_CACHE_SCHEMA_VERSION:
            raise ValueError("quote cache schema mismatch")
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("quote cache does not contain a successful payload")
        portfolio_meta = payload.get("portfolio")
        if not isinstance(portfolio_meta, dict):
            raise ValueError("quote cache portfolio metadata is missing")
        portfolio, _, current_mtime, config_error = _portfolio_snapshot()
        if config_error:
            raise ValueError("current portfolio config is unavailable")
        expected_fingerprint = portfolio_fingerprint_sha256(portfolio)
        if (
            int(envelope.get("portfolio_config_mtime_ns") or 0) != current_mtime
            or int(portfolio_meta.get("config_mtime_ns") or 0) != current_mtime
            or str(envelope.get("portfolio_fingerprint") or "") != expected_fingerprint
        ):
            raise ValueError("quote cache belongs to a different portfolio config")
        quote_time = str((payload.get("market_status") or {}).get("quote_time") or "")
        quote_at = _parse_quote_datetime(quote_time)
        if (
            quote_at is None
            or str(envelope.get("quote_time") or "") != quote_at.isoformat(timespec="seconds")
        ):
            raise ValueError("quote cache quote_time mismatch")
        if not _cacheable_quote_instant(quote_at):
            raise ValueError("quote cache quote_time is outside an A-share quote session")
        generated = _parse_quote_datetime(payload.get("generated_at"))
        now = _now()
        if quote_at > now + timedelta(minutes=5):
            raise ValueError("quote cache quote_time is in the future")
        if generated is None or generated > now + timedelta(minutes=5):
            raise ValueError("quote cache generated_at is invalid")
        positions = payload.get("positions")
        coverage = int(portfolio_meta.get("quote_coverage_count") or 0)
        if not isinstance(positions, list) or coverage <= 0:
            raise ValueError("quote cache has no usable position coverage")
        age = max(0.0, (now - generated).total_seconds())
    except Exception:
        return False

    with QUOTE_LOCK:
        if QUOTE_CACHE.get("payload") is not None:
            return False
        QUOTE_CACHE["payload"] = copy.deepcopy(payload)
        QUOTE_CACHE["monotonic"] = time.monotonic() - age
        QUOTE_CACHE["last_error"] = ""
        QUOTE_CACHE["refreshing"] = False
    return True


def _cacheable_quote_instant(value: datetime) -> bool:
    """Accept only timestamps that can be produced while quote refresh is enabled."""

    local = value.astimezone(CN_TZ)
    is_session_date, _ = _a_share_session_date(local.date())
    if not is_session_date:
        return False
    current = local.time().replace(tzinfo=None)
    return bool(
        daytime(9, 15) <= current <= daytime(9, 25)
        or daytime(9, 30) <= current <= daytime(11, 30)
        or daytime(13, 0) <= current <= daytime(15, 0)
    )


def _configured_holding_binding(
    portfolio: dict[str, Any], configured_positions: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    account_dates = {
        str(account.get("account_id") or ""): str(
            account.get("as_of") or portfolio.get("as_of") or ""
        )
        for account in portfolio.get("accounts") or []
        if isinstance(account, dict)
    }
    result: dict[str, dict[str, Any]] = {}
    for item in configured_positions:
        thscode = str(item.get("thscode") or "").upper()
        quantity = int(item.get("quantity") or 0)
        if not thscode or quantity <= 0:
            continue
        target = result.setdefault(thscode, {"quantity": 0, "accounts": []})
        target["quantity"] += quantity
        account_id = str(item.get("account_id") or "")
        target["accounts"].append(
            (
                account_id,
                str(item.get("account_type") or ""),
                account_dates.get(account_id, str(portfolio.get("as_of") or "")),
                quantity,
            )
        )
    for target in result.values():
        target["accounts"].sort()
    return result


def _intraday_point_matches_portfolio(
    point: dict[str, Any],
    portfolio: dict[str, Any],
    configured_positions: list[dict[str, Any]],
) -> bool:
    if str(point.get("portfolio_id") or "") != str(portfolio.get("portfolio_id") or ""):
        return False
    coverage = point.get("coverage") if isinstance(point.get("coverage"), dict) else {}
    expected = _configured_holding_binding(portfolio, configured_positions)
    rows = point.get("positions") if isinstance(point.get("positions"), list) else []
    if (
        not expected
        or coverage.get("complete") is not True
        or int(coverage.get("required_count") or 0) != len(expected)
        or int(coverage.get("covered_count") or 0) != len(expected)
        or coverage.get("missing_symbols")
        or len(rows) != len(expected)
    ):
        return False
    observed = _parse_quote_datetime(point.get("observed_at"))
    if observed is None or not _cacheable_quote_instant(observed):
        return False
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return False
        thscode = str(row.get("thscode") or "").upper()
        quote_at = _parse_quote_datetime(row.get("quote_at"))
        if (
            not thscode
            or thscode in actual
            or row.get("quote_verified") is not True
            or _float(row.get("price")) is None
            or _float(row.get("last_close")) is None
            or quote_at is None
            or not _cacheable_quote_instant(quote_at)
            or quote_at.date() != observed.date()
            or quote_at > observed + timedelta(seconds=5)
        ):
            return False
        accounts = []
        for account in row.get("accounts") or []:
            if not isinstance(account, dict):
                return False
            accounts.append(
                (
                    str(account.get("account_id") or ""),
                    str(account.get("account_type") or ""),
                    str(account.get("as_of") or ""),
                    int(account.get("quantity") or 0),
                )
            )
        actual[thscode] = {
            "quantity": int(row.get("quantity") or 0),
            "accounts": sorted(accounts),
        }
    return actual == expected


def _latest_local_intraday_point(
    portfolio: dict[str, Any], configured_positions: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not INTRADAY_PNL_DATA_DIR.is_dir():
        return None
    candidates: list[date_type] = []
    for path in INTRADAY_PNL_DATA_DIR.iterdir():
        match = re.fullmatch(r"intraday-pnl-(\d{4}-\d{2}-\d{2})\.jsonl", path.name)
        if not match:
            continue
        try:
            trade_date = date_type.fromisoformat(match.group(1))
        except ValueError:
            continue
        if trade_date <= _now().date():
            candidates.append(trade_date)
    for trade_date in sorted(set(candidates), reverse=True):
        try:
            stored = INTRADAY_PNL_STORE.read_day(trade_date)
        except Exception:
            # Do not step around a corrupt newest ledger and silently use older data.
            return None
        point = stored.get("latest_point") if isinstance(stored, dict) else None
        if isinstance(point, dict) and _intraday_point_matches_portfolio(
            point, portfolio, configured_positions
        ):
            return point
    return None


def _session_close_iso(trade_date: date_type) -> str:
    return datetime.combine(trade_date, daytime(15, 0), tzinfo=CN_TZ).isoformat(
        timespec="seconds"
    )


def _previous_completed_close(
    recent: Any, trade_date: date_type
) -> float | None:
    rows = recent if isinstance(recent, list) else []
    candidates = [
        (_parse_quote_datetime(f"{item.get('date')}T15:00:00+08:00"), _float(item.get("close")))
        for item in rows
        if isinstance(item, dict)
    ]
    values = [
        value
        for parsed, value in candidates
        if parsed is not None and parsed.date() < trade_date and value is not None
    ]
    return values[-1] if values else None


def _recovered_markets_and_watchlist(
    trade_date: date_type,
    watchlist_items: list[dict[str, Any]],
    validation_provider: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    empty = {"indices": [], "breadth": {}, "global": []}
    errors: list[str] = []
    try:
        snapshot = _read_json(MARKET_SNAPSHOT_PATH)
    except Exception as exc:
        return empty, [], [f"本地日终市场快照不可用：{type(exc).__name__}"]
    if (
        str(snapshot.get("data_date") or "") != trade_date.isoformat()
        or (snapshot.get("validation") or {}).get("passed") is not True
    ):
        return empty, [], ["本地日终市场快照与盘中账本日期或校验状态不匹配"]

    quote_time = _session_close_iso(trade_date)
    raw_indices = snapshot.get("indices") if isinstance(snapshot.get("indices"), dict) else {}
    indices: list[dict[str, Any]] = []
    for symbol, name in INDEXES:
        source = raw_indices.get(name)
        if not isinstance(source, dict) or str(source.get("date") or "") != trade_date.isoformat():
            continue
        price = _float(source.get("close"))
        if price is None or price <= 0:
            continue
        previous = _previous_completed_close(source.get("recent_closes"), trade_date)
        change = None if previous is None else price - previous
        indices.append(
            {
                "symbol": symbol,
                "name": name,
                "price": price,
                "prev_close": previous,
                "change": _safe_round(change, 4),
                "change_pct": _safe_round(
                    None if change is None or not previous else change / previous * 100, 3
                ),
                "open": None,
                "high": None,
                "low": None,
                "quote_time": quote_time,
                "quote_time_raw": quote_time,
                "is_stale": False,
                "source": "本地已校验日终市场快照",
            }
        )

    raw_breadth = snapshot.get("breadth") if isinstance(snapshot.get("breadth"), dict) else {}
    combined = raw_breadth.get("沪深合计") if isinstance(raw_breadth.get("沪深合计"), dict) else {}
    exchanges = {
        name: {
            "up": _float((raw_breadth.get(name) or {}).get("up")),
            "flat": _float((raw_breadth.get(name) or {}).get("flat")),
            "down": _float((raw_breadth.get(name) or {}).get("down")),
            "total": _float((raw_breadth.get(name) or {}).get("total")),
            "amount_yi": _float((raw_breadth.get(name) or {}).get("amount_yi")),
            "quote_time": quote_time,
        }
        for name in ("上海", "深圳")
    }
    breadth = {
        "up": _float(combined.get("up")),
        "flat": _float(combined.get("flat")),
        "down": _float(combined.get("down")),
        "total": _float(combined.get("total")),
        "amount_yi": _float(combined.get("amount_yi")),
        "quote_time": quote_time,
        "exchanges": exchanges,
        "source": "本地日终市场快照",
        "note": "广度为日终抓取值；15:00 是交易时段归属，不是抓取服务器时刻。",
    }

    raw_stocks = snapshot.get("stocks") if isinstance(snapshot.get("stocks"), dict) else {}
    watchlist: list[dict[str, Any]] = []
    for configured in watchlist_items:
        name = str(configured.get("name") or "")
        source = raw_stocks.get(name)
        if not isinstance(source, dict):
            continue
        quote = source.get("quote") if isinstance(source.get("quote"), dict) else {}
        technical = source.get("technical") if isinstance(source.get("technical"), dict) else {}
        if str(technical.get("date") or "") != trade_date.isoformat():
            continue
        price = _float(quote.get("price"))
        technical_close = _float(technical.get("close"))
        if price is None or technical_close is None or abs(price - technical_close) > 0.011:
            continue
        previous = _previous_completed_close(technical.get("recent_closes"), trade_date)
        change = None if previous is None else price - previous
        public_quote = {"price": price}
        validation = _quote_validation(
            thscode=str(configured.get("thscode") or ""),
            is_stock=_is_a_share_stock(configured),
            public_quote=public_quote,
            public_stale=False,
            official_quote=None,
            provider=validation_provider,
        )
        watchlist.append(
            {
                "thscode": configured.get("thscode"),
                "symbol": configured.get("symbol"),
                "code": configured.get("code"),
                "name": name,
                "theme": configured.get("theme"),
                "price": price,
                "prev_close": previous,
                "change": _safe_round(change, 4),
                "change_pct": _float(quote.get("change_pct"))
                if _float(quote.get("change_pct")) is not None
                else _safe_round(None if change is None or not previous else change / previous * 100, 3),
                "open": None,
                "high": None,
                "low": None,
                "amount_yi": _float(quote.get("amount_yi")),
                "turnover_pct": _float(quote.get("turnover_pct")),
                "pe_ttm": _float(quote.get("pe_ttm")),
                "pb": _float(quote.get("pb")),
                "mcap_yi": _float(quote.get("mcap_yi")),
                "quote_time": quote_time,
                "is_stale": False,
                "quote_status": "eod_snapshot",
                "data_source": "本地已校验日终市场快照",
                "official_validation": validation,
            }
        )
    if len(indices) != len(INDEXES):
        errors.append(f"本地日终指数覆盖 {len(indices)}/{len(INDEXES)}")
    if len(watchlist) != len(watchlist_items):
        errors.append(f"本地日终观察名单覆盖 {len(watchlist)}/{len(watchlist_items)}")
    return {"indices": indices, "breadth": breadth, "global": []}, watchlist, errors


def _recover_quote_cache_from_local_snapshots() -> bool:
    """Recover display-grade closed-session quotes without any provider I/O."""

    if _quote_refresh_enabled():
        return False
    portfolio, configured_positions, config_mtime_ns, config_error = _portfolio_snapshot()
    if config_error:
        return False
    point = _latest_local_intraday_point(portfolio, configured_positions)
    if point is None:
        return False
    trade_date = date_type.fromisoformat(str(point.get("trade_date") or ""))
    point_rows = {
        str(row.get("thscode") or "").upper(): row
        for row in point.get("positions") or []
        if isinstance(row, dict)
    }
    validation_provider = {
        "status": "local_recovery_unverified",
        "stale": True,
        "source_stale": True,
        "refreshing": False,
        "last_error": "授权源交叉校验明细未持久化；仅恢复展示级公开行情",
        "source_timestamp": "",
        "quotes": {},
    }
    try:
        watchlist_config, watchlist_items = _load_watchlist_items()
    except Exception:
        watchlist_config, watchlist_items = {}, []
    markets, watchlist_rows, recovery_errors = _recovered_markets_and_watchlist(
        trade_date, watchlist_items, validation_provider
    )

    position_rows: list[dict[str, Any]] = []
    for configured in configured_positions:
        source = point_rows[str(configured.get("thscode") or "").upper()]
        price = float(source["price"])
        previous = float(source["last_close"])
        quantity = int(configured.get("quantity") or 0)
        change = price - previous
        public_quote = {"price": price}
        validation = _quote_validation(
            thscode=str(configured.get("thscode") or ""),
            is_stock=_is_a_share_stock(configured),
            public_quote=public_quote,
            public_stale=False,
            official_quote=None,
            provider=validation_provider,
        )
        position_rows.append(
            {
                "account_id": configured.get("account_id"),
                "account_type": configured.get("account_type"),
                "thscode": configured.get("thscode"),
                "symbol": configured.get("symbol"),
                "code": configured.get("code"),
                "name": configured.get("name"),
                "instrument_type": configured.get("instrument_type", "stock"),
                "industry": configured.get("industry", ""),
                "theme": configured.get("theme", ""),
                "quantity": quantity,
                "available": int(configured.get("available") or 0),
                "holding_as_of": portfolio.get("as_of"),
                "quantity_source": "账户快照",
                "price": price,
                "prev_close": previous,
                "change": _safe_round(change, 4),
                "change_pct": _safe_round(change / previous * 100 if previous else None, 3),
                "open": None,
                "high": None,
                "low": None,
                "amount_yi": None,
                "turnover_pct": None,
                "pe_ttm": None,
                "pb": None,
                "mcap_yi": None,
                "market_value": round(price * quantity, 2),
                "daily_pnl": round(change * quantity, 2),
                "quote_time": source.get("quote_at"),
                "is_stale": False,
                "quote_status": "closed_cache",
                "data_source": str(source.get("quote_source") or "腾讯财经"),
                "official_validation": validation,
            }
        )

    market_value = sum(float(row["market_value"]) for row in position_rows)
    total_cash = sum(float(account.get("cash") or 0) for account in portfolio.get("accounts") or [])
    total_debt = sum(float(account.get("debt") or 0) for account in portfolio.get("accounts") or [])
    net_equity = market_value + total_cash - total_debt
    for row in position_rows:
        row["weight_pct"] = round(float(row["market_value"]) / net_equity * 100, 3) if net_equity else None
        row["weight_pct_nav"] = row["weight_pct"]
        row["weight_pct_securities"] = round(float(row["market_value"]) / market_value * 100, 3) if market_value else None
    position_rows.sort(key=lambda row: float(row["market_value"]), reverse=True)

    accounts = []
    for account in portfolio.get("accounts") or []:
        account_rows = [row for row in position_rows if row.get("account_id") == account.get("account_id")]
        account_market_value = sum(float(row["market_value"]) for row in account_rows)
        cash = float(account.get("cash") or 0)
        debt = float(account.get("debt") or 0)
        accounts.append(
            {
                "account_id": account.get("account_id"),
                "account_type": account.get("account_type"),
                "market_value": round(account_market_value, 2),
                "cash": cash,
                "debt": debt,
                "net_equity": round(account_market_value + cash - debt, 2),
                "daily_pnl": round(sum(float(row["daily_pnl"]) for row in account_rows), 2),
                "holding_as_of": portfolio.get("as_of"),
                "values_are_estimates": True,
            }
        )
    quote_times = [
        parsed
        for row in position_rows
        for parsed in (_parse_quote_datetime(row.get("quote_time")),)
        if parsed is not None
    ]
    freshest = max(quote_times)
    quote_validation = _quote_validation_summary(
        position_rows, watchlist_rows, validation_provider
    )
    errors = [
        "行情由本机盘中哈希链账本恢复；授权源交叉校验状态未持久化，仅供展示。",
        *recovery_errors,
    ]
    payload = {
        "ok": True,
        "generated_at": str(point.get("observed_at") or freshest.isoformat(timespec="seconds")),
        "market_status": _market_status(freshest),
        "portfolio": {
            "portfolio_id": portfolio.get("portfolio_id"),
            "as_of": portfolio.get("as_of"),
            "account_snapshot_as_of": portfolio.get("as_of"),
            "holdings_state": "LOCAL_ACCOUNT_SNAPSHOT_NOT_BROKER_LIVE",
            "valuation_basis": "snapshot_quantities_x_latest_available_prices",
            "config_mtime_ns": config_mtime_ns,
            "position_count": len(position_rows),
            "quote_coverage_count": len(position_rows),
            "market_value": round(market_value, 2),
            "daily_pnl": round(sum(float(row["daily_pnl"]) for row in position_rows), 2),
            "daily_pnl_coverage_count": len(position_rows),
            "cash": round(total_cash, 2),
            "debt": round(total_debt, 2),
            "net_equity": round(net_equity, 2),
            "gross_exposure_pct": round(market_value / net_equity * 100, 2) if net_equity else None,
            "accounts": accounts,
        },
        "markets": markets,
        "positions": position_rows,
        "quote_validation": quote_validation,
        "account_snapshot": copy.deepcopy(point.get("account_snapshot") or {}),
        "watchlist": watchlist_rows,
        "watchlist_meta": {
            "as_of": watchlist_config.get("as_of"),
            "review_by": watchlist_config.get("review_by"),
            "quote_coverage_count": len(watchlist_rows),
            "item_count": len(watchlist_items),
        },
        "sources": [
            {"name": "本机盘中哈希链账本", "role": "最后可审计持仓报价"},
            {"name": "本地已校验日终市场快照", "role": "指数、市场宽度与观察名单收盘数据"},
            {"name": "持仓配置", "role": "数量、账户现金与负债", "as_of": portfolio.get("as_of")},
        ],
        "errors": list(dict.fromkeys(errors)),
        "recovery": {
            "mode": "closed_session_local_only",
            "ledger_trade_date": trade_date.isoformat(),
            "ledger_sequence": point.get("sequence"),
            "ledger_record_hash": point.get("record_hash"),
            "official_validation_restored": False,
            "network_io": False,
        },
    }
    try:
        _persist_quote_cache(payload)
    except Exception:
        return False
    return _load_persisted_quote_cache()


def _publish_quote_payload(payload: dict[str, Any]) -> None:
    """Publish a live payload even if its optional disk cache cannot be updated."""

    persistence_error = ""
    try:
        _persist_quote_cache(payload)
    except Exception as exc:
        persistence_error = f"行情缓存持久化失败：{type(exc).__name__}: {exc}"
    with QUOTE_LOCK:
        QUOTE_CACHE["payload"] = payload
        QUOTE_CACHE["monotonic"] = time.monotonic()
        QUOTE_CACHE["last_error"] = persistence_error


def _quote_worker() -> None:
    try:
        # Re-check in the worker so a request queued at the session boundary
        # cannot begin network I/O after the market has entered a closed phase.
        if not _quote_refresh_enabled():
            return
        with QUOTE_REFRESH_LOCK:
            # The lock can be held by a capture that began just before lunch or
            # close.  Re-evaluate only after our turn arrives, immediately
            # before the first provider/network call.
            if not _quote_refresh_enabled():
                return
            fresh = _build_quotes_payload()
        coverage = int((fresh.get("portfolio") or {}).get("quote_coverage_count") or 0)
        required = int((fresh.get("portfolio") or {}).get("position_count") or 0)
        fresh_config_mtime = int((fresh.get("portfolio") or {}).get("config_mtime_ns") or 0)
        _, _, current_config_mtime, _ = _portfolio_snapshot()
        if fresh_config_mtime != current_config_mtime:
            raise RuntimeError("持仓配置在行情刷新期间发生变化，放弃旧配置结果")
        with QUOTE_LOCK:
            cached = QUOTE_CACHE.get("payload") or {}
            cached_coverage = int((cached.get("portfolio") or {}).get("quote_coverage_count") or 0)
        if not fresh.get("ok") or coverage == 0:
            raise RuntimeError(f"持仓报价覆盖不足：{coverage}/{required}")
        if coverage < required and cached_coverage > coverage:
            raise RuntimeError(
                f"持仓报价覆盖降至 {coverage}/{required}，保留覆盖 {cached_coverage}/{required} 的缓存"
            )
        _record_intraday_pnl_snapshot(fresh)
        _, _, post_record_mtime, post_record_error = _portfolio_snapshot()
        if post_record_error or post_record_mtime != fresh_config_mtime:
            raise RuntimeError("持仓配置在盘中轨迹记录期间发生变化，放弃旧配置结果")
        _record_extra_intraday_snapshots(fresh)
        _record_portfolio_performance(fresh)
        _publish_quote_payload(fresh)
        auto_paper_module.run_all_strategies()
    except Exception as exc:
        with QUOTE_LOCK:
            QUOTE_CACHE["last_error"] = f"实时行情刷新失败：{type(exc).__name__}: {exc}"
    finally:
        with QUOTE_LOCK:
            QUOTE_CACHE["refreshing"] = False


def ensure_quote_refresh(force: bool = False) -> bool:
    # ``force`` means bypass the freshness TTL, never bypass the market-session
    # network gate.  This covers startup warmup, scheduler callbacks and every
    # HTTP/decision request that funnels through this function.
    if not _quote_refresh_enabled():
        return False
    with QUOTE_LOCK:
        cached = QUOTE_CACHE["payload"]
        age = time.monotonic() - float(QUOTE_CACHE["monotonic"])
        should_refresh = force or cached is None or age >= QUOTE_TTL_SECONDS
        if QUOTE_CACHE["refreshing"] or not should_refresh:
            return False
        QUOTE_CACHE["refreshing"] = True
    thread = threading.Thread(target=_quote_worker, name="dashboard-quotes", daemon=True)
    try:
        thread.start()
    except Exception as exc:
        with QUOTE_LOCK:
            QUOTE_CACHE["refreshing"] = False
            QUOTE_CACHE["last_error"] = f"行情刷新线程启动失败：{type(exc).__name__}: {exc}"
        return False
    return True


def _refresh_quotes_synchronously() -> bool:
    """Refresh once under the same version fence used by the background worker."""
    # Defence in depth for direct/forced callers.  ``get_quotes_payload`` also
    # checks this policy, but the synchronous primitive must be safe by itself.
    if not _quote_refresh_enabled():
        return False
    try:
        with QUOTE_REFRESH_LOCK:
            # A forced request may wait behind the asynchronous worker across a
            # session boundary.  Never let that wait turn into closed-phase I/O.
            if not _quote_refresh_enabled():
                return False
            fresh = _build_quotes_payload()
        coverage = int((fresh.get("portfolio") or {}).get("quote_coverage_count") or 0)
        required = int((fresh.get("portfolio") or {}).get("position_count") or 0)
        if not fresh.get("ok") or coverage == 0:
            raise RuntimeError(f"持仓报价覆盖不足：{coverage}/{required}")
        fresh_config_mtime = int((fresh.get("portfolio") or {}).get("config_mtime_ns") or 0)
        _, _, current_config_mtime, _ = _portfolio_snapshot()
        if fresh_config_mtime != current_config_mtime:
            raise RuntimeError("持仓配置在同步行情刷新期间发生变化")
        _record_intraday_pnl_snapshot(fresh)
        _, _, post_record_mtime, post_record_error = _portfolio_snapshot()
        if post_record_error or post_record_mtime != fresh_config_mtime:
            raise RuntimeError("持仓配置在盘中轨迹记录期间发生变化，放弃旧配置结果")
        _record_extra_intraday_snapshots(fresh)
        _record_portfolio_performance(fresh)
        _publish_quote_payload(fresh)
        auto_paper_module.run_all_strategies()
        return True
    except Exception as exc:
        with QUOTE_LOCK:
            QUOTE_CACHE["last_error"] = f"同步行情刷新失败：{type(exc).__name__}: {exc}"
        return False


def _backfill_portfolio_views_if_missing(payload: dict[str, Any]) -> None:
    """Synthesize multi-portfolio fields on payloads that predate them.

    Recovery payloads (intraday hash-chain ledger restore) and legacy caches
    only carry the main portfolio. Rebuild the missing附加组合视图 from the
    payload's own position prices so附加组合 tabs、组合列表与比较接口在恢复
    场景下也能展示，无需额外网络请求。
    """

    if not isinstance(payload, dict):
        return
    entries = _all_portfolio_entries()
    extras = [entry for entry in entries if entry["portfolio_id"] != MAIN_PORTFOLIO_ID]
    views = payload.get("portfolio_views")
    if not isinstance(views, dict):
        views = {}
        payload["portfolio_views"] = views
    if not payload.get("portfolios"):
        main_summary = payload.get("portfolio") or {}
        payload["portfolios"] = [
            {
                "portfolio_id": MAIN_PORTFOLIO_ID,
                "label": str(main_summary.get("label") or MAIN_PORTFOLIO_LABEL),
                "editable": False,
                "position_count": main_summary.get("position_count"),
                "market_value": main_summary.get("market_value"),
                "daily_pnl": main_summary.get("daily_pnl"),
                "net_equity": main_summary.get("net_equity"),
                "as_of": main_summary.get("as_of"),
            }
        ]
        for entry in extras:
            payload["portfolios"].append(
                {
                    "portfolio_id": entry["portfolio_id"],
                    "label": entry["label"],
                    "editable": True,
                    "position_count": len(entry["positions"]),
                    "as_of": entry["config"].get("as_of"),
                }
            )
    missing = [entry for entry in extras if entry["portfolio_id"] not in views]
    if not missing:
        return
    quotes: dict[str, dict[str, Any]] = {}
    for row in payload.get("positions") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        quotes[symbol] = {
            "source_name": row.get("name"),
            "price": row.get("price"),
            "prev_close": row.get("prev_close"),
            "change": row.get("change"),
            "change_pct": row.get("change_pct"),
            "quote_time": row.get("quote_time") or "",
            "quote_time_raw": row.get("quote_time") or "",
        }
    provider = {
        "status": "local_recovery_unverified",
        "stale": True,
        "source_stale": True,
        "refreshing": False,
        "last_error": "",
        "source_timestamp": "",
        "quotes": {},
    }
    for entry in missing:
        try:
            extra_rows, extra_summary = _build_portfolio_position_block(
                entry["config"],
                entry["positions"],
                quotes=quotes,
                official_quotes={},
                official_provider=provider,
                live_window=False,
                errors=[],
            )
        except Exception:
            continue
        views[entry["portfolio_id"]] = {
            "portfolio": {
                "portfolio_id": entry["portfolio_id"],
                "label": entry["label"],
                "account_snapshot_as_of": entry["config"].get("as_of"),
                "holdings_state": "USER_DEFINED_SNAPSHOT_NOT_BROKER_LIVE",
                "valuation_basis": "snapshot_quantities_x_latest_available_prices",
                **extra_summary,
            },
            "positions": extra_rows,
        }
    # 让 portfolios 摘要列表与回补后的视图数字保持一致（页内 Tab 徽标读取这里）
    summary_by_pid = {
        item.get("portfolio_id"): item
        for item in payload.get("portfolios") or []
        if isinstance(item, dict)
    }
    for entry in extras:
        view_summary = (views.get(entry["portfolio_id"]) or {}).get("portfolio") or {}
        if not view_summary:
            continue
        item = summary_by_pid.get(entry["portfolio_id"])
        if item is None:
            item = {
                "portfolio_id": entry["portfolio_id"],
                "label": entry["label"],
                "editable": True,
            }
            payload["portfolios"].append(item)
            summary_by_pid[entry["portfolio_id"]] = item
        for key in ("position_count", "market_value", "daily_pnl", "net_equity", "as_of"):
            if view_summary.get(key) is not None:
                item[key] = view_summary.get(key)


def get_quotes_payload(
    force: bool = False, portfolio_id: str = MAIN_PORTFOLIO_ID
) -> dict[str, Any]:
    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    if portfolio_id != MAIN_PORTFOLIO_ID and _resolve_portfolio_entry(portfolio_id) is None:
        raise PortfolioNotFound(f"组合不存在：{portfolio_id}")
    config_changed = _refresh_portfolio_config_if_changed()
    extras_changed = _refresh_extra_portfolios_if_changed()
    refresh_profile = _background_refresh_profile()
    quote_refresh_enabled = bool(
        refresh_profile.get(
            "quote_refresh_enabled",
            str(refresh_profile.get("phase") or "").lower() in {"auction", "open"},
        )
    )
    if force and quote_refresh_enabled:
        _refresh_quotes_synchronously()
    with PORTFOLIO_LOCK:
        current_config_mtime = PORTFOLIO_MTIME_NS
        current_extras_mtime = EXTRA_PORTFOLIOS_MTIME_NS
    with QUOTE_LOCK:
        cached_config_mtime = int(
            ((QUOTE_CACHE.get("payload") or {}).get("portfolio") or {}).get("config_mtime_ns") or 0
        )
        cached_extras_mtime_raw = (QUOTE_CACHE.get("payload") or {}).get("extras_config_mtime_ns")
        cached_extras_mtime = int(cached_extras_mtime_raw or 0)
        cache_uses_old_config = bool(
            QUOTE_CACHE.get("payload")
            and (
                cached_config_mtime != current_config_mtime
                # 旧格式缓存没有 extras 标记时不因附加组合误判过期；
                # 本进程新建的快照都带该字段，附加组合变更仍会触发重建。
                or (cached_extras_mtime_raw is not None and cached_extras_mtime != current_extras_mtime)
            )
        )
        if config_changed or extras_changed or cache_uses_old_config:
            QUOTE_CACHE["payload"] = None
            QUOTE_CACHE["monotonic"] = 0.0
            QUOTE_CACHE["last_error"] = "持仓配置已更新，正在刷新行情"
        cached = copy.deepcopy(QUOTE_CACHE["payload"])
        age = time.monotonic() - float(QUOTE_CACHE["monotonic"])
        last_error = str(QUOTE_CACHE["last_error"] or "")
        refreshing = bool(QUOTE_CACHE["refreshing"])
    if cached is not None:
        if age >= QUOTE_TTL_SECONDS and not refreshing:
            refreshing = ensure_quote_refresh()
        served = _cached_quotes_payload(cached, age, last_error, refreshing)
        _backfill_portfolio_views_if_missing(served)
        if portfolio_id != MAIN_PORTFOLIO_ID:
            return _scoped_quotes_view(served, portfolio_id)
        return _attach_position_advice(served)

    refresh_started = ensure_quote_refresh()
    error = last_error or (
        "实时行情正在首次加载"
        if quote_refresh_enabled
        else "当前为非交易时段且没有可用行情缓存；将在下一交易时段刷新"
    )
    configured_stock_codes = [
        str(item.get("thscode") or "")
        for item in _union_configured_positions()
        if _is_a_share_stock(item)
    ]
    provider = HITHINK_QUOTE_PROVIDER.snapshot(
        configured_stock_codes, str(refresh_profile.get("phase") or "closed")
    )
    cold_payload = {
        "ok": False,
        "generated_at": _iso_now(),
        "cache": {
            "age_seconds": None,
            "ttl_seconds": QUOTE_TTL_SECONDS,
            "max_stale_seconds": QUOTE_MAX_STALE_SECONDS,
            "source": "none",
            "fresh": False,
            "refreshing": refresh_started,
            "expired": False,
            "degraded": True,
            "errors": [error],
        },
        "market_status": _market_status(None),
        "portfolio_id": portfolio_id,
        "portfolio": {},
        "portfolios": [
            {
                "portfolio_id": item["portfolio_id"],
                "label": item["label"],
                "editable": item["editable"],
                "position_count": len(item["positions"]),
            }
            for item in _all_portfolio_entries()
        ],
        "markets": {"indices": [], "breadth": {}, "global": []},
        "positions": [],
        "watchlist": [],
        "quote_validation": {
            "status": "pending" if provider.get("refreshing") else "critical",
            "required_for_execution": True,
            "decision_verified": False,
            "positions": {
                "required_count": len(set(configured_stock_codes)),
                "passed_count": 0,
                "verified": False,
                "failed_codes": sorted(set(configured_stock_codes)),
                "states": {"pending": len(set(configured_stock_codes))},
            },
            "watchlist": {},
            "provider": {key: value for key, value in provider.items() if key != "quotes"},
        },
        "sources": [
            {"name": "腾讯财经", "role": "A股行情、指数、市场宽度"},
            {"name": "新浪财经", "role": "海外期货、外汇和亚太指数"},
        ],
        "errors": [error],
    }
    if portfolio_id != MAIN_PORTFOLIO_ID:
        return cold_payload
    return _attach_position_advice(cold_payload)


def _strip_html(value: Any) -> str:
    plain = re.sub(r"<[^>]+>", "", str(value or ""))
    return html.unescape(re.sub(r"\s+", " ", plain)).strip()


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def _eastmoney_circuit_snapshot() -> dict[str, Any]:
    """Return active per-host circuit state without exposing request details."""

    now_monotonic = time.monotonic()
    with EM_THROTTLE_LOCK:
        hosts = {
            host: {
                "status_code": int(state["status_code"]),
                "retry_after_seconds": round(
                    max(0.0, float(state["open_until_monotonic"]) - now_monotonic), 1
                ),
                "opened_at": str(state.get("opened_at") or ""),
            }
            for host, state in EM_CIRCUITS.items()
            if float(state.get("open_until_monotonic") or 0.0) > now_monotonic
        }
    return {"open": bool(hosts), "hosts": hosts}


def _eastmoney_cooldown_seconds(response: requests.Response) -> float:
    retry_after = 0.0
    try:
        retry_after = float(str(response.headers.get("Retry-After") or "").strip())
    except (AttributeError, TypeError, ValueError):
        pass
    # A short Retry-After is not enough after a WAF/IP throttle.  The skill's
    # operational guidance and observed incidents support a conservative 30 min.
    return max(EM_RATE_LIMIT_COOLDOWN_SECONDS, min(retry_after, 6 * 60 * 60.0))


def _serialized_eastmoney_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
) -> requests.Response:
    """Run an Eastmoney request inside the dashboard-wide serial throttle."""

    global EM_LAST_CALL
    hostname = (urlparse(url).hostname or "eastmoney.com").lower().rstrip(".")
    with EM_THROTTLE_LOCK:
        now_monotonic = time.monotonic()
        circuit = EM_CIRCUITS.get(hostname) or {}
        open_until = float(circuit.get("open_until_monotonic") or 0.0)
        if now_monotonic < open_until:
            status_code = int(circuit.get("status_code") or 429)
            raise EastmoneyCircuitOpen(
                f"Eastmoney {hostname} circuit open after HTTP {status_code}; "
                f"retry in {max(1, round(open_until - now_monotonic))} seconds"
            )
        wait = EM_MIN_INTERVAL_SECONDS - (now_monotonic - EM_LAST_CALL)
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.18))
        try:
            response = EM_SESSION.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
        finally:
            EM_LAST_CALL = time.monotonic()
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code in (403, 429):
            EM_CIRCUITS[hostname] = {
                "status_code": status_code,
                "open_until_monotonic": EM_LAST_CALL + _eastmoney_cooldown_seconds(response),
                "opened_at": _iso_now(),
            }
        elif isinstance(status_code, int) and 200 <= status_code < 400:
            EM_CIRCUITS.pop(hostname, None)
        return response


def _em_get(params: dict[str, Any]) -> requests.Response:
    response = _serialized_eastmoney_get(
        EASTMONEY_SEARCH,
        params=params,
        headers={"User-Agent": UA, "Referer": "https://so.eastmoney.com/"},
        timeout=15,
    )
    response.raise_for_status()
    return response


def _market_news_fetch(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> requests.Response:
    """Fetch market news while sharing Eastmoney's process-wide throttle.

    The throttle lock intentionally covers the HTTP request itself, not only the
    preceding wait.  This prevents the market feed and per-position news worker from
    issuing concurrent requests against separate Eastmoney subdomains.
    """

    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    if hostname == "eastmoney.com" or hostname.endswith(".eastmoney.com"):
        return _serialized_eastmoney_get(
            url,
            params=params,
            headers=headers,
            timeout=timeout,
        )
    if hostname == "news.google.com" or hostname.endswith(".google.com"):
        # Unreachable from this network most cycles.  Bound the connect cost so
        # one dead collector cannot stretch the whole feed cycle past its TTL.
        return NEWS_SESSION.get(
            url, params=params, headers=headers, timeout=(2.0, 5.0)
        )
    return NEWS_SESSION.get(url, params=params, headers=headers, timeout=timeout)


def _fetch_eastmoney_news(position: dict[str, Any], page_size: int = 8) -> list[dict[str, Any]]:
    callback = "jQuery_dashboard"
    inner = json.dumps(
        {
            "uid": "",
            "keyword": position["code"],
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": page_size,
                    "preTag": "",
                    "postTag": "",
                }
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = _em_get({"cb": callback, "param": inner})
    text = response.text
    if "(" not in text or ")" not in text:
        raise RuntimeError("Eastmoney returned malformed JSONP")
    payload = json.loads(text[text.index("(") + 1 : text.rindex(")")])
    articles = ((payload.get("result") or {}).get("cmsArticleWebOld") or [])
    rows = []
    for item in articles:
        rows.append(
            {
                "code": position["code"],
                "thscode": position["thscode"],
                "stock_name": position["name"],
                "title": _strip_html(item.get("title")),
                "summary": _strip_html(item.get("content"))[:260],
                "time": str(item.get("date") or ""),
                "source": str(item.get("mediaName") or "东方财富聚合"),
                "source_id": "eastmoney",
                "source_kind": "media",
                "url": _safe_url(item.get("url")),
            }
        )
    return rows


def _load_cninfo_org_map() -> dict[str, str]:
    global CNINFO_ORG_MAP
    if CNINFO_ORG_MAP is not None:
        return CNINFO_ORG_MAP
    response = NEWS_SESSION.get(CNINFO_ORG_LIST, headers={"User-Agent": UA}, timeout=15)
    response.raise_for_status()
    CNINFO_ORG_MAP = {
        str(item.get("code")): str(item.get("orgId"))
        for item in (response.json().get("stockList") or [])
        if item.get("code") and item.get("orgId")
    }
    return CNINFO_ORG_MAP


def _fetch_cninfo_announcements(position: dict[str, Any], page_size: int = 8) -> list[dict[str, Any]]:
    if position.get("instrument_type") != "stock" or position["thscode"].endswith(".BJ"):
        return []
    code = position["code"]
    org_id = _load_cninfo_org_map().get(code)
    if not org_id:
        prefix = "sh" if position["thscode"].endswith(".SH") else "sz"
        org_id = f"gs{prefix}0{code}"
    response = NEWS_SESSION.post(
        CNINFO_ANNOUNCEMENTS,
        data={
            "stock": f"{code},{org_id}",
            "tabName": "fulltext",
            "pageSize": str(page_size),
            "pageNum": "1",
            "column": "",
            "category": "",
            "plate": "",
            "seDate": "",
            "searchkey": "",
            "secid": "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        },
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        },
        timeout=15,
    )
    response.raise_for_status()
    rows = []
    for item in response.json().get("announcements") or []:
        timestamp = item.get("announcementTime")
        published = ""
        if isinstance(timestamp, (int, float)):
            published = datetime.fromtimestamp(timestamp / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        adjunct_url = str(item.get("adjunctUrl") or "").lstrip("/")
        rows.append(
            {
                "code": code,
                "thscode": position["thscode"],
                "stock_name": position["name"],
                "title": _strip_html(item.get("announcementTitle")),
                "summary": str(item.get("announcementTypeName") or "公司公告"),
                "time": published,
                "source": "巨潮资讯",
                "source_id": "cninfo",
                "source_kind": "official",
                "url": _safe_url(
                    f"https://static.cninfo.com.cn/{adjunct_url}" if adjunct_url else ""
                ),
            }
        )
    return rows


def _fetch_sina_linked_news(positions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    response = NEWS_SESSION.get(
        SINA_FEED,
        params={"zhibo_id": 152, "page_size": 200, "dire": "f"},
        headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"},
        timeout=15,
    )
    response.raise_for_status()
    feed = ((((response.json().get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or [])
    by_symbol: dict[str, dict[str, Any]] = {}
    if positions is None:
        _, positions, _, _ = _portfolio_snapshot()
    for position in positions:
        by_symbol[position["symbol"].lower()] = position
        by_symbol[position["code"]] = position
    rows: list[dict[str, Any]] = []
    for item in feed:
        try:
            extension = item.get("ext") or "{}"
            extension = json.loads(extension) if isinstance(extension, str) else extension
        except json.JSONDecodeError:
            extension = {}
        matched: dict[str, dict[str, Any]] = {}
        for linked in extension.get("stocks") or []:
            symbol = str(linked.get("symbol") or "").lower()
            position = by_symbol.get(symbol) or by_symbol.get(re.sub(r"^(sh|sz|bj)", "", symbol))
            if position:
                matched[position["code"]] = position
        if not matched:
            continue
        body = _strip_html(item.get("rich_text"))
        bracket = re.match(r"^【([^】]+)】", body)
        title = bracket.group(1) if bracket else re.split(r"[。！？]", body, maxsplit=1)[0][:90]
        tags = [str(tag.get("name")) for tag in item.get("tag") or [] if tag.get("name")]
        for position in matched.values():
            rows.append(
                {
                    "code": position["code"],
                    "thscode": position["thscode"],
                    "stock_name": position["name"],
                    "title": title,
                    "summary": body[:300],
                    "time": str(item.get("create_time") or ""),
                    "source": "新浪财经7×24",
                    "source_id": "sina",
                    "source_kind": "wire",
                    "url": _safe_url(item.get("docurl")),
                    "upstream_tags": tags,
                }
            )
    return rows


def _fetch_eastmoney_announcements(
    position: dict[str, Any], page_size: int = 8
) -> list[dict[str, Any]]:
    """东财公告（巨潮不可用时的备援源，2026-10-20 实测可用）。

    与巨潮同为法定披露口径，但按 stock_list=六位代码 过滤，不依赖巨潮的
    orgId 映射表——巨潮 org 表拉取失败时整条公告链路会降级，本源可独立补位。
    走东财公共节流器（1.15s 间隔 + 按 host 熔断），避免与行情刷新抢配额。
    """

    if position.get("instrument_type") != "stock":
        return []
    code = position["code"]
    response = _serialized_eastmoney_get(
        EASTMONEY_ANNOUNCEMENTS,
        params={
            "page_size": str(page_size),
            "page_index": "1",
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
            "f_node": "0",
            "s_node": "0",
        },
        headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
        timeout=15,
    )
    response.raise_for_status()
    rows = []
    for item in ((response.json().get("data") or {}).get("list") or []):
        title = _strip_html(item.get("title"))
        if not title:
            continue
        # display_time 是真实披露时刻（形如 "2026-10-16 18:19:16:321"），
        # notice_date 是归整到零点的公告日（盘后披露会落到次日），取前者。
        published = str(item.get("display_time") or item.get("notice_date") or "")
        published = published.split(".")[0].strip()
        columns = item.get("columns") or []
        summary = "公司公告"
        if columns and isinstance(columns[0], dict):
            summary = str(columns[0].get("column_name") or summary)
        art_code = str(item.get("art_code") or "")
        rows.append(
            {
                "code": code,
                "thscode": position["thscode"],
                "stock_name": position["name"],
                "title": title,
                "summary": summary,
                "time": published,
                "source": "东方财富公告",
                "source_id": "eastmoney_ann",
                "source_kind": "official",
                "url": _safe_url(
                    f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
                    if art_code
                    else ""
                ),
            }
        )
    return rows


def _score_news(item: dict[str, Any]) -> dict[str, Any]:
    searchable = f"{item.get('title', '')} {item.get('summary', '')}"
    tags = []
    score = 1 if item.get("source_kind") == "official" else 0
    for label, terms in MATERIAL_TERMS.items():
        if any(term in searchable for term in terms):
            tags.append(label)
            score += 3 if label in ("业绩", "监管", "交易") else 2
    tags.extend(item.get("upstream_tags") or [])
    if item.get("stock_name") and str(item["stock_name"]) in searchable:
        score += 1
    try:
        published = datetime.strptime(str(item.get("time") or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
        days = (_now() - published).total_seconds() / 86400
        if days <= 3:
            score += 1
        elif days <= 10:
            score += 1
    except ValueError:
        pass
    importance = "high" if score >= 6 else "medium" if score >= 4 else "low"
    result = dict(item)
    result["importance"] = importance
    result["importance_score"] = score
    result["tags"] = list(dict.fromkeys(tag for tag in tags if tag))[:5]
    identity = f"{item.get('code')}|{item.get('url')}|{item.get('title')}"
    result["id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    result.pop("upstream_tags", None)
    return result


def _deduplicate_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in items:
        if not raw.get("title"):
            continue
        item = _score_news(raw)
        normalized = re.sub(r"[^\w\u4e00-\u9fff]", "", str(item["title"])).lower()
        key = (str(item["code"]), normalized)
        current = best.get(key)
        if current is None or (
            int(item["importance_score"]), item.get("source") == "巨潮资讯", item.get("time", "")
        ) > (
            int(current["importance_score"]), current.get("source") == "巨潮资讯", current.get("time", "")
        ):
            best[key] = item
    # The same event is often repeated as a media story and a formal filing.
    # Keep the strongest item for each stock/date/material-event combination.
    event_best: dict[tuple[str, str, str], dict[str, Any]] = {}
    untagged: list[dict[str, Any]] = []
    for item in best.values():
        event_tag = next((tag for tag in item.get("tags", []) if tag in MATERIAL_TERMS), "")
        event_date = str(item.get("time") or "")[:10]
        if not event_tag or not event_date:
            untagged.append(item)
            continue
        event_key = (str(item["code"]), event_date, event_tag)
        current = event_best.get(event_key)
        if current is None or (
            int(item["importance_score"]), item.get("source") == "巨潮资讯", item.get("time", "")
        ) > (
            int(current["importance_score"]), current.get("source") == "巨潮资讯", current.get("time", "")
        ):
            event_best[event_key] = item

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in [*event_best.values(), *untagged]:
        grouped.setdefault(str(item["code"]), []).append(item)
    selected: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        rows.sort(key=lambda row: (int(row["importance_score"]), row.get("time", "")), reverse=True)
        material = [row for row in rows if row["importance"] != "low"]
        selected.extend((material or rows[:2])[:4])
    selected.sort(key=lambda row: row.get("time", ""), reverse=True)
    return selected


def _load_persisted_news() -> None:
    if not NEWS_CACHE_PATH.exists():
        return
    try:
        payload = _read_json(NEWS_CACHE_PATH)
        generated = datetime.fromisoformat(str(payload.get("generated_at") or ""))
        age = max(0.0, (_now() - generated.astimezone(CN_TZ)).total_seconds())
    except Exception:
        return
    with NEWS_LOCK:
        NEWS_STATE["payload"] = payload
        NEWS_STATE["monotonic"] = time.monotonic() - age


def _news_source_kind(item: dict[str, Any]) -> str:
    kind = str(item.get("source_kind") or "")
    if kind:
        return kind
    source_id = str(item.get("source_id") or "")
    if source_id == "cninfo":
        return "official"
    if source_id == "sina":
        return "wire"
    if source_id == "eastmoney":
        return "media"
    source = str(item.get("source") or "")
    if source == "巨潮资讯":
        return "official"
    if "新浪" in source:
        return "wire"
    return "media"


def _refresh_news() -> None:
    with NEWS_REFRESH_LOCK:
        portfolio_config, _main_positions, news_config_mtime, _ = _portfolio_snapshot()
        # Fetch once per ticker across every portfolio so that a stock held by
        # several portfolios shares a single news/announcement fetch.
        configured_positions = _union_configured_positions()
        configured_codes = {str(item.get("code") or "") for item in configured_positions}
        with NEWS_LOCK:
            NEWS_STATE["refreshing"] = True
            previous = copy.deepcopy(NEWS_STATE.get("payload") or {})
        old_items = previous.get("items") or []
        old_by_code_kind: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in old_items:
            kind = _news_source_kind(item)
            restored = {**item, "source_kind": kind}
            old_by_code_kind.setdefault((str(item.get("code") or ""), kind), []).append(restored)

        source_status: dict[str, Any] = {
            "eastmoney": {"ok": True, "count": 0, "errors": []},
            "eastmoney_ann": {"ok": True, "count": 0, "errors": []},
            "cninfo": {"ok": True, "count": 0, "errors": []},
            "sina": {"ok": True, "count": 0, "errors": []},
        }
        gathered: list[dict[str, Any]] = []
        effective_updates = 0
        attempt_at = _iso_now()
        try:
            sina_rows = _fetch_sina_linked_news(configured_positions)
            source_status["sina"]["count"] = len(sina_rows)
            if sina_rows:
                gathered.extend(sina_rows)
                effective_updates += 1
            else:
                for (old_code, kind), rows in old_by_code_kind.items():
                    if old_code in configured_codes and kind == "wire":
                        gathered.extend(rows)
        except Exception as exc:
            source_status["sina"].update(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
            for (old_code, kind), rows in old_by_code_kind.items():
                if old_code in configured_codes and kind == "wire":
                    gathered.extend(rows)

        cninfo_ready = True
        try:
            _load_cninfo_org_map()
        except Exception as exc:
            cninfo_ready = False
            source_status["cninfo"].update(
                ok=False, errors=[f"org-map:{type(exc).__name__}"]
            )

        for position in configured_positions:
            code = position["code"]
            try:
                rows = _fetch_eastmoney_news(position)
                source_status["eastmoney"]["count"] += len(rows)
                if rows:
                    gathered.extend(rows)
                    effective_updates += 1
                else:
                    gathered.extend(old_by_code_kind.get((code, "media"), []))
            except Exception as exc:
                source_status["eastmoney"]["ok"] = False
                source_status["eastmoney"]["errors"].append(f"{code}:{type(exc).__name__}")
                gathered.extend(old_by_code_kind.get((code, "media"), []))

            official_rows: list[dict[str, Any]] = []
            if cninfo_ready:
                try:
                    official_rows = _fetch_cninfo_announcements(position)
                    source_status["cninfo"]["count"] += len(official_rows)
                except Exception as exc:
                    source_status["cninfo"]["ok"] = False
                    source_status["cninfo"]["errors"].append(f"{code}:{type(exc).__name__}")
            if not official_rows:
                # 巨潮无结果或整体不可用 → 东财公告备援（不依赖 orgId 映射表）。
                try:
                    official_rows = _fetch_eastmoney_announcements(position)
                    source_status["eastmoney_ann"]["count"] += len(official_rows)
                except Exception as exc:
                    source_status["eastmoney_ann"]["ok"] = False
                    source_status["eastmoney_ann"]["errors"].append(
                        f"{code}:{type(exc).__name__}"
                    )
            if official_rows:
                gathered.extend(official_rows)
                effective_updates += 1
            else:
                gathered.extend(old_by_code_kind.get((code, "official"), []))

        _, _, current_config_mtime, _ = _portfolio_snapshot()
        _, current_extras_mtime, extras_error = _extra_portfolios_snapshot()
        if current_config_mtime != news_config_mtime:
            raise RuntimeError("持仓配置在新闻刷新期间发生变化，放弃旧配置结果")
        if extras_error:
            raise RuntimeError("附加组合配置在新闻刷新期间不可用，放弃旧配置结果")

        if effective_updates == 0 and previous:
            failed_payload = copy.deepcopy(previous)
            failed_payload["source_status"] = source_status
            failed_payload["last_attempt_at"] = attempt_at
            failed_payload.setdefault("last_success_at", previous.get("generated_at", ""))
            with NEWS_LOCK:
                NEWS_STATE["payload"] = failed_payload
                NEWS_STATE["refreshing"] = False
                source_failed = any(not bool(status.get("ok")) for status in source_status.values())
                NEWS_STATE["last_error"] = (
                    "所有新闻源刷新失败，保留上次成功数据"
                    if source_failed
                    else "新闻源未返回新数据，保留上次成功数据"
                )
            return

        items = _deduplicate_news(gathered)
        payload = {
            "ok": bool(items),
            "generated_at": attempt_at,
            "last_attempt_at": attempt_at,
            "last_success_at": attempt_at,
            "items": items,
            "source_status": source_status,
            "position_count": len(configured_positions),
            "portfolio_id": portfolio_config.get("portfolio_id"),
            "portfolio_config_mtime_ns": news_config_mtime,
            "note": "重要性由公告来源、关键词和时效规则排序，不代表利好或利空判断。",
        }
        try:
            _atomic_write_json(NEWS_CACHE_PATH, payload)
        except Exception as exc:
            source_status.setdefault("cache", {}).update(ok=False, error=f"{type(exc).__name__}")
        with NEWS_LOCK:
            NEWS_STATE["payload"] = payload
            NEWS_STATE["monotonic"] = time.monotonic()
            NEWS_STATE["refreshing"] = False
            NEWS_STATE["last_error"] = ""


def _news_worker() -> None:
    try:
        _refresh_news()
    except Exception as exc:
        with NEWS_LOCK:
            NEWS_STATE["refreshing"] = False
            NEWS_STATE["last_error"] = f"{type(exc).__name__}: {exc}"


def ensure_news_refresh(force: bool = False) -> bool:
    ttl_seconds = _position_news_ttl_seconds()
    with NEWS_LOCK:
        age = time.monotonic() - float(NEWS_STATE["monotonic"])
        should_refresh = force or NEWS_STATE["payload"] is None or age >= ttl_seconds
        if NEWS_STATE["refreshing"] or not should_refresh:
            return False
        NEWS_STATE["refreshing"] = True
    thread = threading.Thread(target=_news_worker, name="dashboard-news", daemon=True)
    try:
        thread.start()
    except Exception as exc:
        with NEWS_LOCK:
            NEWS_STATE["refreshing"] = False
            NEWS_STATE["last_error"] = f"持仓新闻线程启动失败：{type(exc).__name__}: {exc}"
        return False
    return True


def get_news_payload(
    code: str = "",
    force: bool = False,
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    with NEWS_LOCK:
        needs_disk_warmup = NEWS_STATE.get("payload") is None
    if needs_disk_warmup:
        _load_persisted_news()
    ensure_news_refresh(force=force)
    portfolio_codes: set[str] | None = None
    portfolio_label = ""
    if portfolio_id and portfolio_id != MAIN_PORTFOLIO_ID:
        entry = _resolve_portfolio_entry(portfolio_id)
        if entry is None:
            raise PortfolioNotFound(f"组合不存在：{portfolio_id}")
        portfolio_codes = {str(p.get("code") or "") for p in entry["positions"]}
        portfolio_label = entry["label"]
    with NEWS_LOCK:
        base = copy.deepcopy(NEWS_STATE.get("payload") or {})
        refreshing = bool(NEWS_STATE["refreshing"])
        age = time.monotonic() - float(NEWS_STATE["monotonic"]) if NEWS_STATE["payload"] else None
        last_error = str(NEWS_STATE["last_error"] or "")
    ttl_seconds = _position_news_ttl_seconds()
    items = base.get("items") or []
    if portfolio_codes is not None:
        items = [item for item in items if str(item.get("code") or "") in portfolio_codes]
    if code:
        items = [item for item in items if str(item.get("code")) == code]
    return {
        "ok": bool(base.get("ok")) or refreshing,
        "generated_at": base.get("generated_at", ""),
        "last_attempt_at": base.get("last_attempt_at", base.get("generated_at", "")),
        "last_success_at": base.get("last_success_at", base.get("generated_at", "")),
        "portfolio_id": portfolio_id or MAIN_PORTFOLIO_ID,
        "portfolio_label": portfolio_label,
        "source_portfolio_id": base.get("portfolio_id", ""),
        "portfolio_config_mtime_ns": base.get("portfolio_config_mtime_ns"),
        "position_count": base.get("position_count"),
        "cache_age_seconds": None if age is None else round(max(0.0, age), 1),
        "ttl_seconds": ttl_seconds,
        "stale": age is None or age >= ttl_seconds,
        "refreshing": refreshing,
        "source_status": base.get("source_status", {}),
        "items": items,
        "note": base.get("note", "新闻正在首次加载。"),
        "errors": [last_error] if last_error else [],
    }


def _market_news_content_fingerprint(items: list[dict[str, Any]]) -> str:
    canonical = sorted(
        (copy.deepcopy(item) for item in items),
        key=lambda item: (
            str(item.get("id") or ""),
            str(item.get("published_at") or ""),
            str(item.get("title") or ""),
        ),
    )
    evidence = {
        "schema_version": str(market_news_module.SCHEMA_VERSION),
        "items": canonical,
    }
    return hashlib.sha256(
        json.dumps(evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _market_news_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _market_news_latest_event_at(items: list[dict[str, Any]], preferred: Any = "") -> str:
    """Return the collector timestamp when valid, otherwise the latest item time."""

    preferred_time = _market_news_datetime(preferred)
    if preferred_time is not None:
        return preferred_time.isoformat(timespec="seconds")
    candidates = [
        parsed
        for item in items
        if (parsed := _market_news_datetime(item.get("published_at"))) is not None
    ]
    return max(candidates).isoformat(timespec="seconds") if candidates else ""


def _market_news_errors(source_status: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for source_id, raw in source_status.items():
        status = raw if isinstance(raw, dict) else {}
        if status.get("ok") is False:
            detail = str(status.get("error") or "source unavailable").strip()
            errors.append(f"{source_id}: {detail}"[:400])
    return errors


def _market_news_retry_delay(errors: list[str]) -> float:
    combined = " ".join(str(error) for error in errors)
    if re.search(r"(?<!\d)(?:403|429)(?!\d)", combined):
        return MARKET_NEWS_RATE_LIMIT_RETRY_SECONDS
    return MARKET_NEWS_FAILURE_RETRY_SECONDS


def _empty_market_news_payload(error: str = "", *, refreshing: bool = False) -> dict[str, Any]:
    return {
        "schema_version": str(market_news_module.SCHEMA_VERSION),
        "ok": False,
        "degraded": True,
        "status": "loading" if refreshing else "unavailable",
        "generated_at": "",
        "last_attempt_at": "",
        "last_success_at": "",
        "latest_event_at": "",
        "content_fingerprint": "",
        "source_status": {},
        "source_count": 0,
        "item_count": 0,
        "items": [],
        "cache_age_seconds": None,
        "ttl_seconds": MARKET_NEWS_TTL_SECONDS,
        "retry_after_seconds": 0.0,
        "stale": True,
        "refreshing": refreshing,
        "errors": [error] if error else [],
    }


def _load_persisted_market_news() -> bool:
    """Warm the market-news cache only from a valid last-success envelope."""

    if not MARKET_NEWS_CACHE_PATH.exists():
        return False
    try:
        payload = _read_json(MARKET_NEWS_CACHE_PATH)
        if payload.get("schema_version") != str(market_news_module.SCHEMA_VERSION):
            raise ValueError("market-news cache schema mismatch")
        if payload.get("ok") is not True or payload.get("status") not in {"healthy", "degraded"}:
            raise ValueError("market-news cache is not a successful snapshot")
        items = payload.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("market-news cache items must be objects")
        source_status = payload.get("source_status")
        if not isinstance(source_status, dict):
            raise ValueError("market-news cache source_status must be an object")
        expected_fingerprint = _market_news_content_fingerprint(items)
        if payload.get("content_fingerprint") != expected_fingerprint:
            raise ValueError("market-news cache fingerprint mismatch")
        payload["latest_event_at"] = _market_news_latest_event_at(
            items, payload.get("latest_event_at")
        )
        succeeded = _market_news_datetime(payload.get("last_success_at"))
        if succeeded is None:
            raise ValueError("market-news cache last_success_at is invalid")
        age = (_now() - succeeded).total_seconds()
        if age < -300:
            raise ValueError("market-news cache last_success_at is in the future")
        age = max(0.0, age)
    except Exception:
        return False

    with MARKET_NEWS_LOCK:
        if MARKET_NEWS_STATE.get("payload") is not None:
            return False
        MARKET_NEWS_STATE["payload"] = copy.deepcopy(payload)
        MARKET_NEWS_STATE["monotonic"] = time.monotonic() - age
        MARKET_NEWS_STATE["last_attempt_monotonic"] = 0.0
        MARKET_NEWS_STATE["next_retry_monotonic"] = 0.0
        MARKET_NEWS_STATE["last_error"] = ""
    return True


def _publish_market_news_failure(
    previous: dict[str, Any],
    *,
    attempt_at: str,
    source_status: dict[str, Any],
    errors: list[str],
) -> None:
    error_list = list(dict.fromkeys(error for error in errors if error))
    if not error_list:
        error_list = ["all market-news sources unavailable"]
    retry_delay = _market_news_retry_delay(error_list)
    failed = copy.deepcopy(previous) if previous else _empty_market_news_payload()
    failed.update(
        {
            "schema_version": str(market_news_module.SCHEMA_VERSION),
            "ok": False,
            "degraded": True,
            "status": "stale",
            "last_attempt_at": attempt_at,
            "source_status": copy.deepcopy(source_status),
            "source_count": 0,
            "item_count": len(failed.get("items") or []),
            "stale": True,
            "refreshing": False,
            "errors": error_list,
        }
    )
    with MARKET_NEWS_LOCK:
        MARKET_NEWS_STATE["payload"] = failed
        # Deliberately preserve monotonic: failed attempts never make old data fresh.
        MARKET_NEWS_STATE["next_retry_monotonic"] = time.monotonic() + retry_delay
        MARKET_NEWS_STATE["refreshing"] = False
        MARKET_NEWS_STATE["last_error"] = "; ".join(error_list)[:800]


def _refresh_market_news() -> None:
    with MARKET_NEWS_REFRESH_LOCK:
        attempt_at = _iso_now()
        attempt_monotonic = time.monotonic()
        with MARKET_NEWS_LOCK:
            MARKET_NEWS_STATE["refreshing"] = True
            MARKET_NEWS_STATE["last_attempt_monotonic"] = attempt_monotonic
            previous = copy.deepcopy(MARKET_NEWS_STATE.get("payload") or {})

        try:
            fetched = market_news_module.fetch_market_news(fetch=_market_news_fetch)
            if not isinstance(fetched, dict):
                raise TypeError("market-news collector must return an object")
            source_status = fetched.get("source_status")
            source_status = copy.deepcopy(source_status) if isinstance(source_status, dict) else {}
            source_errors = _market_news_errors(source_status)
            if fetched.get("schema_version") != str(market_news_module.SCHEMA_VERSION):
                raise ValueError("market-news collector schema mismatch")
            items = fetched.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("market-news collector items must be objects")
            if fetched.get("ok") is not True:
                _publish_market_news_failure(
                    previous,
                    attempt_at=attempt_at,
                    source_status=source_status,
                    errors=source_errors or ["all market-news sources unavailable"],
                )
                return
            generated_at = str(fetched.get("generated_at") or "")
            generated = _market_news_datetime(generated_at)
            if generated is None or generated > _now() + timedelta(minutes=5):
                raise ValueError("market-news collector generated_at is invalid")

            status = str(fetched.get("status") or "").lower()
            degraded = bool(fetched.get("degraded")) or status != "healthy" or bool(source_errors)
            payload = {
                "schema_version": str(market_news_module.SCHEMA_VERSION),
                "ok": True,
                "degraded": degraded,
                "status": "degraded" if degraded else "healthy",
                "generated_at": generated_at,
                "last_attempt_at": attempt_at,
                "last_success_at": generated_at,
                "latest_event_at": _market_news_latest_event_at(
                    items, fetched.get("latest_event_at")
                ),
                "content_fingerprint": _market_news_content_fingerprint(items),
                "source_status": source_status,
                "source_count": int(fetched.get("source_count") or 0),
                "item_count": len(items),
                "items": copy.deepcopy(items),
                "stale": False,
                "refreshing": False,
                "errors": source_errors,
            }
            try:
                _atomic_write_json(MARKET_NEWS_CACHE_PATH, payload)
            except Exception as exc:
                cache_error = f"cache: {type(exc).__name__}"
                payload["degraded"] = True
                payload["status"] = "degraded"
                payload["errors"] = list(dict.fromkeys([*payload["errors"], cache_error]))

            with MARKET_NEWS_LOCK:
                MARKET_NEWS_STATE["payload"] = payload
                MARKET_NEWS_STATE["monotonic"] = time.monotonic()
                MARKET_NEWS_STATE["next_retry_monotonic"] = 0.0
                MARKET_NEWS_STATE["refreshing"] = False
                MARKET_NEWS_STATE["last_error"] = "; ".join(payload["errors"])[:800]
        except Exception as exc:
            source_status = (
                copy.deepcopy(fetched.get("source_status") or {})
                if "fetched" in locals() and isinstance(fetched, dict)
                else {}
            )
            _publish_market_news_failure(
                previous,
                attempt_at=attempt_at,
                source_status=source_status,
                errors=[f"{type(exc).__name__}: {exc}"[:500]],
            )


def _market_news_worker() -> None:
    _refresh_market_news()


def ensure_market_news_refresh(force: bool = False) -> bool:
    with MARKET_NEWS_LOCK:
        now = time.monotonic()
        if now < float(MARKET_NEWS_STATE.get("next_retry_monotonic") or 0.0):
            return False
        payload = MARKET_NEWS_STATE.get("payload")
        age = now - float(MARKET_NEWS_STATE.get("monotonic") or 0.0)
        should_refresh = (
            force
            or payload is None
            or age >= MARKET_NEWS_TTL_SECONDS - MARKET_NEWS_REFRESH_MARGIN_SECONDS
            or not payload.get("ok")
        )
        if MARKET_NEWS_STATE["refreshing"] or not should_refresh:
            return False
        MARKET_NEWS_STATE["refreshing"] = True
    thread = threading.Thread(target=_market_news_worker, name="dashboard-market-news", daemon=True)
    try:
        thread.start()
    except Exception as exc:
        with MARKET_NEWS_LOCK:
            MARKET_NEWS_STATE["refreshing"] = False
            MARKET_NEWS_STATE["last_error"] = f"市场快讯线程启动失败：{type(exc).__name__}: {exc}"
        return False
    return True


def get_market_news_payload(force: bool = False) -> dict[str, Any]:
    with MARKET_NEWS_LOCK:
        needs_disk_warmup = MARKET_NEWS_STATE.get("payload") is None
    if needs_disk_warmup:
        _load_persisted_market_news()
    ensure_market_news_refresh(force=force)
    with MARKET_NEWS_LOCK:
        base = copy.deepcopy(MARKET_NEWS_STATE.get("payload") or {})
        refreshing = bool(MARKET_NEWS_STATE["refreshing"])
        last_error = str(MARKET_NEWS_STATE.get("last_error") or "")
        retry_after = max(
            0.0,
            float(MARKET_NEWS_STATE.get("next_retry_monotonic") or 0.0) - time.monotonic(),
        )
        has_success = bool(base.get("last_success_at"))
        age = (
            time.monotonic() - float(MARKET_NEWS_STATE.get("monotonic") or 0.0)
            if has_success
            else None
        )
    if not base:
        result = _empty_market_news_payload(last_error, refreshing=refreshing)
        result["retry_after_seconds"] = round(retry_after, 1)
        return result

    stale = (
        age is None
        or age >= MARKET_NEWS_TTL_SECONDS + MARKET_NEWS_STALE_GRACE_SECONDS
        or base.get("status") == "stale"
    )
    errors = list(base.get("errors") or [])
    if last_error:
        errors.append(last_error)
    result = copy.deepcopy(base)
    result.update(
        {
            "ok": bool(base.get("ok")) and not stale,
            "degraded": bool(base.get("degraded")) or stale or bool(errors),
            "status": "stale" if stale else str(base.get("status") or "degraded"),
            "cache_age_seconds": None if age is None else round(max(0.0, age), 1),
            "ttl_seconds": MARKET_NEWS_TTL_SECONDS,
            "retry_after_seconds": round(retry_after, 1),
            "stale": stale,
            "refreshing": refreshing,
            "item_count": len(base.get("items") or []),
            "errors": list(dict.fromkeys(error for error in errors if error)),
        }
    )
    return result


_ACTION_CURRENCY_RE = re.compile(
    r"(?:人民币\s*)?(?:约\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:亿元|万元|元|CNY)",
    re.IGNORECASE,
)
_ACTION_SHARE_RE = re.compile(
    r"(?:约\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:万股|股|手)",
    re.IGNORECASE,
)
_ACTION_TARGET_WEIGHT_RE = re.compile(
    r"(?:目标(?:权重|仓位)?|调(?:整)?至|减仓至|加仓至)\s*[:：]?\s*[+-]?\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)


def _account_snapshot_current(risk_payload: dict[str, Any]) -> bool:
    quality = (
        risk_payload.get("data_quality")
        if isinstance(risk_payload.get("data_quality"), dict)
        else {}
    )
    account = (
        quality.get("account_snapshot")
        if isinstance(quality.get("account_snapshot"), dict)
        else {}
    )
    return bool(
        account.get("current_verified") is True
        and account.get("verified") is True
        and account.get("stale") is False
    )


def _without_action_sizing(value: Any) -> Any:
    """Remove executable sizing semantics while retaining non-monetary risk direction."""
    if isinstance(value, str):
        value = _ACTION_CURRENCY_RE.sub("[金额待账户核验]", value)
        value = _ACTION_SHARE_RE.sub("[股数待账户核验]", value)
        return _ACTION_TARGET_WEIGHT_RE.sub("[目标权重待账户核验]", value)
    if isinstance(value, list):
        return [_without_action_sizing(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if any(
            token in normalized
            for token in (
                "target",
                "adjustment",
                "notional",
                "quantity",
                "shares",
                "amount",
                "proceeds",
                "weight",
            )
        ):
            continue
        cleaned[key] = _without_action_sizing(item)
    return cleaned


def _fail_closed_risk_payload(
    payload: dict[str, Any],
    *,
    force: bool = False,
    reason: str = "账户快照未核验",
) -> dict[str, Any]:
    """Return an unsized risk view whenever an execution-critical gate is unverified."""
    if _account_snapshot_current(payload) and not force:
        return payload
    result = copy.deepcopy(payload)
    result["new_buy_allowed"] = False
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    for key in list(metrics):
        normalized = str(key).lower()
        if any(
            token in normalized
            for token in ("target", "adjustment", "notional", "proceeds", "order", "suggested", "recommended", "risk_reduction_required")
        ):
            metrics.pop(key, None)
    for key in ("targets", "adjustments", "execution", "orders", "order_draft", "trade_plan"):
        result.pop(key, None)
    actions: list[Any] = []
    for raw in result.get("actions") or []:
        action = _without_action_sizing(raw)
        if isinstance(action, dict) and str(action.get("kind") or "").upper() == "REDUCE_EXPOSURE":
            action["detail"] = (
                f"{reason}，不能计算精确减仓金额、目标权重或买卖股数；"
                "仅保留降低敞口、补充现金缓冲和降低负债的风险方向。"
            )
        actions.append(action)
    result["actions"] = actions
    return result


def _fail_closed_rebalance_payload(
    payload: dict[str, Any],
    *,
    reason_code: str = "ACCOUNT_SNAPSHOT_NOT_CURRENT",
    reason_message: str = "账户持仓、可用数量、现金和负债尚未核验为当前交易日状态",
) -> dict[str, Any]:
    """Reduce any untrusted plan to identity-only risk-review priority."""
    source = payload if isinstance(payload, dict) else {}
    gate = (
        _without_action_sizing(copy.deepcopy(source.get("gate")))
        if isinstance(source.get("gate"), dict)
        else {}
    )
    gate["verified"] = False
    reasons = list(gate.get("reasons") or [])
    if not any(
        isinstance(item, dict) and item.get("code") == reason_code
        for item in reasons
    ):
        reasons.append(
            {
                "code": reason_code,
                "message": reason_message,
            }
        )
    gate["reasons"] = reasons
    priority = []
    allowed_priority_fields = {
        "rank",
        "account_id",
        "thscode",
        "name",
        "risk_score",
        "risk_flags",
        "reasons",
    }
    for raw in source.get("priority_draft") or []:
        if isinstance(raw, dict):
            priority.append(
                _without_action_sizing(
                    {key: value for key, value in raw.items() if key in allowed_priority_fields}
                )
            )
    result = {
        "schema_version": source.get("schema_version", 1),
        "generated_at": source.get("generated_at") or _iso_now(),
        "status": "DATA_UNVERIFIED",
        "executable": False,
        "gate": gate,
        "priority_draft": priority,
        "warnings": [
            *_without_action_sizing(list(source.get("warnings") or [])),
            f"{reason_message}；未生成目标权重、调整金额、名义金额或买卖股数。",
        ],
        "note": "执行数据门禁未通过：仅保留无金额的减险复核优先级。",
    }
    for key in ("input_binding", "liquidity_gate", "event_gate", "decision_snapshot_id"):
        if key in source:
            result[key] = _without_action_sizing(copy.deepcopy(source[key]))
    return result


def get_risk_payload(
    force: bool = False,
    quotes_payload: dict[str, Any] | None = None,
    portfolio_id: str = MAIN_PORTFOLIO_ID,
) -> dict[str, Any]:
    """Return a risk snapshot without coupling risk-engine failures to live quotes."""

    portfolio_id = str(portfolio_id or MAIN_PORTFOLIO_ID)
    entry = _resolve_portfolio_entry(portfolio_id)
    if entry is None:
        raise PortfolioNotFound(f"组合不存在：{portfolio_id}")
    try:
        # Lazy import keeps the quote/news dashboard usable while the optional
        # analytics module is unavailable or being upgraded.
        try:
            from scripts.portfolio_risk_engine import get_risk_snapshot
        except ModuleNotFoundError as exc:
            if exc.name not in {"scripts", "scripts.portfolio_risk_engine"}:
                raise
            # ``python3 scripts/dashboard_server.py`` puts scripts/ rather than
            # the repository root at sys.path[0].
            from portfolio_risk_engine import get_risk_snapshot

        if portfolio_id == MAIN_PORTFOLIO_ID:
            captured_quotes = quotes_payload if quotes_payload is not None else get_quotes_payload()
            payload = (
                get_risk_snapshot(captured_quotes, force=True)
                if force
                else get_risk_snapshot(captured_quotes)
            )
        else:
            captured_quotes = get_quotes_payload(portfolio_id=portfolio_id)
            payload = get_risk_snapshot(
                captured_quotes,
                force=True,
                portfolio=copy.deepcopy(entry["config"]),
            )
        if not isinstance(payload, dict):
            raise TypeError("risk snapshot must be a JSON object")
        result = _fail_closed_risk_payload(payload)
        result["portfolio_id"] = portfolio_id
        result["portfolio_label"] = entry["label"]
        return result
    except Exception as exc:
        portfolio, positions, _, config_error = _portfolio_snapshot()
        error = f"{type(exc).__name__}: {exc}"[:500]
        if config_error:
            error = f"{error}; portfolio: {config_error}"[:500]
        return {
            "ok": False,
            "generated_at": _iso_now(),
            "state": "unavailable",
            "risk_state": "UNKNOWN",
            "new_buy_allowed": False,
            "data_quality": {
                "status": "critical",
                "score": None,
                "confidence": None,
                "account_snapshot": {
                    "as_of": portfolio.get("as_of"),
                    "verified": None,
                    "position_count": len(positions),
                },
                "quotes": {},
                "history": {},
                "critical_failures": ["风险引擎暂不可用"],
                "warnings": [],
            },
            "metrics": {},
            "breaches": [
                {
                    "code": "RISK_ENGINE_UNAVAILABLE",
                    "severity": "critical",
                    "metric": "risk_engine",
                    "actual": None,
                    "limit": None,
                    "unit": "",
                    "message": "风险指标未完成计算，禁止把缺失数据视为零风险。",
                }
            ],
            "actions": [
                {
                    "priority": "P0",
                    "kind": "data_quality",
                    "title": "暂停新增买入",
                    "detail": "等待风险引擎恢复并重新核验行情、历史与账户快照。",
                    "symbols": [],
                }
            ],
            "positions": [],
            "history": {
                "status": "unavailable",
                "minimum_sessions": None,
                "portfolio_return_sessions": None,
                "as_of_session": None,
                "partial_session_excluded": None,
                "model_coverage_pct": None,
                "eligible_positions": [],
                "short_history_positions": [],
                "missing_positions": [],
                "sources": [],
            },
            "cache": {
                "source": "fallback",
                "fresh": False,
                "stale": True,
                "age_seconds": None,
                "ttl_seconds": None,
                "path": None,
                "error": error,
            },
        }


def _decision_imports() -> tuple[Any, Any, Any, Any]:
    """Load optional analytics lazily so basic quotes keep working on failure."""
    try:
        from scripts.market_regime_engine import build_market_state
        from scripts.opportunity_radar import build_opportunity_radar
        from scripts.portfolio_event_monitor import build_snapshot as build_event_snapshot
        from scripts.portfolio_rebalance_engine import get_rebalance_plan
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "scripts",
            "scripts.market_regime_engine",
            "scripts.opportunity_radar",
            "scripts.portfolio_event_monitor",
            "scripts.portfolio_rebalance_engine",
        }:
            raise
        from market_regime_engine import build_market_state
        from opportunity_radar import build_opportunity_radar
        from portfolio_event_monitor import build_snapshot as build_event_snapshot
        from portfolio_rebalance_engine import get_rebalance_plan
    return build_market_state, build_opportunity_radar, build_event_snapshot, get_rebalance_plan


def _liquidity_builder_import() -> Any:
    """Load the read-only ADV builder lazily so quote service startup stays cheap."""
    try:
        from scripts.liquidity_payload_builder import build_liquidity_payload
    except ModuleNotFoundError as exc:
        if exc.name not in {"scripts", "scripts.liquidity_payload_builder"}:
            raise
        from liquidity_payload_builder import build_liquidity_payload
    return build_liquidity_payload


def _canonical_json_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redact_error(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    # Known credential namespaces plus legacy high-entropy, non-slug tokens.
    # This intentionally preserves ordinary company names/URLs such as
    # "SK-hynix-announces-new-HBM-memory".
    text = re.sub(
        r"(?i)\bsk-(?:fuyao|proj|svcacct)-[A-Za-z0-9_-]{8,}",
        "[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_]{20,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization\s*:?\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{8,}",
        r"\1 [REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
        r"client[ _-]?secret|api[ _-]?secret|token|password|passwd)\s*[:=]\s*)"
        r"[^\s,;&]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
        r"secret|password|passwd)=)[^&#\s]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit]


def _redact_public_payload(
    value: Any, _state: dict[str, int] | None = None
) -> Any:
    """Recursively redact credentials at the final HTTP serialization boundary."""

    state = _state if _state is not None else {"key_index": 0}
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = key
            if isinstance(key, str):
                candidate = _redact_error(key, max(500, len(key) + 32))
                if candidate != key:
                    state["key_index"] += 1
                    candidate = f"[REDACTED_KEY_{state['key_index']}]"
                safe_key = candidate
            while safe_key in result:
                state["key_index"] += 1
                safe_key = f"[REDACTED_KEY_{state['key_index']}]"
            result[safe_key] = _redact_public_payload(item, state)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_public_payload(item, state) for item in value]
    if isinstance(value, str):
        return _redact_error(value, max(500, len(value) + 32))
    return value


def _unavailable_liquidity(
    portfolio: dict[str, Any], policy: dict[str, Any], error: str, now: datetime
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "payload_type": "portfolio-liquidity-adv.v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "portfolio_as_of": str(portfolio.get("as_of") or ""),
        "data_as_of": str(portfolio.get("as_of") or ""),
        "policy_version": str(policy.get("policy_version") or ""),
        "ok": False,
        "status": "failed",
        "stale": True,
        "input_errors": [
            {"code": "LIQUIDITY_BUILD_UNAVAILABLE", "message": _redact_error(error)}
        ],
        "positions": [],
        "coverage": {
            "complete": False,
            "required_symbol_count": 0,
            "valid_symbol_count": 0,
            "coverage_ratio": 0.0,
        },
    }


def _liquidity_cache_view(
    payload: dict[str, Any], age: float, source: str
) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["cache"] = {
        "source": source,
        "fresh": bool(payload.get("ok")) and age < LIQUIDITY_TTL_SECONDS,
        "age_seconds": round(max(0.0, age), 3),
        "ttl_seconds": LIQUIDITY_TTL_SECONDS,
    }
    return result


def get_liquidity_payload(
    portfolio: dict[str, Any], policy: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Return one input-bound ADV envelope without rebuilding it on every UI poll."""
    observed = (now or _now()).astimezone(CN_TZ)
    input_fingerprint = _canonical_json_sha256(
        {"portfolio": portfolio, "policy": policy}
    )

    def cached_view() -> dict[str, Any] | None:
        with LIQUIDITY_LOCK:
            cached = copy.deepcopy(LIQUIDITY_STATE.get("payload"))
            cached_fingerprint = str(LIQUIDITY_STATE.get("input_fingerprint") or "")
            age = time.monotonic() - float(LIQUIDITY_STATE.get("monotonic") or 0.0)
        if not isinstance(cached, dict) or cached_fingerprint != input_fingerprint:
            return None
        generated = _parse_quote_datetime(cached.get("generated_at"))
        semantic_age = (observed - generated).total_seconds() if generated else None
        ttl = (
            LIQUIDITY_TTL_SECONDS
            if cached.get("ok") is True
            else LIQUIDITY_FAILURE_RETRY_SECONDS
        )
        if (
            age < 0
            or age >= ttl
            or semantic_age is None
            or semantic_age < -60
            or semantic_age >= ttl
        ):
            return None
        return _liquidity_cache_view(cached, max(age, semantic_age), "memory")

    available = cached_view()
    if available is not None:
        return available

    with LIQUIDITY_BUILD_LOCK:
        available = cached_view()
        if available is not None:
            return available
        try:
            builder = _liquidity_builder_import()
            payload = builder(portfolio, policy, generated_at=observed)
            if not isinstance(payload, dict):
                raise TypeError("liquidity builder must return a JSON object")
        except Exception as exc:
            payload = _unavailable_liquidity(
                portfolio,
                policy,
                f"{type(exc).__name__}: {exc}",
                observed,
            )
        errors = payload.get("input_errors") if isinstance(payload.get("input_errors"), list) else []
        error_summary = ", ".join(
            str(item.get("code") or "LIQUIDITY_BUILD_FAILED")
            for item in errors
            if isinstance(item, dict)
        )
        with LIQUIDITY_LOCK:
            LIQUIDITY_STATE["payload"] = copy.deepcopy(payload)
            LIQUIDITY_STATE["input_fingerprint"] = input_fingerprint
            LIQUIDITY_STATE["monotonic"] = time.monotonic()
            LIQUIDITY_STATE["last_error"] = "" if payload.get("ok") is True else error_summary[:500]
        return _liquidity_cache_view(payload, 0.0, "computed")


def _unavailable_market_state(error: str, now: datetime) -> dict[str, Any]:
    return {
        "ok": False,
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot_id": "",
        "regime": "UNVERIFIED",
        "label": "数据待核验",
        "score": None,
        "confidence": 0.0,
        "style": {},
        "components": {},
        "reason_codes": [],
        "guidance": {
            "regime_allows_new_risk": False,
            "message": "市场状态引擎暂不可用；停止依赖该状态新增风险敞口。",
        },
        "quality": {
            "status": "CRITICAL",
            "critical_failures": [error],
            "warnings": [],
        },
        "evidence": {},
    }


def _unavailable_events(
    portfolio: dict[str, Any], policy: dict[str, Any], error: str, now: datetime
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "policy_version": str(policy.get("policy_version") or ""),
        "risk_state": "red",
        "alert_count": 1,
        "counts_by_severity": {"low": 0, "medium": 0, "high": 0, "critical": 1},
        "input_errors": [error],
        "alerts": [
            {
                "id": "event-engine-unavailable",
                "scope": "data_quality",
                "severity": "critical",
                "category": "event_engine_unavailable",
                "title": "事件监控暂不可用",
                "evidence_time": now.isoformat(timespec="seconds"),
                "affected_symbols": [],
                "recommended_action": "暂停依据事件静默扩大风险，待数据恢复后复核。",
            }
        ],
        "notes": ["事件数据缺失时按 fail-closed 原则处理。"],
    }


def _unavailable_opportunities(error: str, now: datetime) -> dict[str, Any]:
    return {
        "ok": False,
        "generated_at": now.isoformat(timespec="seconds"),
        "radar_id": "",
        "new_buy_gate_open": False,
        "items": [],
        "counts_by_state": {},
        "missing_quotes": [],
        "errors": [error],
        "note": "机会雷达暂不可用；不得据此新增买入。",
    }


def _unavailable_rebalance(error: str, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "status": "DATA_UNVERIFIED",
        "executable": False,
        "gate": {
            "verified": False,
            "reasons": [{"code": "ENGINE_UNAVAILABLE", "message": error}],
        },
        "priority_draft": [],
        "warnings": ["再平衡引擎暂不可用；未生成金额或股数。"],
        "note": "数据门禁未通过。",
    }


def _build_decision_context(force: bool = False) -> dict[str, Any]:
    """Capture quotes once, then derive every analytical panel from that copy."""
    observed_at = _now()
    with QUOTE_LOCK:
        quote_cache_is_cold = QUOTE_CACHE.get("payload") is None
    # The basic quote endpoint is deliberately non-blocking on a cold start;
    # the atomic decision endpoint waits for one complete capture before it
    # derives risk, events and the watchlist from that same copy.
    quotes = get_quotes_payload(force=force or quote_cache_is_cold)
    portfolio, _, config_mtime_ns, config_error = _portfolio_snapshot()
    quote_config_mtime = int(
        ((quotes.get("portfolio") or {}).get("config_mtime_ns") or 0)
    )
    if quote_config_mtime != config_mtime_ns:
        quotes = get_quotes_payload(force=True)
        portfolio, _, config_mtime_ns, config_error = _portfolio_snapshot()
        quote_config_mtime = int(
            ((quotes.get("portfolio") or {}).get("config_mtime_ns") or 0)
        )
        if quote_config_mtime != config_mtime_ns:
            raise RuntimeError("持仓配置在统一快照捕获期间持续变化，已停止生成混合时点结果")
    portfolio_fingerprint = portfolio_fingerprint_sha256(portfolio)
    # News keeps its own ten-minute cache. A decision refresh only guarantees a
    # synchronous quote capture; stale or differently-bound news is refreshed
    # in the background and explicitly closes the event/new-risk gate meanwhile.
    news = get_news_payload(force=False)
    news_binding_matches = bool(
        str(news.get("portfolio_id") or "") == str(portfolio.get("portfolio_id") or "")
        and int(news.get("portfolio_config_mtime_ns") or 0) == config_mtime_ns
    )
    if not news_binding_matches:
        ensure_news_refresh(force=True)
    # Whole-market flashes have an independent short cache and are informational
    # evidence only.  Capture one immutable view for this decision snapshot; a
    # refresh may continue in the background for the next snapshot.
    market_news = get_market_news_payload(force=False)

    component_errors: list[str] = []
    if config_error:
        component_errors.append(f"portfolio:{config_error}")
    try:
        daily = _read_json(MARKET_SNAPSHOT_PATH)
    except Exception as exc:
        daily = {}
        component_errors.append(f"daily:{type(exc).__name__}: {exc}")
    try:
        macro = _read_json(MACRO_SNAPSHOT_PATH) if MACRO_SNAPSHOT_PATH.exists() else None
    except Exception as exc:
        macro = None
        component_errors.append(f"macro:{type(exc).__name__}: {exc}")
    try:
        market_policy = _read_json(MARKET_POLICY_PATH)
        monitoring_policy = _read_json(MONITORING_POLICY_PATH)
        risk_policy = _read_json(RISK_POLICY_PATH)
        build_market_state, build_opportunity_radar, build_event_snapshot, get_rebalance_plan = (
            _decision_imports()
        )
    except Exception as exc:
        message = f"analytics:{type(exc).__name__}: {exc}"[:500]
        component_errors.append(message)
        market_policy, monitoring_policy, risk_policy = {}, {}, {}
        build_market_state = build_opportunity_radar = build_event_snapshot = get_rebalance_plan = None

    try:
        liquidity = get_liquidity_payload(portfolio, risk_policy, now=observed_at)
    except Exception as exc:  # defensive: helper normally returns its own failure envelope
        error = _redact_error(f"{type(exc).__name__}: {exc}")
        component_errors.append(f"liquidity:{error}")
        liquidity = _unavailable_liquidity(portfolio, risk_policy, error, observed_at)

    try:
        risk = get_risk_payload(force=False, quotes_payload=quotes)
    except Exception as exc:  # get_risk_payload normally returns its own fallback
        component_errors.append(f"risk:{type(exc).__name__}: {exc}")
        risk = get_risk_payload(force=False)
    # Defence in depth: callers and tests may inject an alternate risk provider.
    # The unified API must still never publish action sizing for an old account.
    risk = _fail_closed_risk_payload(risk)
    captured_quote_validation = (
        quotes.get("quote_validation")
        if isinstance(quotes.get("quote_validation"), dict)
        else {}
    )
    if captured_quote_validation.get("decision_verified") is not True:
        risk = _fail_closed_risk_payload(
            risk,
            force=True,
            reason="股票行情未通过授权源交叉校验",
        )

    try:
        if build_market_state is None:
            raise RuntimeError("market-state engine import failed")
        market_state = build_market_state(
            quotes, daily, macro, market_policy, now=observed_at
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        component_errors.append(f"market_state:{error}")
        market_state = _unavailable_market_state(error, observed_at)

    event_input_errors: list[str] = []
    if not (quotes.get("positions") or []):
        event_input_errors.append("持仓行情未返回")
    if not (news.get("items") or []) and not news.get("refreshing"):
        event_input_errors.append("持仓新闻未返回")
    if not news_binding_matches:
        event_input_errors.append("新闻缓存未绑定当前持仓配置，正在后台刷新")
    with DECISION_LOCK:
        previous = copy.deepcopy(DECISION_STATE.get("payload") or {})
    previous_events = previous.get("events") if isinstance(previous.get("events"), dict) else None
    try:
        if build_event_snapshot is None:
            raise RuntimeError("event-monitor import failed")
        events = build_event_snapshot(
            portfolio,
            monitoring_policy,
            quotes=quotes,
            news=news,
            macro=macro,
            previous_payload=previous_events,
            now=observed_at,
            input_errors=event_input_errors,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        component_errors.append(f"events:{error}")
        events = _unavailable_events(portfolio, monitoring_policy, error, observed_at)

    try:
        if build_opportunity_radar is None:
            raise RuntimeError("opportunity-radar import failed")
        watchlist_mtime_ns = WATCHLIST_PATH.stat().st_mtime_ns
        watchlist = _read_json(WATCHLIST_PATH)
        opportunities = build_opportunity_radar(
            watchlist,
            quotes,
            daily,
            market_state,
            risk,
            now=observed_at,
        )
        if WATCHLIST_PATH.stat().st_mtime_ns != watchlist_mtime_ns:
            raise RuntimeError("观察名单在统一快照计算期间发生变化")
    except Exception as exc:
        watchlist_mtime_ns = None
        error = f"{type(exc).__name__}: {exc}"[:500]
        component_errors.append(f"opportunities:{error}")
        opportunities = _unavailable_opportunities(error, observed_at)

    captured_market_status = (
        quotes.get("market_status") if isinstance(quotes.get("market_status"), dict) else {}
    )
    evidence = {
        "portfolio_id": portfolio.get("portfolio_id"),
        "portfolio_as_of": portfolio.get("as_of"),
        "portfolio_config_mtime_ns": config_mtime_ns,
        "watchlist_mtime_ns": watchlist_mtime_ns,
        "quote_time": captured_market_status.get("quote_time"),
        "quote_prices": [
            (
                row.get("thscode"),
                row.get("price"),
                row.get("quote_time"),
                row.get("data_source"),
                ((row.get("official_validation") or {}).get("state")),
            )
            for row in [*(quotes.get("positions") or []), *(quotes.get("watchlist") or [])]
        ],
        "quote_validation": quotes.get("quote_validation") or {},
        "completed_session": daily.get("data_date"),
        "macro_period": ((macro or {}).get("pmi") or {}).get("period")
        if isinstance((macro or {}).get("pmi"), dict)
        else None,
        "news_generated_at": news.get("generated_at"),
        "market_news_content_fingerprint": market_news.get("content_fingerprint"),
        "market_news_latest_event_at": market_news.get("latest_event_at"),
        "market_news_last_success_at": market_news.get("last_success_at"),
        "market_news_source_status": market_news.get("source_status") or {},
        "risk_fingerprint": ((risk.get("cache") or {}).get("input_fingerprint")),
        "liquidity_binding_sha256": (
            (liquidity.get("input_binding") or {}).get("binding_sha256")
            if isinstance(liquidity.get("input_binding"), dict)
            else None
        ),
        "market_snapshot_id": market_state.get("snapshot_id"),
        "event_ids": [item.get("id") for item in events.get("alerts") or []],
        "radar_id": opportunities.get("radar_id"),
    }
    snapshot_id = hashlib.sha256(
        json.dumps(
            evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:20]
    # Add the display binding before the rebalance engine hashes its inputs, so
    # the returned risk/event objects exactly match the auditable SHA-256 body.
    for component in (
        quotes,
        news,
        market_news,
        risk,
        liquidity,
        market_state,
        events,
        opportunities,
    ):
        if isinstance(component, dict):
            component["decision_snapshot_id"] = snapshot_id

    try:
        if get_rebalance_plan is None:
            raise RuntimeError("rebalance engine import failed")
        rebalance = get_rebalance_plan(
            risk,
            portfolio,
            risk_policy,
            liquidity_payload=liquidity,
            event_payload=events,
            now=observed_at,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        component_errors.append(f"rebalance:{error}")
        rebalance = _unavailable_rebalance(error, observed_at)
    rebalance["decision_snapshot_id"] = snapshot_id

    portfolio_summary = quotes.get("portfolio") if isinstance(quotes.get("portfolio"), dict) else {}
    market_status = quotes.get("market_status") if isinstance(quotes.get("market_status"), dict) else {}
    quote_cache = quotes.get("cache") if isinstance(quotes.get("cache"), dict) else {}
    quote_validation = (
        quotes.get("quote_validation")
        if isinstance(quotes.get("quote_validation"), dict)
        else {}
    )
    risk_quality = risk.get("data_quality") if isinstance(risk.get("data_quality"), dict) else {}
    account_quality = (
        risk_quality.get("account_snapshot")
        if isinstance(risk_quality.get("account_snapshot"), dict)
        else {}
    )
    quote_coverage = int(portfolio_summary.get("quote_coverage_count") or 0)
    position_count = int(portfolio_summary.get("position_count") or 0)
    position_rows = quotes.get("positions") if isinstance(quotes.get("positions"), list) else []
    quote_fresh = bool(
        position_count > 0
        and quote_coverage == position_count
        and len(position_rows) == position_count
        and all(not row.get("is_stale") for row in position_rows if isinstance(row, dict))
        and not market_status.get("feed_stale")
        and not quote_cache.get("expired")
    )
    official_validation_verified = quote_validation.get("decision_verified") is True
    liquidity_gate = (
        rebalance.get("liquidity_gate")
        if isinstance(rebalance.get("liquidity_gate"), dict)
        else {}
    )
    liquidity_verified = liquidity_gate.get("verified") is True
    account_current = bool(
        account_quality.get("current_verified") is True
        and account_quality.get("verified") is True
        and account_quality.get("stale") is False
    )
    if not account_current:
        rebalance = _fail_closed_rebalance_payload(rebalance)
        rebalance["decision_snapshot_id"] = snapshot_id
    if not quote_fresh:
        rebalance = _fail_closed_rebalance_payload(
            rebalance,
            reason_code="LIVE_QUOTE_NOT_FRESH",
            reason_message="持仓行情未达到全覆盖且盘中可用状态",
        )
        rebalance["decision_snapshot_id"] = snapshot_id
    if not official_validation_verified:
        rebalance = _fail_closed_rebalance_payload(
            rebalance,
            reason_code="OFFICIAL_QUOTE_VALIDATION_FAILED",
            reason_message="股票行情未通过同花顺授权源交叉校验",
        )
        rebalance["decision_snapshot_id"] = snapshot_id
    critical: list[str] = []
    warnings: list[str] = []
    if not account_current:
        critical.append("账户快照不是当前交易日核验数据；实时价格不等于实时持仓")
    if not quote_fresh:
        critical.append("持仓行情未达到全覆盖且可用状态")
    if not official_validation_verified:
        critical.append("股票行情未通过同花顺授权源交叉校验；仅保留观察模式")
    if market_state.get("regime") == "UNVERIFIED":
        critical.append("市场状态证据未通过门禁")
    if risk.get("state") == "DATA_UNVERIFIED" or not risk.get("ok"):
        critical.append("组合风险快照未通过数据质量门禁")
    if macro is None:
        warnings.append("官方宏观快照尚未载入；市场状态按可用分项重新归一化")
    if news.get("stale"):
        warnings.append("新闻缓存已超过刷新周期，后台正在尝试更新")
    market_news_degraded = bool(
        market_news.get("stale")
        or market_news.get("degraded")
        or market_news.get("errors")
        or str(market_news.get("status") or "").lower() != "healthy"
    )
    if market_news_degraded:
        warnings.append("全市场快讯为过期或部分来源数据；仅供人工查看，不参与自动交易信号")
    if events.get("input_errors"):
        critical.extend(
            f"事件监控输入错误：{value}" for value in events.get("input_errors") or []
        )
    if rebalance.get("status") == "DATA_UNVERIFIED":
        critical.append("再平衡草案的数据门禁未通过；仅可查看无金额减险优先级")
    if not liquidity_verified:
        critical.append("20日成交容量证据未通过绑定、完整性或时效门禁")
    for error in component_errors:
        (warnings if error.startswith("macro:") else critical).append(error)
    critical = list(dict.fromkeys(critical))
    warnings = list(dict.fromkeys(warnings))

    macro_period = None
    if isinstance(macro, dict):
        pmi = macro.get("pmi") if isinstance(macro.get("pmi"), dict) else {}
        social = macro.get("social_financing") if isinstance(macro.get("social_financing"), dict) else {}
        macro_period = pmi.get("period") or social.get("latest_month")
    # Analytics above can be comparatively expensive. Revalidate the portfolio
    # after the rebalance engine finishes so an edit made mid-build can never be
    # published as a mixed-config decision snapshot.
    final_portfolio, _, final_config_mtime_ns, final_config_error = _portfolio_snapshot()
    if (
        final_config_error
        or final_config_mtime_ns != config_mtime_ns
        or str(final_portfolio.get("portfolio_id") or "")
        != str(portfolio.get("portfolio_id") or "")
        or str(final_portfolio.get("as_of") or "")
        != str(portfolio.get("as_of") or "")
        or portfolio_fingerprint_sha256(final_portfolio) != portfolio_fingerprint
    ):
        raise RuntimeError("持仓配置在统一快照计算期间发生变化，已停止生成混合版本结果")

    position_advice = _cached_position_advice_payload(
        final_portfolio, config_error=final_config_error
    )
    ensure_position_advice_refresh(
        snapshot_id,
        final_portfolio,
        config_mtime_ns=config_mtime_ns,
        quotes=quotes,
        position_news=news,
        market_news=market_news,
        market_state=market_state,
        risk=risk,
        rebalance=rebalance,
        input_errors=critical,
    )
    # Scheduling is deliberately after the first version fence. Fence once more
    # around the hand-off; the worker independently checks this same binding
    # before and after its disk transaction.
    post_advice_portfolio, _, post_advice_mtime_ns, post_advice_error = _portfolio_snapshot()
    if (
        post_advice_error
        or post_advice_mtime_ns != config_mtime_ns
        or str(post_advice_portfolio.get("portfolio_id") or "")
        != str(portfolio.get("portfolio_id") or "")
        or str(post_advice_portfolio.get("as_of") or "")
        != str(portfolio.get("as_of") or "")
        or portfolio_fingerprint_sha256(post_advice_portfolio)
        != portfolio_fingerprint
    ):
        raise RuntimeError("持仓配置在建议刷新调度期间发生变化，已停止发布混合版本结果")
    quotes = _attach_position_advice(quotes, position_advice)
    if not position_advice.get("ok"):
        warnings.append(str(position_advice.get("message") or "当前操作建议暂不可用"))
    warnings = list(dict.fromkeys(warnings))
    component_status = {
        "quotes": "GOOD" if quote_fresh else "CRITICAL",
        "official_quote_validation": "GOOD" if official_validation_verified else "CRITICAL",
        "news": "DEGRADED" if news.get("stale") or news.get("errors") else "GOOD",
        "market_news": "DEGRADED" if market_news_degraded else "GOOD",
        "risk": str(risk_quality.get("status") or "CRITICAL").upper(),
        "market_state": str((market_state.get("quality") or {}).get("status") or "CRITICAL").upper(),
        "events": "CRITICAL" if events.get("input_errors") else str(events.get("risk_state") or "unknown").upper(),
        "opportunities": "GOOD" if opportunities.get("ok") else "DEGRADED",
        "liquidity": "GOOD" if liquidity_verified else "CRITICAL",
        "rebalance": "GOOD" if rebalance.get("status") in {"READY", "READY_WITH_REVIEW"} else "BLOCKED",
        "position_advice": "GOOD" if position_advice.get("ok") else "BLOCKED",
        "macro": "GOOD" if macro is not None else "DEGRADED",
    }
    return {
        "schema_version": "decision-context.v1",
        "ok": bool(quotes.get("positions")),
        "decision_ready": not critical,
        "generated_at": observed_at.isoformat(timespec="seconds"),
        "snapshot_id": snapshot_id,
        "portfolio_binding": {
            "portfolio_id": portfolio.get("portfolio_id"),
            "portfolio_as_of": portfolio.get("as_of"),
            "config_mtime_ns": config_mtime_ns,
            "fingerprint_sha256": portfolio_fingerprint,
        },
        "as_of": {
            "account": portfolio.get("as_of"),
            "quote": market_status.get("quote_time"),
            "completed_session": daily.get("data_date"),
            "liquidity": liquidity.get("data_as_of"),
            "macro": macro_period,
            "news": news.get("last_success_at") or news.get("generated_at"),
            "market_news": market_news.get("latest_event_at")
            or market_news.get("last_success_at"),
            "position_advice": position_advice.get("generated_at"),
        },
        "data_quality": {
            "status": "CRITICAL" if critical else "DEGRADED" if warnings else "GOOD",
            "account_current": account_current,
            "quote_fresh": quote_fresh,
            "official_quote_validation_verified": official_validation_verified,
            "component_status": component_status,
            "errors": critical,
            "warnings": warnings,
        },
        "quotes": quotes,
        "news": news,
        "market_news": market_news,
        "risk": risk,
        "liquidity": liquidity,
        "market_state": market_state,
        "events": events,
        "opportunities": opportunities,
        "rebalance": rebalance,
        "position_advice": position_advice,
        "sources": {
            "live_quotes": "腾讯财经/新浪财经公开行情；同花顺授权行情独立交叉校验与显式备援",
            "daily": daily.get("sources") or {},
            "macro": (macro or {}).get("sources") or {},
            "news": news.get("source_status") or {},
            "market_news": market_news.get("source_status") or {},
            "liquidity": liquidity.get("evidence") or {},
        },
    }


def _decision_cache_view(payload: dict[str, Any], age: float, source: str) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["cache"] = {
        "source": source,
        "fresh": age < DECISION_TTL_SECONDS,
        "age_seconds": round(max(0.0, age), 3),
        "ttl_seconds": DECISION_TTL_SECONDS,
    }
    return result


def _decision_cache_matches_current_portfolio(payload: dict[str, Any]) -> bool:
    """Reject a cached decision after any account-snapshot edit."""

    try:
        portfolio, _, config_mtime_ns, config_error = _portfolio_snapshot()
        binding = (
            payload.get("portfolio_binding")
            if isinstance(payload.get("portfolio_binding"), dict)
            else {}
        )
        return bool(
            not config_error
            and str(binding.get("portfolio_id") or "")
            == str(portfolio.get("portfolio_id") or "")
            and str(binding.get("portfolio_as_of") or "")
            == str(portfolio.get("as_of") or "")
            and int(binding.get("config_mtime_ns") or 0) == config_mtime_ns
            and str(binding.get("fingerprint_sha256") or "")
            == portfolio_fingerprint_sha256(portfolio)
        )
    except Exception:
        return False


def get_decision_context(force: bool = False) -> dict[str, Any]:
    with DECISION_LOCK:
        cached = copy.deepcopy(DECISION_STATE.get("payload"))
        age = time.monotonic() - float(DECISION_STATE.get("monotonic") or 0.0)
        observed_generation = int(DECISION_STATE.get("generation") or 0)
    if isinstance(cached, dict) and not _decision_cache_matches_current_portfolio(cached):
        cached = None
    if isinstance(cached, dict) and not force:
        stale = age >= DECISION_TTL_SECONDS
        result = _decision_cache_view(
            cached, age, "stale-memory" if stale else "memory"
        )
        if stale:
            # Reads must not queue behind a potentially slow analytics/advice
            # build.  The lifecycle-owned maintenance worker coalesces repeated
            # revalidation requests while this stale snapshot remains usable.
            try:
                ensure_decision_refresh()
            except Exception as exc:
                # A thread-start/resource failure must not turn a usable stale
                # snapshot into an HTTP failure.  Health still exposes the
                # scheduling error for diagnosis.
                with DECISION_LOCK:
                    DECISION_STATE["last_error"] = (
                        f"统一决策后台刷新启动失败：{type(exc).__name__}: {exc}"
                    )[:800]
        return result

    with DECISION_BUILD_LOCK:
        with DECISION_LOCK:
            cached = copy.deepcopy(DECISION_STATE.get("payload"))
            age = time.monotonic() - float(DECISION_STATE.get("monotonic") or 0.0)
            current_generation = int(DECISION_STATE.get("generation") or 0)
        if isinstance(cached, dict) and not _decision_cache_matches_current_portfolio(cached):
            cached = None
        if isinstance(cached, dict) and not force:
            return _decision_cache_view(
                cached,
                age,
                "stale-memory" if age >= DECISION_TTL_SECONDS else "memory",
            )
        if (
            isinstance(cached, dict)
            and force
            and current_generation != observed_generation
        ):
            # Another caller completed a matching rebuild while this request
            # waited for the single-flight lock.  Reuse that newer generation
            # even for a forced request instead of serially rebuilding it again.
            return _decision_cache_view(cached, age, "coalesced-memory")
        try:
            payload = _build_decision_context(force=force)
            with DECISION_LOCK:
                DECISION_STATE["payload"] = copy.deepcopy(payload)
                DECISION_STATE["monotonic"] = time.monotonic()
                DECISION_STATE["generation"] = (
                    int(DECISION_STATE.get("generation") or 0) + 1
                )
                DECISION_STATE["last_error"] = ""
            return _decision_cache_view(payload, 0.0, "computed")
        except Exception as exc:
            error = f"统一决策快照刷新失败：{type(exc).__name__}: {exc}"[:800]
            with DECISION_LOCK:
                DECISION_STATE["last_error"] = error
            if isinstance(cached, dict):
                fallback = _decision_cache_view(cached, age, "stale-memory")
                fallback["ok"] = False
                fallback["decision_ready"] = False
                quality = fallback.setdefault("data_quality", {})
                quality["status"] = "CRITICAL"
                quality["errors"] = list(dict.fromkeys([*(quality.get("errors") or []), error]))
                fallback["cache"]["fresh"] = False
                fallback["cache"]["error"] = error
                return fallback
            return {
                "schema_version": "decision-context.v1",
                "ok": False,
                "decision_ready": False,
                "generated_at": _iso_now(),
                "snapshot_id": "",
                "as_of": {},
                "data_quality": {
                    "status": "CRITICAL",
                    "account_current": False,
                    "quote_fresh": False,
                    "component_status": {"market_news": "DEGRADED"},
                    "errors": [error],
                    "warnings": [],
                },
                "quotes": {},
                "news": {},
                "market_news": _empty_market_news_payload(error),
                "risk": {},
                "market_state": _unavailable_market_state(error, _now()),
                "events": {},
                "opportunities": {},
                "rebalance": _unavailable_rebalance(error, _now()),
                "cache": {"source": "none", "fresh": False, "error": error},
            }


def _personal_index_symbol(code: str) -> str:
    if code.startswith(
        ("600", "601", "603", "605", "688", "510", "512", "515", "516", "560", "561", "562", "563", "588")
    ):
        return f"sh{code}"
    return f"sz{code}"


def _personal_index_quote_rows() -> tuple[dict[str, dict[str, Any]], str]:
    """Batch-fetch OHLCV snapshots for index constituents and benchmarks."""
    symbols = [_personal_index_symbol(code) for code in PERSONAL_INDEX_HOLDINGS]
    symbols.extend(PERSONAL_INDEX_BENCHMARKS)
    quotes, trade_date = _live_quote_ohlcv(symbols)
    rows = {
        key: {
            "open": value["open"],
            "close": value["close"],
            "high": value["high"],
            "low": value["low"],
            # 个人指数历史用"股"口径，日K序列用"手"，两者相差 100 倍。
            "volume": value["volume_lots"] * 100.0,
            "date": value["date"],
        }
        for key, value in quotes.items()
    }
    return rows, trade_date


def _personal_index_market_open(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 7


def _personal_index_build_payload(force: bool) -> dict[str, Any]:
    """Static personal-index K-line merged with an intraday provisional bar.

    Mirrors the conventions of stock_all_in_1/update_personal_index.py:
    index point = 1000 * mv / base_mv, pnl_cum chained from the previous bar.
    Any live-merge failure degrades gracefully to the static file content.
    """
    now = datetime.now(tz=CN_TZ)
    today = now.strftime("%Y-%m-%d")
    try:
        with PERSONAL_INDEX_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"personal index data unavailable: {exc}"}

    payload.setdefault("kline", [])
    bars: list[dict[str, Any]] = payload["kline"]
    warnings: list[str] = []
    live_applied = False

    # 盘中实时合并；收盘后（15:07 起）同一交易日仍要用收盘行情修正"今日"bar，
    # 否则静态文件里盘中定格的快照（如 13:50 写入）会被当成收盘值长期展示，
    # 当日盈亏与收盘口径不符。周末/盘前不合并（行情日期不等于今天会被跳过）。
    minutes_now = now.hour * 60 + now.minute
    post_close_same_day = (
        now.weekday() < 5
        and not _personal_index_market_open(now)
        and minutes_now >= 15 * 60 + 7
    )
    if _personal_index_market_open(now) or force or post_close_same_day:
        try:
            rows, quote_date = _personal_index_quote_rows()
            if quote_date and quote_date != today:
                warnings.append(f"quote date {quote_date} is not {today}")
            elif rows:
                base_mv = float(payload.get("base_mv") or 0.0)
                if base_mv <= 0:
                    warnings.append("base_mv missing in static payload")
                else:
                    benchmarks = payload.setdefault("benchmarks", {})
                    bench_closes_today: dict[str, float] = {}
                    for bench_code, bench_name in PERSONAL_INDEX_BENCHMARKS.items():
                        node = benchmarks.setdefault(bench_code, {"name": bench_name, "kline": []})
                        bench_rows: list[dict[str, Any]] = node.setdefault("kline", [])
                        base_raw = next(
                            (
                                row.get("raw_close")
                                for row in bench_rows
                                if row.get("date") == PERSONAL_INDEX_BASE_DATE
                            ),
                            None,
                        )
                        quote = rows.get(bench_code)
                        if not base_raw or not quote:
                            continue
                        raw_close = float(quote["close"])
                        close = round(raw_close / float(base_raw) * 1000.0, 2)
                        prev_row = None
                        if bench_rows:
                            last_row = bench_rows[-1]
                            prev_row = (
                                last_row
                                if last_row.get("date") != today and bench_rows
                                else (bench_rows[-2] if len(bench_rows) > 1 else None)
                            )
                        prev_close = float(prev_row["close"]) if prev_row and prev_row.get("close") else None
                        row = {
                            "date": today,
                            "close": close,
                            "pct_change": (
                                round((close / prev_close - 1) * 100, 2) if prev_close else None
                            ),
                            "raw_close": raw_close,
                        }
                        if bench_rows and bench_rows[-1].get("date") == today:
                            bench_rows[-1] = row
                        else:
                            bench_rows.append(row)
                        bench_closes_today[bench_code] = close

                    prev_bar = None
                    for bar in bars:
                        if str(bar.get("date")) < today:
                            prev_bar = bar
                    mv_open = mv_close = mv_high = mv_low = 0.0
                    volume = 0.0
                    missing: list[str] = []
                    for code, (name, qty) in PERSONAL_INDEX_HOLDINGS.items():
                        quote = rows.get(_personal_index_symbol(code))
                        if not quote:
                            missing.append(name)
                            continue
                        mv_open += quote["open"] * qty
                        mv_close += quote["close"] * qty
                        mv_high += quote["high"] * qty
                        mv_low += quote["low"] * qty
                        volume += quote["volume"] * qty
                    if missing:
                        warnings.append("missing quotes: " + ",".join(missing))
                    if not missing and mv_close > 0 and prev_bar is not None:
                        def point(mv: float) -> float:
                            return round(1000.0 * mv / base_mv, 2)

                        close = point(mv_close)
                        prev_close = float(prev_bar.get("close") or close)
                        prev_mv = float(prev_bar.get("mv") or 0.0)
                        prev_pnl_cum = prev_bar.get("pnl_cum")
                        pnl_today = round(mv_close - prev_mv, 2) if prev_mv else None
                        bar: dict[str, Any] = {
                            "date": today,
                            "open": point(mv_open),
                            "close": close,
                            "high": point(mv_high),
                            "low": point(mv_low),
                            "volume": volume,
                            "mv": round(mv_close, 2),
                            "pct_change": (
                                round((close / prev_close - 1) * 100, 2) if prev_close else None
                            ),
                            "pnl_today": pnl_today,
                            "pnl_cum": (
                                round(float(prev_pnl_cum) + (mv_close - prev_mv), 2)
                                if (prev_pnl_cum is not None and prev_mv)
                                else None
                            ),
                            "composition": {
                                "available": len(PERSONAL_INDEX_HOLDINGS),
                                "total": len(PERSONAL_INDEX_HOLDINGS),
                                "missing_codes": [],
                            },
                        }
                        closes = [
                            float(hist_bar["close"])
                            for hist_bar in bars
                            if hist_bar.get("close") is not None and str(hist_bar.get("date")) < today
                        ]
                        closes.append(close)
                        for window in (5, 10, 20):
                            tail = closes[-window:]
                            bar[f"ma{window}"] = (
                                round(sum(tail) / window, 2) if len(tail) >= window else None
                            )
                        for bench_code in PERSONAL_INDEX_BENCHMARKS:
                            if bench_code in bench_closes_today:
                                bar[f"alpha_{bench_code}"] = round(
                                    close - bench_closes_today[bench_code], 2
                                )
                        if bars and bars[-1].get("date") == today:
                            bars[-1] = bar
                        else:
                            bars.append(bar)
                        payload["update_date"] = today
                        payload["total_mv_today"] = round(mv_close, 2)
                        live_applied = True
        except Exception as exc:  # defensive: static data must survive live failures
            warnings.append(f"live merge failed: {type(exc).__name__}: {exc}")

    payload["intraday_live"] = live_applied
    payload["generated_at"] = now.isoformat(timespec="seconds")
    if warnings:
        payload["warnings"] = warnings
    return payload


def get_personal_index_payload(force: bool = False) -> dict[str, Any]:
    now_monotonic = time.monotonic()
    cache = _PERSONAL_INDEX_CACHE
    if not force and cache["payload"] is not None and now_monotonic < float(cache["expires_at"]):
        return cache["payload"]
    payload = _personal_index_build_payload(force)
    cache["payload"] = payload
    cache["expires_at"] = now_monotonic + PERSONAL_INDEX_TTL_SECONDS
    return payload


def _runtime_diagnostics() -> dict[str, Any]:
    """Expose enough revision state to detect a stale in-memory server safely."""

    try:
        source_mtime_ns_on_disk: int | None = Path(__file__).stat().st_mtime_ns
    except OSError:
        source_mtime_ns_on_disk = None
    return {
        "started_at": SERVER_PROCESS_STARTED_AT,
        "source_mtime_ns_at_start": SERVER_SOURCE_MTIME_NS_AT_START,
        "source_mtime_ns_on_disk": source_mtime_ns_on_disk,
        "source_in_sync": source_mtime_ns_on_disk == SERVER_SOURCE_MTIME_NS_AT_START,
        "api_capabilities": list(SERVER_API_CAPABILITIES),
    }


def get_health_payload() -> dict[str, Any]:
    portfolio, configured_positions, config_mtime_ns, config_error = _portfolio_snapshot()
    refresh_profile = _background_refresh_profile()
    news_ttl_seconds = float(refresh_profile["position_news_seconds"])
    eastmoney_circuit = _eastmoney_circuit_snapshot()
    service = BACKGROUND_REFRESH_SERVICE
    background_status = (
        service.snapshot()
        if service is not None
        else {
            "running": False,
            "phase": refresh_profile["phase"],
            "interval_seconds": {
                "quote": refresh_profile["quote_seconds"],
                "position_news": refresh_profile["position_news_seconds"],
                "market_news": refresh_profile["market_news_seconds"],
                "daily_snapshot": refresh_profile["daily_snapshot_seconds"],
                "macro_snapshot": refresh_profile["macro_snapshot_seconds"],
                "data_retention": refresh_profile["data_retention_seconds"],
                "decision": refresh_profile["decision_seconds"],
            },
            "next_due_seconds": {},
            "last_tick_at": "",
            "last_errors": {},
        }
    )
    with QUOTE_LOCK:
        quote_age = (
            time.monotonic() - float(QUOTE_CACHE["monotonic"])
            if QUOTE_CACHE["payload"] is not None
            else None
        )
        quote_error = str(QUOTE_CACHE["last_error"] or "")
        quote_refreshing = bool(QUOTE_CACHE["refreshing"])
    with NEWS_LOCK:
        news_age = (
            time.monotonic() - float(NEWS_STATE["monotonic"])
            if NEWS_STATE["payload"] is not None
            else None
        )
        news_refreshing = bool(NEWS_STATE["refreshing"])
        news_error = str(NEWS_STATE["last_error"] or "")
    with MARKET_NEWS_LOCK:
        market_news_payload = copy.deepcopy(MARKET_NEWS_STATE.get("payload") or {})
        market_news_age = (
            time.monotonic() - float(MARKET_NEWS_STATE["monotonic"])
            if market_news_payload.get("last_success_at")
            else None
        )
        market_news_refreshing = bool(MARKET_NEWS_STATE["refreshing"])
        market_news_error = str(MARKET_NEWS_STATE["last_error"] or "")
        market_news_retry_after = max(
            0.0,
            float(MARKET_NEWS_STATE.get("next_retry_monotonic") or 0.0) - time.monotonic(),
        )
    with LIQUIDITY_LOCK:
        liquidity_payload = copy.deepcopy(LIQUIDITY_STATE.get("payload") or {})
        liquidity_age = (
            time.monotonic() - float(LIQUIDITY_STATE.get("monotonic") or 0.0)
            if liquidity_payload
            else None
        )
        liquidity_error = str(LIQUIDITY_STATE.get("last_error") or "")
    with INTRADAY_PNL_LOCK:
        intraday_state = copy.deepcopy(INTRADAY_PNL_STATE)
    advice_payload = _get_position_advice_payload()
    with POSITION_ADVICE_LOCK:
        advice_state = copy.deepcopy(POSITION_ADVICE_STATE)
    market_news_stale = bool(
        market_news_age is None
        or market_news_age >= MARKET_NEWS_TTL_SECONDS + MARKET_NEWS_STALE_GRACE_SECONDS
        or market_news_payload.get("status") == "stale"
    )
    official_codes = [
        str(item.get("thscode") or "")
        for item in configured_positions
        if _is_a_share_stock(item)
    ]
    official_quote_health = HITHINK_QUOTE_PROVIDER.snapshot(
        official_codes, str(refresh_profile.get("phase") or "closed")
    )
    return {
        "ok": True,
        "server_time": _iso_now(),
        "runtime": _runtime_diagnostics(),
        "bind_scope": "loopback-only",
        "configured_positions": len(configured_positions),
        "portfolio": {
            "as_of": portfolio.get("as_of"),
            "config_mtime_ns": config_mtime_ns,
            "reload_error": config_error,
        },
        "quote": {
            "available": quote_age is not None,
            "age_seconds": None if quote_age is None else round(quote_age, 2),
            "ttl_seconds": QUOTE_TTL_SECONDS,
            "max_stale_seconds": QUOTE_MAX_STALE_SECONDS,
            "refreshing": quote_refreshing,
            "last_error": quote_error,
        },
        "official_quote_validation": {
            key: value
            for key, value in official_quote_health.items()
            if key != "quotes"
        },
        "data_source_chains": _data_source_chains_snapshot(),
        "news": {
            "available": news_age is not None,
            "age_seconds": None if news_age is None else round(news_age, 1),
            "ttl_seconds": news_ttl_seconds,
            "refreshing": news_refreshing,
            "last_error": news_error,
        },
        "market_news": {
            "available": bool(market_news_payload.get("last_success_at")),
            "ok": bool(market_news_payload.get("ok")) and not market_news_stale,
            "status": "stale" if market_news_stale else market_news_payload.get("status", "unavailable"),
            "age_seconds": None if market_news_age is None else round(max(0.0, market_news_age), 1),
            "ttl_seconds": MARKET_NEWS_TTL_SECONDS,
            "retry_after_seconds": round(market_news_retry_after, 1),
            "stale": market_news_stale,
            "refreshing": market_news_refreshing,
            "last_success_at": market_news_payload.get("last_success_at", ""),
            "latest_event_at": market_news_payload.get("latest_event_at", ""),
            "last_error": market_news_error,
        },
        "liquidity": {
            "available": bool(liquidity_payload),
            "ok": liquidity_payload.get("ok") is True,
            "status": liquidity_payload.get("status", "unavailable"),
            "age_seconds": (
                None if liquidity_age is None else round(max(0.0, liquidity_age), 1)
            ),
            "ttl_seconds": LIQUIDITY_TTL_SECONDS,
            "data_as_of": liquidity_payload.get("data_as_of"),
            "coverage": liquidity_payload.get("coverage") or {},
            "last_error": liquidity_error,
        },
        "intraday_pnl": {
            "available": bool(intraday_state.get("last_recorded_at")),
            "last_attempt_at": intraday_state.get("last_attempt_at", ""),
            "last_recorded_at": intraday_state.get("last_recorded_at", ""),
            "last_result": intraday_state.get("last_result") or {},
            "last_error": intraday_state.get("last_error", ""),
        },
        "position_advice": {
            "available": advice_payload.get("ok") is True,
            "position_count": len(advice_payload.get("positions") or []),
            "generated_at": advice_payload.get("generated_at", ""),
            "last_attempt_at": advice_state.get("last_attempt_at", ""),
            "last_success_at": advice_state.get("last_success_at", ""),
            "last_error": advice_state.get("last_error", ""),
        },
        "background_refresh": background_status,
        "maintenance": _maintenance_health(),
        "eastmoney_circuit": eastmoney_circuit,
        "portfolios": {
            "count": len(_all_portfolio_entries()),
            "user_defined_count": len(_all_portfolio_entries()) - 1,
            "extras_error": _extra_portfolios_snapshot()[2],
        },
        "credentials": "No API key is read or exposed by this service.",
    }


def get_portfolios_payload() -> dict[str, Any]:
    """List every portfolio with light live summaries for the tab bar."""

    entries = _all_portfolio_entries()
    _, _, extras_error = _extra_portfolios_snapshot()
    summaries: dict[str, Any] = {}
    with QUOTE_LOCK:
        cached = copy.deepcopy(QUOTE_CACHE.get("payload"))
    if isinstance(cached, dict):
        _backfill_portfolio_views_if_missing(cached)
        main_summary = cached.get("portfolio") or {}
        summaries[MAIN_PORTFOLIO_ID] = {
            "market_value": main_summary.get("market_value"),
            "daily_pnl": main_summary.get("daily_pnl"),
            "net_equity": main_summary.get("net_equity"),
        }
        for pid, view in (cached.get("portfolio_views") or {}).items():
            if not isinstance(view, dict):
                continue
            summary = view.get("portfolio") or {}
            summaries[pid] = {
                "market_value": summary.get("market_value"),
                "daily_pnl": summary.get("daily_pnl"),
                "net_equity": summary.get("net_equity"),
            }
    portfolios = []
    for entry in entries:
        config = entry["config"]
        item = {
            "portfolio_id": entry["portfolio_id"],
            "label": entry["label"],
            "editable": entry["editable"],
            "base_portfolio_id": entry["base_portfolio_id"],
            "as_of": config.get("as_of"),
            "position_count": len(entry["positions"]),
            "created_at": config.get("created_at"),
            "note": str(config.get("note") or ""),
            "history_start": str(config.get("history_start") or ""),
        }
        item.update(summaries.get(entry["portfolio_id"]) or {})
        portfolios.append(item)
    return {
        "ok": True,
        "generated_at": _iso_now(),
        "schema_version": "multi-portfolio.api.v1",
        "main_portfolio_id": MAIN_PORTFOLIO_ID,
        "portfolios": portfolios,
        "errors": [f"附加组合配置暂不可用：{extras_error}"] if extras_error else [],
    }


def _rank_map(rows: list[dict[str, Any]], key: str) -> dict[str, int | None]:
    """Rank portfolios best-first by a numeric key; missing values get no rank."""

    ordered = sorted(
        (row for row in rows if row.get(key) is not None),
        key=lambda row: row[key],
        reverse=True,
    )
    ranks = {row["portfolio_id"]: index + 1 for index, row in enumerate(ordered)}
    return {row["portfolio_id"]: ranks.get(row["portfolio_id"]) for row in rows}


def get_portfolio_compare_payload(force: bool = False) -> dict[str, Any]:
    """Rank and compare every portfolio on the same shared quote snapshot."""

    quotes = get_quotes_payload(force=force)
    entries = _all_portfolio_entries()
    views = quotes.get("portfolio_views") or {}
    with PERFORMANCE_LEDGER_LOCK:
        ledger = _read_performance_ledger()
    ledger_records = ledger.get("records") or {}

    rows: list[dict[str, Any]] = []
    for entry in entries:
        pid = entry["portfolio_id"]
        if pid == MAIN_PORTFOLIO_ID:
            summary = quotes.get("portfolio") or {}
            positions = quotes.get("positions") or []
        else:
            view = views.get(pid) or {}
            summary = view.get("portfolio") or {}
            positions = view.get("positions") or []
        market_value = _float(summary.get("market_value"))
        daily_pnl = _float(summary.get("daily_pnl"))
        previous_value = (
            market_value - daily_pnl
            if market_value is not None and daily_pnl is not None
            else None
        )
        daily_pnl_pct = (
            round(daily_pnl / previous_value * 100, 4)
            if daily_pnl is not None and previous_value
            else None
        )
        top = positions[0] if positions else {}
        cumulative_pnl = None
        history_start = ""
        cumulative_series: list[dict[str, Any]] = []
        cumulative_partial = False
        configured_start = str(entry["config"].get("history_start") or "").strip()
        try:
            cached_history = _daily_pnl_store_for(pid).peek(
                entry["config"],
                start=configured_start or DAILY_PNL_HISTORY_DEFAULT_START_DATE,
                # 跨午夜后缓存尾部日期自然落后一天；比较视图接受旧尾部的缓存，
                # 避免每天 0 点到下次刷新之间趋势图整体消失。
                allow_older_end=True,
            )
        except Exception:
            cached_history = None
        if cached_history:
            records = cached_history.get("records") or []
            for record in records:
                if not isinstance(record, dict):
                    continue
                trade_date = str(record.get("trade_date") or "")
                value = _float(record.get("cumulative_pnl_cny"))
                if value is None:
                    value = _float(record.get("covered_cumulative_pnl_cny"))
                    if value is not None:
                        cumulative_partial = True
                if value is None:
                    value = _float(record.get("cumulative_pnl"))
                if not trade_date or value is None:
                    continue
                cumulative_series.append({"date": trade_date, "cumulative_pnl": value})
            if records:
                history_start = str(records[0].get("trade_date") or "")
            if cumulative_series:
                cumulative_pnl = cumulative_series[-1]["cumulative_pnl"]
        series = [item for item in (ledger_records.get(pid) or []) if isinstance(item, dict)]
        record_return_pct = None
        record_days = len(series)
        if len(series) >= 2:
            first_equity = _float(series[0].get("net_equity"))
            last_equity = _float(series[-1].get("net_equity"))
            if first_equity and last_equity is not None:
                record_return_pct = round((last_equity / first_equity - 1) * 100, 4)
        rows.append(
            {
                "portfolio_id": pid,
                "label": entry["label"],
                "editable": entry["editable"],
                "as_of": entry["config"].get("as_of"),
                "position_count": int(summary.get("position_count") or len(positions)),
                "quote_coverage_count": summary.get("quote_coverage_count"),
                "market_value": market_value,
                "daily_pnl": daily_pnl,
                "daily_pnl_pct": daily_pnl_pct,
                "net_equity": _float(summary.get("net_equity")),
                "cash": _float(summary.get("cash")),
                "debt": _float(summary.get("debt")),
                "gross_exposure_pct": _float(summary.get("gross_exposure_pct")),
                "top_position": (
                    {
                        "thscode": top.get("thscode"),
                        "name": top.get("name"),
                        "weight_pct_securities": top.get("weight_pct_securities"),
                    }
                    if top
                    else None
                ),
                "cumulative_pnl": cumulative_pnl,
                "cumulative_series": cumulative_series[-400:],
                "cumulative_partial": cumulative_partial,
                "history_start": history_start,
                "record_days": record_days,
                "record_return_pct": record_return_pct,
                "history": series[-60:],
            }
        )

    ranks_daily = _rank_map(rows, "daily_pnl_pct")
    ranks_cumulative = _rank_map(rows, "cumulative_pnl")
    ranks_record = _rank_map(rows, "record_return_pct")
    for row in rows:
        row["ranks"] = {
            "daily_pnl_pct": ranks_daily.get(row["portfolio_id"]),
            "cumulative_pnl": ranks_cumulative.get(row["portfolio_id"]),
            "record_return_pct": ranks_record.get(row["portfolio_id"]),
        }

    membership: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        for position in entry["positions"]:
            key = str(position.get("thscode") or "").upper()
            if not key:
                continue
            membership.setdefault(key, []).append(
                {
                    "portfolio_id": entry["portfolio_id"],
                    "label": entry["label"],
                    "quantity": position.get("quantity"),
                    "name": position.get("name"),
                }
            )
    shared_positions = [
        {
            "thscode": key,
            "name": holders[0].get("name") or key,
            "holders": holders,
        }
        for key, holders in sorted(membership.items())
        if len(holders) > 1
    ]

    return {
        "ok": True,
        "generated_at": _iso_now(),
        "schema_version": "multi-portfolio.compare.v1",
        "quote_time": (quotes.get("market_status") or {}).get("quote_time") or quotes.get("generated_at"),
        "portfolios": rows,
        "shared_positions": shared_positions,
        "note": "各组合共用同一次行情快照；累计盈亏来自每日静态回算缓存，记录收益来自本机逐日台账，均非券商实际账单。",
    }


def _enrich_draft_positions(positions: list[dict[str, Any]]) -> None:
    """Fill missing names/snapshot prices from one shared quote fetch."""

    symbols = []
    for position in positions:
        try:
            symbols.append(_symbol_from_thscode(position["thscode"]))
        except ValueError:
            continue
    if not symbols:
        return
    try:
        quotes = _fetch_tencent(list(dict.fromkeys(symbols)))
    except Exception:
        # 腾讯行情失败 → 新浪 A股备援补齐名称与现价（估值字段缺失不影响草稿）。
        try:
            quotes = _fetch_sina_cn_quotes(list(dict.fromkeys(symbols)))
        except Exception:
            return
    for position in positions:
        try:
            symbol = _symbol_from_thscode(position["thscode"])
        except ValueError:
            continue
        quote = quotes.get(symbol)
        if not quote:
            continue
        if not position.get("name") or position.get("name") == position["thscode"]:
            name = str(quote.get("source_name") or "").strip()
            if name:
                position["name"] = name[:30]
        if position.get("snapshot_price") is None and quote.get("price"):
            position["snapshot_price"] = round(float(quote["price"]), 4)


def create_user_portfolio(draft: dict[str, Any]) -> dict[str, Any]:
    draft = dict(draft or {})
    if "positions" not in draft or not draft.get("positions"):
        base_id = str(draft.get("base_portfolio_id") or MAIN_PORTFOLIO_ID)
        base = _resolve_portfolio_entry(base_id)
        if base is None:
            raise PortfolioNotFound(f"基准组合不存在：{base_id}")
        cloned_positions = [
            {
                "thscode": p.get("thscode"),
                "name": p.get("name"),
                "quantity": p.get("quantity"),
                "instrument_type": p.get("instrument_type"),
                "industry": p.get("industry"),
                "theme": p.get("theme"),
                "snapshot_price": p.get("snapshot_price"),
                "daily_limit_pct": p.get("daily_limit_pct"),
            }
            for p in base["positions"]
        ]
        draft["positions"] = cloned_positions
        if "cash" not in draft or "debt" not in draft:
            cash, debt = 0.0, 0.0
            for account in base["config"].get("accounts") or []:
                cash += float(account.get("cash") or 0)
                debt += float(account.get("debt") or 0)
            draft.setdefault("cash", cash)
            draft.setdefault("debt", debt)
    _enrich_draft_positions(draft["positions"])
    config = create_portfolio(draft, path=EXTRA_PORTFOLIOS_PATH)
    _after_registry_change()
    return get_portfolios_payload() | {"created": config["portfolio_id"], "created_label": config["label"]}


def update_user_portfolio(portfolio_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    if portfolio_id == MAIN_PORTFOLIO_ID:
        raise PortfolioRegistryError("主组合来自券商快照配置，不能在页面上修改；可基于它创建新组合")
    draft = dict(draft or {})
    if isinstance(draft.get("positions"), list):
        _enrich_draft_positions(draft["positions"])
    update_portfolio(portfolio_id, draft, path=EXTRA_PORTFOLIOS_PATH)
    _after_registry_change()
    return get_portfolios_payload() | {"updated": portfolio_id}


def delete_user_portfolio(portfolio_id: str) -> dict[str, Any]:
    if portfolio_id == MAIN_PORTFOLIO_ID:
        raise PortfolioRegistryError("主组合不能删除")
    removed = delete_portfolio(portfolio_id, path=EXTRA_PORTFOLIOS_PATH)
    for path in (
        DAILY_PNL_HISTORY_CACHE_PATH.with_name(f"daily-pnl-history-{portfolio_id}.json"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    with PORTFOLIO_STORES_LOCK:
        EXTRA_INTRADAY_STORES.pop(portfolio_id, None)
        EXTRA_DAILY_PNL_STORES.pop(portfolio_id, None)
    with PERFORMANCE_LEDGER_LOCK:
        document = _read_performance_ledger()
        if document["records"].pop(portfolio_id, None) is not None:
            try:
                _atomic_write_json(PORTFOLIO_PERFORMANCE_PATH, document)
            except Exception:
                pass
    _after_registry_change()
    return get_portfolios_payload() | {
        "deleted": portfolio_id,
        "deleted_label": removed.get("label"),
    }


def _after_registry_change() -> None:
    """Reload portfolios and rebuild the shared quote snapshot once.

    An explicit user action (create/update/delete) justifies one synchronous
    rebuild even outside polling hours; the last-success disk cache stays the
    fallback when the rebuild cannot complete.
    """

    _refresh_extra_portfolios_if_changed(force=True)
    try:
        fresh = _build_quotes_payload()
        _record_portfolio_performance(fresh)
        _publish_quote_payload(fresh)
        return
    except Exception as exc:
        with QUOTE_LOCK:
            QUOTE_CACHE["payload"] = None
            QUOTE_CACHE["monotonic"] = 0.0
            QUOTE_CACHE["last_error"] = f"组合配置已更新，行情重建失败：{type(exc).__name__}: {exc}"
        ensure_quote_refresh(force=True)


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "LDLPortfolioDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        public_payload = _redact_public_payload(payload)
        if public_payload != payload:
            public_payload["response_redaction"] = {
                "applied": True,
                "integrity": "FAIL_CLOSED_REDACTED_AFTER_BINDING",
                "message": "响应中检测到凭据形态文本，已脱敏；原内容哈希不可由本响应重算。",
            }
            if "decision_ready" in public_payload:
                public_payload["decision_ready"] = False
            quality = public_payload.get("data_quality")
            if isinstance(quality, dict):
                quality["status"] = "CRITICAL"
                errors = quality.get("errors")
                if not isinstance(errors, list):
                    errors = []
                quality["errors"] = list(
                    dict.fromkeys([*errors, "HTTP_RESPONSE_CREDENTIAL_REDACTED"])
                )
        body = json.dumps(public_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely cancel superseded polling requests.  The
            # response is already unusable at that point, so do not turn a
            # normal client disconnect into a noisy server traceback.
            return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'",
        )

    def end_headers(self) -> None:
        # 带 ?v= 版本号的静态资源（如 /styles.css?v=…）同样按静态文件处理。
        static_path = self.path.split("?", 1)[0].split("#", 1)[0]
        if static_path.endswith((".html", ".js", ".css")) or self.path in ("/", ""):
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
        super().end_headers()

    def _is_local_request(self) -> bool:
        try:
            client_is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            client_is_loopback = False
        host = str(self.headers.get("Host") or "").strip().lower()
        if host.startswith("[") and "]" in host:
            hostname = host[1 : host.index("]")]
        elif host.count(":") == 1:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        return client_is_loopback and hostname.rstrip(".") in {"127.0.0.1", "localhost"}

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        # HEAD would otherwise inherit SimpleHTTPRequestHandler's static-file
        # path without the loopback gate that do_GET enforces.  Apply the same
        # access policy before any static handler runs, while keeping HEAD
        # semantics (headers only, never a response body).
        if not self._is_local_request():
            try:
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self._security_headers()
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._is_local_request():
            self._send_json(
                {"ok": False, "error": "This dashboard accepts loopback requests only."},
                HTTPStatus.FORBIDDEN,
            )
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/quotes":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            portfolio_id = str((query.get("portfolio") or [""])[0])
            try:
                if portfolio_id:
                    payload = get_quotes_payload(force=force, portfolio_id=portfolio_id)
                else:
                    payload = get_quotes_payload(force=force)
            except PortfolioNotFound as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(payload, HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/personal-index":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            payload = get_personal_index_payload(force=force)
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok", True) else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/portfolios":
            self._send_json(get_portfolios_payload())
            return
        if parsed.path == "/api/portfolio-compare":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            try:
                payload = get_portfolio_compare_payload(force=force)
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": _redact_error(f"{type(exc).__name__}: {exc}")},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(payload, HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/news":
            query = parse_qs(parsed.query)
            code = str((query.get("code") or [""])[0])
            if code and not re.fullmatch(r"\d{6}", code):
                self._send_json({"ok": False, "error": "code must be a six-digit ticker"}, HTTPStatus.BAD_REQUEST)
                return
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            portfolio_id = str((query.get("portfolio") or [""])[0])
            try:
                if portfolio_id:
                    self._send_json(get_news_payload(code=code, force=force, portfolio_id=portfolio_id))
                else:
                    self._send_json(get_news_payload(code=code, force=force))
            except PortfolioNotFound as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/tomorrow-plans":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            self._send_json(get_tomorrow_plans_payload(force=force))
            return
        if parsed.path == "/api/market-news":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            self._send_json(get_market_news_payload(force=force))
            return
        if parsed.path == "/api/stock-chart":
            query = parse_qs(parsed.query)
            symbol = str((query.get("symbol") or [""])[0])
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            indicators_raw = str((query.get("indicators") or [""])[0])
            indicators: list[str] | None = None
            if indicators_raw:
                indicators = [
                    name.strip().lower()
                    for name in indicators_raw.split(",")
                    if name.strip()
                ]
            try:
                payload = get_stock_chart_payload(symbol, force=force, indicators=indicators)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error_code": "INVALID_SYMBOL", "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except StockChartSymbolNotHeld:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "SYMBOL_NOT_IN_PORTFOLIO",
                        "error": "symbol is not in the current portfolio",
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/technical-signals":
            query = parse_qs(parsed.query)
            symbol = str((query.get("symbol") or [""])[0])
            if not symbol:
                self._send_json(
                    {"ok": False, "error_code": "MISSING_SYMBOL", "error": "symbol is required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                payload = get_technical_signals_payload(symbol)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error_code": "INVALID_SYMBOL", "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except StockChartSymbolNotHeld:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "SYMBOL_NOT_IN_PORTFOLIO",
                        "error": "symbol is not in the current portfolio",
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/technical-signals-batch":
            query = parse_qs(parsed.query)
            symbols_raw = str((query.get("symbols") or [""])[0])
            symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
            self._send_json(get_technical_signals_batch_payload(symbols))
            return
        if parsed.path == "/api/school-signals":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            try:
                payload = get_school_signals_payload(force=force)
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error_code": "SCHOOL_SIGNALS_UNAVAILABLE",
                     "error": f"{type(exc).__name__}: {exc}"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/intraday-pnl/history":
            query = parse_qs(parsed.query)
            raw_date = str((query.get("date") or [""])[0])
            raw_portfolio = str((query.get("portfolio") or [""])[0])
            try:
                if raw_portfolio:
                    payload = get_intraday_pnl_history_payload(
                        raw_date or None,
                        portfolio_id=raw_portfolio,
                    )
                else:
                    payload = get_intraday_pnl_history_payload(raw_date or None)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/leading-indicators":
            self._send_json(get_leading_indicators_payload(), HTTPStatus.OK)
            return
        if parsed.path == "/api/derivatives-regime":
            payload = derivatives_regime_module.get_derivatives_regime()
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/intraday-pnl/dates":
            query = parse_qs(parsed.query)
            raw_portfolio = str((query.get("portfolio") or [""])[0])
            try:
                payload = get_intraday_pnl_dates_payload(raw_portfolio)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(payload, HTTPStatus.OK)
            return
        if parsed.path == "/api/paper-t":
            self._send_json(auto_paper_module.today_payload(), HTTPStatus.OK)
            return
        if parsed.path == "/api/paper-t/history":
            self._send_json(auto_paper_module.compare_payload(), HTTPStatus.OK)
            return
        if parsed.path == "/api/autopilot":
            self._send_json(auto_pilot_module.payload(), HTTPStatus.OK)
            return
        if parsed.path == "/api/autopilot/journal":
            query = parse_qs(parsed.query)
            try:
                limit = int(str((query.get("limit") or ["200"])[0]))
            except ValueError:
                limit = 200
            self._send_json(auto_pilot_module.journal_payload(limit=limit), HTTPStatus.OK)
            return
        if parsed.path == "/api/intraday-pnl":
            query = parse_qs(parsed.query)
            raw_date = str((query.get("date") or [""])[0])
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            try:
                if str((query.get("portfolio") or [""])[0]):
                    payload = get_intraday_pnl_payload(
                        raw_date or None,
                        refresh=force,
                        portfolio_id=str((query.get("portfolio") or [""])[0]),
                    )
                else:
                    payload = get_intraday_pnl_payload(raw_date or None, refresh=force)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                payload,
                HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path == "/api/daily-pnl-history":
            query = parse_qs(parsed.query)
            start = str((query.get("start") or [""])[0])
            force = str((query.get("refresh") or [""])[0]).lower() in (
                "1",
                "true",
                "yes",
            )
            try:
                if str((query.get("portfolio") or [""])[0]):
                    payload = get_daily_pnl_history_payload(
                        start or None,
                        refresh=force,
                        portfolio_id=str((query.get("portfolio") or [""])[0]),
                    )
                else:
                    payload = get_daily_pnl_history_payload(start or None, refresh=force)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "available": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except DailyPnlHistoryUnavailable:
                self._send_json(
                    {
                        "schema_version": "daily-pnl-history.api.v1",
                        "ok": False,
                        "available": False,
                        "error_code": "DAILY_PNL_HISTORY_UNAVAILABLE",
                        "error": "daily P&L history is temporarily unavailable",
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            except Exception:
                self._send_json(
                    {
                        "schema_version": "daily-pnl-history.api.v1",
                        "ok": False,
                        "available": False,
                        "error_code": "DAILY_PNL_HISTORY_UNAVAILABLE",
                        "error": "daily P&L history is temporarily unavailable",
                    },
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._send_json(payload, HTTPStatus.OK)
            return
        if parsed.path == "/api/risk":
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            portfolio_id = str((query.get("portfolio") or [""])[0])
            try:
                if portfolio_id:
                    self._send_json(get_risk_payload(force=force, portfolio_id=portfolio_id))
                else:
                    self._send_json(get_risk_payload(force=force))
            except PortfolioNotFound as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path in {
            "/api/decision-context",
            "/api/market-state",
            "/api/events",
            "/api/opportunities",
            "/api/rebalance",
        }:
            query = parse_qs(parsed.query)
            force = str((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
            context = get_decision_context(force=force)
            if parsed.path == "/api/decision-context":
                payload = context
            else:
                key = {
                    "/api/market-state": "market_state",
                    "/api/events": "events",
                    "/api/opportunities": "opportunities",
                    "/api/rebalance": "rebalance",
                }[parsed.path]
                payload = copy.deepcopy(context.get(key) or {})
                if isinstance(payload, dict):
                    payload.setdefault("decision_snapshot_id", context.get("snapshot_id"))
                    payload["decision_context_as_of"] = context.get("as_of") or {}
                    payload["decision_data_quality"] = context.get("data_quality") or {}
            self._send_json(payload)
            return
        if parsed.path == "/api/health":
            self._send_json(get_health_payload())
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read a small JSON request body; ``None`` signals a handled error."""

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 256 * 1024:
            self._send_json(
                {"ok": False, "error": "请求体必须是不超过 256KB 的 JSON"},
                HTTPStatus.BAD_REQUEST,
            )
            return None
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(value, dict):
            self._send_json({"ok": False, "error": "请求体必须是 JSON 对象"}, HTTPStatus.BAD_REQUEST)
            return None
        return value

    def _portfolio_write_guard(self) -> bool:
        if not self._is_local_request():
            self._send_json(
                {"ok": False, "error": "This dashboard accepts loopback requests only."},
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _handle_portfolio_write_error(self, exc: Exception) -> None:
        if isinstance(exc, PortfolioNotFound):
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        elif isinstance(exc, PortfolioRegistryError):
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        else:
            self._send_json(
                {"ok": False, "error": _redact_error(f"{type(exc).__name__}: {exc}")},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._portfolio_write_guard():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/intraday-pnl/backfill":
            body = self._read_json_body()
            if body is None:
                return
            try:
                payload = post_intraday_pnl_backfill(
                    str(body.get("portfolio") or body.get("portfolio_id") or ""),
                    body.get("dates") or [],
                    force=str(body.get("force") or "").lower() in ("1", "true", "yes"),
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": _redact_error(f"{type(exc).__name__}: {exc}")},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json(payload, HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path == "/api/paper-t/void":
            body = self._read_json_body()
            if body is None:
                return
            try:
                payload = auto_paper_module.void_session(
                    str(body.get("strategy") or ""), str(body.get("reason") or "")
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": _redact_error(f"{type(exc).__name__}: {exc}")},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json(payload, HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if parsed.path != "/api/portfolios":
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            payload = create_user_portfolio(body)
        except Exception as exc:
            self._handle_portfolio_write_error(exc)
            return
        self._send_json(payload, HTTPStatus.CREATED)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._portfolio_write_guard():
            return
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/portfolios/([A-Za-z0-9-]+)", parsed.path)
        if not match:
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            payload = update_user_portfolio(match.group(1), body)
        except Exception as exc:
            self._handle_portfolio_write_error(exc)
            return
        self._send_json(payload)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._portfolio_write_guard():
            return
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/portfolios/([A-Za-z0-9-]+)", parsed.path)
        if not match:
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = delete_user_portfolio(match.group(1))
        except Exception as exc:
            self._handle_portfolio_write_error(exc)
            return
        self._send_json(payload)

    def log_message(self, format_: str, *args: Any) -> None:
        message = format_ % args
        if "/api/quotes" not in message:
            print(f"[{_now().strftime('%H:%M:%S')}] {self.client_address[0]} {message}")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global BACKGROUND_REFRESH_SERVICE

    parser = argparse.ArgumentParser(description="Serve the local portfolio dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765)")
    parser.add_argument("--no-news-warmup", action="store_true", help="do not refresh news at startup")
    parser.add_argument(
        "--no-background-refresh",
        action="store_true",
        help="disable client-independent maintenance (diagnostics only)",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("--host must be a loopback address: 127.0.0.1 or localhost")
    # 真实服务启动后注入依赖并打开自动模拟交易（worker/调度器/接口三路驱动）。
    _configure_auto_paper()
    auto_paper_module.enable()
    # 自动领航模拟盘：镜像实盘的波段级自动决策账户（每日收盘前决策窗口调仓留痕）。
    _configure_auto_pilot()
    auto_pilot_module.enable()
    # 启动即做一次初始化尝试（非交易日也先镜像建仓，首个交易日即开始对比）。
    threading.Thread(
        target=lambda: auto_pilot_module.run_cycle(),
        name="auto-pilot-warmup",
        daemon=True,
    ).start()
    # 后台预热自适应阈值缓存（首扫需校验十几天的历史账本），避免首个页面请求超时。
    threading.Thread(
        target=lambda: auto_paper_module.run_all_strategies(),
        name="auto-paper-warmup",
        daemon=True,
    ).start()
    if not STATIC_DIR.is_dir():
        raise SystemExit(f"Dashboard assets missing: {STATIC_DIR}")
    server = DashboardServer((args.host, args.port), DashboardHandler)
    background = None if args.no_background_refresh else BackgroundRefreshService()
    try:
        _load_persisted_news()
        _load_persisted_market_news()
        if not _load_persisted_quote_cache():
            _recover_quote_cache_from_local_snapshots()
        ensure_quote_refresh()
        if not args.no_news_warmup:
            ensure_news_refresh()
            ensure_market_news_refresh()
        if background is not None:
            background.start(immediate=False)
            BACKGROUND_REFRESH_SERVICE = background
        print(f"股票实时看板：http://{args.host}:{args.port}")
        print("仅绑定本机；按 Ctrl-C 停止。")
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if background is not None:
            background.stop()
        if BACKGROUND_REFRESH_SERVICE is background:
            BACKGROUND_REFRESH_SERVICE = None
        server.server_close()


if __name__ == "__main__":
    main()
