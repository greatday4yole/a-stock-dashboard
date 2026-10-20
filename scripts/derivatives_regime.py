#!/usr/bin/env python3
"""衍生品风险仪表盘：把「10-15 市场研判」里的说法拆成可检验的因子。

这个模块做三件事：

1. **口径升级**——把原文里"期权贵/便宜""Vega 很低就是 VIX""skew 偏空"这类
   不可比的口径，换成行业标准且可跨时点比较的读数：隐含波动率历史百分位、
   25Δ 风险逆转（RR）/ 25Δ 蝶式（BF）、期限结构 contango/backwardation。
2. **逐条核查**——原文每条可证伪的说法都配一个可自动取数核对的判定函数，
   核不上的就明说"待验证"，不替它圆场。
3. **历史留档**——每个交易日把衍生品读数落一份快照，逐步积累出 A 股侧的
   历史百分位（当前 IV/PCR 的"贵不贵"只有相对于自身历史才有意义）。

数据源：东方财富 ETF 期权行情（PCR/IV）、新浪 VIX 实时、腾讯 VIX 与指数日K、
腾讯市场广度。各源独立容错，任一源失败只标记该块不可用。
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

try:  # 包式导入（被 dashboard_server 以 scripts.derivatives_regime 引用）
    from scripts import options_sentiment as options_sentiment_module
except ModuleNotFoundError:  # 直跑 scripts/derivatives_regime.py
    import options_sentiment as options_sentiment_module  # type: ignore[no-redef]

# ---------------------------------------------------------------- 数据源常量

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_TENCENT_NEWFQ_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
_TENCENT_USFQ_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/usfqkline/get",
)
_TENCENT_KLINE_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
    "https://ifzq.gtimg.cn/appstock/app/kline/kline",
)

_SINA_HQ = "https://hq.sinajs.cn/list="

_CN_INDEXES = {
    "sh000001": "上证指数",
    "sh000300": "沪深300",
    "sz399001": "深证成指",
}

_HISTORY_DIR = Path("data/runtime")
_HISTORY_FILE = _HISTORY_DIR / "derivatives-regime-history.json"
_HISTORY_SCHEMA = "derivatives-regime-history.v1"
_HISTORY_MAX_DAYS = 500

_CACHE_TTL_SEC = 300.0
# 期权链拉取的时间预算（秒）。超时按「暂无数据」处理，先出其余读数。
_OPTIONS_FETCH_BUDGET_SEC = 20.0

_session = requests.Session()
_session.headers.update({"User-Agent": _UA})
_host_lock = threading.Lock()
_host_sticky: dict[str, int] = {}


# ------------------------------------------------------------------ HTTP 工具


def _curl_get_text(url: str, timeout: float = 12.0, referer: str = "") -> str:
    """curl 取文本：新浪 hq 需要 Referer，且部分网络环境会掐掉 requests 的 TLS 指纹。"""
    command = [
        "/usr/bin/curl", "--silent", "--show-error", "--fail", "--compressed",
        "--max-time", str(int(timeout)),
        "-H", f"User-Agent: {_UA}",
    ]
    if referer:
        command += ["-H", f"Referer: {referer}"]
    command.append(url)
    result = subprocess.run(command, capture_output=True, check=True, timeout=timeout + 4.0)
    return result.stdout.decode("gbk", errors="replace")


def _tencent_json(purpose: str, hosts: tuple[str, ...], params: dict[str, Any]) -> dict[str, Any]:
    """腾讯接口轮换主机：WAF 会对单主机/单端点做整站拦截，换一台即可。"""
    with _host_lock:
        start = _host_sticky.get(purpose, 0)
    last_error: Optional[Exception] = None
    for offset in range(len(hosts)):
        index = (start + offset) % len(hosts)
        try:
            response = _session.get(
                hosts[index], params=params, timeout=(2.5, 8.0),
                headers={"Referer": "https://gu.qq.com/"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("腾讯接口返回的不是 JSON 对象")
            with _host_lock:
                _host_sticky[purpose] = index
            return payload
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"腾讯接口主机全部失败: {type(last_error).__name__}: {last_error}")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _percentile_of(value: float, sample: list[float]) -> Optional[float]:
    """value 在样本中的百分位（0-100），并集含并列值。"""
    if not sample:
        return None
    below = sum(1 for x in sample if x < value)
    equal = sum(1 for x in sample if x == value)
    return round((below + 0.5 * equal) / len(sample) * 100, 1)


# ------------------------------------------------------------------ 数据抓取


def fetch_vix_quote() -> dict[str, Any]:
    """新浪 VIX 实时报价（恐慌指数）。

    字段顺序实测为：名称,最新,涨跌,涨跌幅,买,卖,日期,时间,开盘,昨收,最高,最低。
    昨收用「最新 - 涨跌」反推，避免依赖字段顺序的隐含假设。
    """
    text = _curl_get_text(
        f"{_SINA_HQ}znb_VIX", referer="https://finance.sina.com.cn"
    )
    body = text.split('="', 1)[-1].rstrip('";\n ')
    parts = body.split(",")
    if len(parts) < 4:
        raise RuntimeError(f"VIX 报价字段不足: {body[:120]}")
    last = _to_float(parts[1])
    change = _to_float(parts[2])
    if last is None:
        raise RuntimeError(f"VIX 最新价不可解析: {body[:120]}")
    prev_close = (last - change) if (change is not None) else None
    return {
        "name": parts[0],
        "last": last,
        "change": change,
        "change_pct": _to_float(parts[3]),
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "quote_date": parts[6].strip() if len(parts) > 6 else "",
        "quote_time": parts[7].strip() if len(parts) > 7 else "",
        "source": "新浪财经",
    }


def fetch_vix_history(count: int = 500) -> dict[str, Any]:
    """VIX 日线历史（腾讯）。用于把 VIX 绝对水平换成历史百分位。"""
    payload = _tencent_json(
        "vix_daily", _TENCENT_USFQ_HOSTS, {"param": f"usVIX,day,,,{int(count)},qfq"}
    )
    body = (payload.get("data") or {}).get("usVIX") or {}
    rows = body.get("qfqday") or body.get("day") or []
    series: list[dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 3:
            continue
        close = _to_float(raw[2])
        if close is None:
            continue
        series.append({"date": str(raw[0]), "close": close})
    if not series:
        raise RuntimeError("VIX 日线历史为空")
    return {"series": series, "source": "腾讯财经"}


def fetch_index_history(symbol: str, days: int = 120) -> dict[str, Any]:
    """A 股指数日K（腾讯 newfqkline）。用于事后回溯点位类断言。"""
    # end 取"今天"：腾讯对**指数**类品种在 end=今天 时会多返回一根当日实时 bar
    # （实测 2026-10-20 盘中，上证 last=2026-10-20 且成交量在累积），
    # 因此盘中就能拿到当日 bar。个股的前复权序列不含当日 bar，由看板侧用
    # 实时行情合并（dashboard_server._merge_live_daily_bar）。
    end = date.today()
    # 区间长度仍是 days*2 个日历日，拉取根数（days*2）不变。
    start = date.fromordinal(end.toordinal() - int(days) * 2)
    payload = _tencent_json(
        f"index_daily_{symbol}",
        _TENCENT_NEWFQ_HOSTS,
        {
            "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},{int(days) * 2},qfq"
        },
    )
    body = (payload.get("data") or {}).get(symbol) or {}
    rows = body.get("qfqday") or body.get("day") or []
    bars: list[dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 5:
            continue
        close = _to_float(raw[2])
        high = _to_float(raw[3])
        low = _to_float(raw[4])
        if close is None:
            continue
        bars.append({
            "date": str(raw[0]),
            "close": close,
            "high": high,
            "low": low,
        })
    if not bars:
        raise RuntimeError(f"{symbol} 日线为空")
    return {"symbol": symbol, "bars": bars, "source": "腾讯财经"}


def fetch_breadth() -> dict[str, Any]:
    """沪深两市市场广度（腾讯 zhishu 字段）。"""
    total_up = total_flat = total_down = total_count = 0
    for symbol in ("sh000001", "sz399001"):
        payload = _tencent_json(
            f"breadth_{symbol}", _TENCENT_KLINE_HOSTS, {"param": f"{symbol},day,,,5"}
        )
        body = (payload.get("data") or {}).get(symbol) or {}
        rank = ((body.get("qt") or {}).get("zhishu") or [])
        if len(rank) < 6:
            raise RuntimeError(f"{symbol} 广度字段缺失")
        total_up += int(rank[2])
        total_flat += int(rank[3])
        total_down += int(rank[4])
        total_count += int(rank[5])
    if total_count <= 0:
        raise RuntimeError("市场广度合计为 0")
    return {
        "up": total_up,
        "flat": total_flat,
        "down": total_down,
        "total": total_count,
        "up_ratio_pct": round(total_up / total_count * 100, 2),
        "advance_decline": round((total_up - total_down) / total_down, 3) if total_down else None,
        "source": "腾讯财经",
    }


# ------------------------------------------------------------- 历史快照留档


class RegimeHistoryStore:
    """每日一份衍生品读数快照，供历史百分位使用。

    百分位必须相对自身历史才有意义——绝对 IV 数字在不同标的、不同年份之间
    不可比。当前 VIX 百分位可以直接用腾讯的 500 日历史算出；A 股 IV/PCR 的
    百分位需要靠这里逐日积累，所以面板会如实标注样本天数。
    """

    _lock = threading.RLock()

    def __init__(self, path: Path = _HISTORY_FILE) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {"schema": _HISTORY_SCHEMA, "days": {}}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"schema": _HISTORY_SCHEMA, "days": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("days"), dict):
            return {"schema": _HISTORY_SCHEMA, "days": {}}
        return payload

    def record(self, trade_day: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            payload = self.load()
            payload["schema"] = _HISTORY_SCHEMA
            days = payload.setdefault("days", {})
            days[trade_day] = snapshot
            if len(days) > _HISTORY_MAX_DAYS:
                for key in sorted(days)[:-_HISTORY_MAX_DAYS]:
                    days.pop(key, None)
            payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def series(self, metric: str, key: str) -> list[tuple[str, float]]:
        """返回 (日期, 读数) 序列，按日期升序。"""
        days = self.load().get("days") or {}
        out: list[tuple[str, float]] = []
        for trade_day in sorted(days):
            value = (days.get(trade_day) or {}).get(metric) or {}
            raw = value.get(key) if isinstance(value, dict) else None
            number = _to_float(raw)
            if number is not None:
                out.append((trade_day, number))
        return out

    def sample_days(self) -> int:
        return len(self.load().get("days") or {})


# --------------------------------------------------------- 因子科学性审查表
#
# verdict 取值：
#   "ok"          口径本身科学，可直接使用
#   "recalibrate" 因子方向对，但原文口径不可比，必须换口径才能用
#   "wrong"       概念性错误，按原文理解会得出错误结论
#   "principle"   结构性金融事实（由定义推导，非统计量）
#   "backtest"    主观判断，只能事后回溯，不能量化建模

FACTOR_REVIEW: list[dict[str, Any]] = [
    {
        "factor": "IV 绝对水平",
        "claim": "「漂亮国现在的期权非常贵，A股期权不贵」",
        "verdict": "recalibrate",
        "rating": "需换口径",
        "ours": "IV 历史百分位（相对自身分布）",
        "why": "IV 的绝对数值不可跨市场、跨标的、跨年份比较——美股 20% 可能算便宜，A股 20% 可能算贵。"
               "唯一可比的是「当前值在自身历史里的位置」。美股侧已用 VIX 500 日历史算出百分位；"
               "A股侧靠每日快照逐步积累。",
    },
    {
        "factor": "Vega 与 VIX",
        "claim": "「Vega 变化处在一个非常低的低位，就是 VIX」",
        "verdict": "wrong",
        "rating": "概念混淆",
        "ours": "Vega 留在希腊字母栏，VIX 独立成项",
        "why": "Vega 是期权希腊字母，衡量期权价格对隐含波动率变化的敏感度；"
               "VIX 是由标普 500 期权算出的波动率指数，是隐含波动率本身的市场度量。"
               "两者相关但不同物，把 Vega 说成 VIX 会让「Vega 低→期权便宜」的推论失去支点。",
    },
    {
        "factor": "VIX 的用途",
        "claim": "「VIX 是判断期权贵不贵的，不是判断股市有没有风险」",
        "verdict": "recalibrate",
        "rating": "部分正确",
        "ours": "同时给水平、百分位与近期变动",
        "why": "VIX 高确实意味着期权贵（隐含波动率被推高），但它同时也是市场对未来波动的定价，"
               "两种用途不矛盾。面板把 VIX 当作「保险价格」读：既贵不贵，也贵得是否异常。",
    },
    {
        "factor": "偏斜（skew）",
        "claim": "「put 贵就是偏空、恐慌」",
        "verdict": "recalibrate",
        "rating": "口径粗暴",
        "ours": "25Δ 风险逆转 RR / 25Δ 蝶式 BF（恒定期限）",
        "why": "原文隐含的「取最近一档虚值 put 与 call 相减」不可靠：行权价是离散网格，"
               "跨档差值不可比；近月深虚值合约时间价值趋零，IV 反解对报价微扰极敏感"
               "（实测偏差可达 40 个百分点以上）。行业标准用 delta 定位行权价，并剔除 vega 过低的合约。",
    },
    {
        "factor": "期限结构",
        "claim": "（原文未提及）",
        "verdict": "ok",
        "rating": "最强信号·已补入",
        "ours": "近月 vs 次月 IV 差 + contango/backwardation 判定",
        "why": "期权口径里最可靠的压力信号：近月 IV 高于次月（backwardation）表示避险需求集中在近端，"
               "比 IV 绝对水平更早反映风险偏好切换。原文完全没提这一项，是最大的遗漏。",
    },
    {
        "factor": "PCR",
        "claim": "「PCR 反映市场情绪」",
        "verdict": "recalibrate",
        "rating": "弱信号·需分解",
        "ours": "持仓 PCR / 成交 PCR / 持仓变化比 三项分列",
        "why": "PCR 的绝对水平在 A 股（散户结构）与美股不可比，且与短期方向的相关性弱、"
               "更接近中长期情绪温度计。持仓量变化比（认沽净增 / 认购净增）比水平值更早反映资金流向。",
    },
    {
        "factor": "市场广度",
        "claim": "「整个市场宽广度已经废掉了，没法看」",
        "verdict": "ok",
        "rating": "有效因子·已量化",
        "ours": "上涨/平盘/下跌家数 + 涨跌家数比 + 上涨占比",
        "why": "广度是有效的市场内部结构因子，「废掉」应翻译成可读的数值："
               "上涨家数占比、涨跌家数比，以及连续几日的趋势。",
    },
    {
        "factor": "点位断言",
        "claim": "「3741 肯定是守不住，肯定是要破」",
        "verdict": "backtest",
        "rating": "不可量化·留档回溯",
        "ours": "自动回溯：破/守状态、盘中触及、收盘站上天数",
        "why": "「肯定」是绝对化表述，点位本身不构成因子。可检验的只有事后结果："
               "此后是否收在该位之下、盘中是否反复触及、该位后来变成了支撑还是阻力。",
    },
    {
        "factor": "期权 vs 期货风险",
        "claim": "「买认沽风险可控，不会被反弹毛刺打掉；期货会被打掉」",
        "verdict": "principle",
        "rating": "结构性事实",
        "ours": "不设数值指标，在原理栏标注",
        "why": "由合约结构推导，不是统计量：买方的最大损失是已付权利金（有界），"
               "期货空头的损失随标的上行线性扩大（无界），故期货更容易被一次性反弹触发止损。",
    },
    {
        "factor": "杠杆的本质",
        "claim": "「加杠杆加的是风险」",
        "verdict": "principle",
        "rating": "结构性事实",
        "ours": "不设数值指标，在原理栏标注",
        "why": "杠杆同时放大盈亏，盈利后加仓放大的是下行风险。属定义层面的事实，不需要统计验证。",
    },
    {
        "factor": "卖方收 Theta",
        "claim": "「横盘就做期权卖方，赚时间价值」",
        "verdict": "recalibrate",
        "rating": "机制对·风险被略过",
        "ours": "期限结构栏标注 + 原理栏风险提示",
        "why": "Theta 衰减机制正确，卖方确实赚时间价值。但卖方是负凸性持仓："
               "横盘时稳定收租，一旦跳空就是尾部大亏。只说「Theta 收租」会低估这条策略的风险特征。",
    },
    {
        "factor": "裸卖风险",
        "claim": "「不是 naked short 风险其实很低，不会穿仓」",
        "verdict": "recalibrate",
        "rating": "需区分买卖方",
        "ours": "按买方/卖方/非裸卖三档分别标注",
        "why": "「不会穿仓」只对买方成立。卖方（含裸卖）风险不低，naked short 亏损无上界；"
               "非裸卖（有担保）才把风险约束在权利金范围内。原文结论不错但适用条件需要写明。",
    },
    {
        "factor": "顺势做空/做多",
        "claim": "「跌的时候做空，多的时候去做，怎么着都能赚」",
        "verdict": "recalibrate",
        "rating": "条件性成立",
        "ours": "不设指标，列为条件性经验",
        "why": "趋势跟随确实在单边行情中有效，但原文漏了成本与震荡期损耗："
               "做空有负 carry（融券费/期货基差），且横盘期反复止损会侵蚀账户。",
    },
]


# ------------------------------------------------------------- 观点逐条核查

CLAIM_SOURCE = {
    "date": "2026-10-15",
    "title": "市场研判风险提示（转录）",
    "note": "以下每条都配了可自动取数核对的判定，核不上的标为待验证，不替它圆场。",
}

# 10-15 发言当天上证收盘（腾讯日K回溯核对）
_SPEECH_DATE = "2026-10-15"
_LEVEL_CLAIM = 3741.0


def _pct_change(new: float, old: float) -> float:
    return round((new - old) / old * 100, 2) if old else 0.0


def _check_market_direction(ctx: dict[str, Any]) -> dict[str, Any]:
    """核查「市场继续往下走，而且有加速态势」。

    基准取发言**前**一交易日收盘：原话是盘前录制，发言时当天收盘价还没出来，
    所以判断"往下走"的起点应是 10-14 收盘，而不是发言当天收盘。
    """
    bars = (ctx.get("sse") or {}).get("bars") or []
    before = [b for b in bars if b["date"] < _SPEECH_DATE]
    after = [b for b in bars if b["date"] >= _SPEECH_DATE]
    if not before or not after:
        return {"status": "pending", "text": "指数日K不足以覆盖发言日之后，暂无法核查。"}
    base = before[-1]["close"]
    last = after[-1]["close"]
    change = _pct_change(last, base)
    # 「加速」的可操作定义：发言后日均振幅是否超过发言前的日均振幅
    pre_ranges = [
        round((b["high"] - b["low"]) / b["low"] * 100, 2)
        for b in before[-10:] if b.get("high") and b.get("low") and b["low"]
    ]
    post_ranges = [
        round((b["high"] - b["low"]) / b["low"] * 100, 2)
        for b in after if b.get("high") and b.get("low") and b["low"]
    ]
    pre_avg = round(sum(pre_ranges) / len(pre_ranges), 2) if pre_ranges else None
    post_avg = round(sum(post_ranges) / len(post_ranges), 2) if post_ranges else None
    accelerated = bool(pre_avg and post_avg and post_avg > pre_avg)
    if change < 0 and accelerated:
        status = "confirmed"
    elif change < 0:
        status = "partial"
    else:
        status = "refuted"
    return {
        "status": status,
        "text": (
            f"发言为盘前录制，基准取发言前一交易日（{before[-1]['date']}）收盘 {base:.2f}；"
            f"至 {after[-1]['date']} 收 {last:.2f}，区间 {change:+.2f}%。"
            f"日均振幅 发言前10日 {pre_avg}% → 发言后 {post_avg}%"
            f"（{'确有放大' if accelerated else '未见放大'}）。"
        ),
        "metrics": {
            "base_close": base, "base_date": before[-1]["date"],
            "last_close": last, "last_date": after[-1]["date"], "change_pct": change,
            "pre_range_avg_pct": pre_avg, "post_range_avg_pct": post_avg,
        },
    }


def _check_level_3741(ctx: dict[str, Any]) -> dict[str, Any]:
    """核查「3741 肯定守不住」。"""
    bars = (ctx.get("sse") or {}).get("bars") or []
    after = [b for b in bars if b["date"] >= _SPEECH_DATE]
    if not after:
        return {"status": "pending", "text": "缺少发言日之后的日K，暂无法核查。"}
    closes_below = [b for b in after if b["close"] < _LEVEL_CLAIM]
    touched = [b for b in after if (b.get("low") or 0) <= _LEVEL_CLAIM <= (b.get("high") or 0)]
    recross = [b for b in after if b["close"] >= _LEVEL_CLAIM]
    broke = [b for b in after if b["date"] > _SPEECH_DATE and b["close"] < _LEVEL_CLAIM]
    if not broke:
        status = "refuted"
    elif recross:
        status = "partial"
    else:
        status = "confirmed"
    return {
        "status": status,
        "text": (
            f"自发言日起 {len(after)} 个交易日中，{len(closes_below)} 日收盘低于 {_LEVEL_CLAIM:.0f}"
            f"（其中 {len(broke)} 日为发言后确认跌破）；期间 {len(touched)} 日盘中触及该位，"
            f"{len(recross)} 日收盘重新站上。"
            + ("该位此后表现为阻力而非支撑。" if recross else "该位已被跌破且未收复。")
        ),
        "metrics": {
            "level": _LEVEL_CLAIM, "days_below": len(closes_below),
            "days_touched": len(touched), "days_recrossed": len(recross),
            "days_broken": len(broke), "total_days": len(after),
        },
    }


def _check_breadth(ctx: dict[str, Any]) -> dict[str, Any]:
    """核查「市场宽广度已经废掉了」。"""
    breadth = ctx.get("breadth") or {}
    if not breadth.get("total"):
        return {"status": "pending", "text": "市场广度数据暂不可用，无法核查。"}
    up_ratio = breadth["up_ratio_pct"]
    ad = breadth.get("advance_decline")
    if up_ratio is not None and up_ratio < 35:
        status = "confirmed"
    elif up_ratio is not None and up_ratio < 50:
        status = "partial"
    else:
        status = "refuted"
    return {
        "status": status,
        "text": (
            f"沪深两市 {breadth['total']} 只个股中上涨 {breadth['up']} 家（{up_ratio}%）、"
            f"平盘 {breadth['flat']} 家、下跌 {breadth['down']} 家，涨跌家数比 {ad}。"
        ),
        "metrics": {
            "up_ratio_pct": up_ratio, "advance_decline": ad,
            "up": breadth["up"], "down": breadth["down"],
        },
    }


def _check_us_options_expensive(ctx: dict[str, Any]) -> dict[str, Any]:
    """核查「美股期权非常贵，Vega 非常高」。"""
    quote = ctx.get("vix_quote") or {}
    history = ctx.get("vix_history") or {}
    series = [row["close"] for row in history.get("series") or []]
    last = quote.get("last")
    if last is None:
        return {"status": "pending", "text": "VIX 实时报价暂不可用，无法核查。"}
    if not series:
        return {
            "status": "pending",
            "text": f"VIX 现价 {last:.2f}，但历史序列缺失，无法给出百分位。",
        }
    percentile = _percentile_of(last, series)
    sample_start = (history.get("series") or [{}])[0].get("date", "")
    if percentile is None:
        status = "pending"
    elif percentile >= 80:
        status = "confirmed"
    elif percentile >= 50:
        status = "partial"
    else:
        status = "refuted"
    return {
        "status": status,
        "text": (
            f"VIX 现价 {last:.2f}"
            + (f"（{quote.get('quote_date','')} {quote.get('quote_time','')}）" if quote.get("quote_date") else "")
            + f"，处于 {sample_start} 以来 {len(series)} 个交易日分布的第 {percentile} 百分位，"
            f"中位数 {round(sorted(series)[len(series)//2], 2)}。"
            + ("属历史偏高区间。" if percentile and percentile >= 80 else
               "属中性区间，谈不上「非常贵」。" if percentile and percentile >= 50 else
               "属历史偏低区间，「非常贵」的说法不成立。")
        ),
        "metrics": {
            "vix_last": last, "percentile": percentile, "sample_days": len(series),
            "sample_start": sample_start,
            "median": round(sorted(series)[len(series) // 2], 2),
        },
    }


def _check_cn_options_cheap(ctx: dict[str, Any]) -> dict[str, Any]:
    """核查「A股期权不贵」。"""
    items = ctx.get("option_items") or []
    if not items:
        return {"status": "pending", "text": "期权数据暂不可用，无法核查。"}
    item = items[0]
    near = item.get("iv_near") or {}
    iv = near.get("iv")
    shape = item.get("term_shape")
    shape_text = {
        "contango": "近月低于次月（contango），属正常形态",
        "backwardation": "近月高于次月（backwardation），近端避险需求偏强",
        "flat": "近月与次月基本持平",
    }.get(shape or "", "期限结构不可用")
    if iv is None:
        return {"status": "pending", "text": "近月平值 IV 不可用，无法核查。"}
    sample = ctx.get("iv_history_sample") or []
    percentile = _percentile_of(iv, sample) if sample else None
    if percentile is None:
        status = "partial"
        text = (
            f"50ETF 近月（{near.get('days','')} 天）平值 IV {iv}%，{shape_text}。"
            f"绝对水平跨市场不可比，A股自身历史百分位需积累样本（当前 {ctx.get('iv_history_days', 0)} 个交易日）。"
        )
    elif percentile >= 60:
        status = "refuted"
        text = (
            f"50ETF 近月平值 IV {iv}%，处于自身历史第 {percentile} 百分位，"
            f"「不贵」不成立。{shape_text}。"
        )
    else:
        status = "confirmed"
        text = (
            f"50ETF 近月平值 IV {iv}%，处于自身历史第 {percentile} 百分位，"
            f"相对自身确实不贵。{shape_text}。"
        )
    return {
        "status": status,
        "text": text,
        "metrics": {
            "iv_near": iv, "iv_percentile": percentile,
            "history_days": ctx.get("iv_history_days", 0),
            "term_shape": shape,
        },
    }


def _check_term_structure(ctx: dict[str, Any]) -> dict[str, Any]:
    """补充核查：期限结构（原文未提，但这是期权口径最强的信号）。"""
    items = ctx.get("option_items") or []
    if not items:
        return {"status": "pending", "text": "期权数据暂不可用。"}
    item = items[0]
    shape = item.get("term_shape")
    slope = item.get("term_slope")
    if shape is None:
        return {"status": "pending", "text": "期限结构暂不可用。"}
    if shape == "backwardation":
        status, note = "confirmed", "近端避险需求集中，属压力形态"
    elif shape == "contango":
        status, note = "confirmed", "属常态形态，无近端避险压力"
    else:
        status, note = "partial", "近月与次月基本持平"
    return {
        "status": status,
        "text": f"50ETF 近月与次月 IV 差 {slope} 个百分点，{note}（{shape}）。",
        "metrics": {"term_shape": shape, "term_slope": slope},
    }


def _check_vega_vix_confusion(ctx: dict[str, Any]) -> dict[str, Any]:
    """概念核查：Vega 是否等于 VIX。"""
    return {
        "status": "refuted",
        "text": (
            "概念错误。Vega 是期权希腊字母（IV 每变动 1 个百分点，期权价格变动多少），"
            "VIX 是标普 500 期权隐含波动率指数。两者相关但不是同一物——"
            "把「Vega 处于低位」当作「VIX 低」来推论期权便宜与否，推不出来。"
        ),
        "metrics": {},
    }


def _check_vix_usage(ctx: dict[str, Any]) -> dict[str, Any]:
    """概念核查：VIX 是不是只用来判断期权贵不贵。"""
    quote = ctx.get("vix_quote") or {}
    history = ctx.get("vix_history") or {}
    series = [row["close"] for row in history.get("series") or []]
    last = quote.get("last")
    percentile = _percentile_of(last, series) if (last is not None and series) else None
    return {
        "status": "partial",
        "text": (
            "半对。VIX 确实同时是「保险价格」（隐含波动率、期权贵不贵）和"
            "「市场对未来波动的定价」（情绪与风险预期），两种用途不冲突。"
            + (f"当前 {last:.2f}，历史第 {percentile} 百分位。" if percentile else "")
        ),
        "metrics": {"vix_last": last, "percentile": percentile},
    }


def _check_put_vs_futures(ctx: dict[str, Any]) -> dict[str, Any]:
    """原理核查：买认沽 vs 期货做空的风险结构。"""
    return {
        "status": "confirmed",
        "text": (
            "成立，且是定义层面的事实而非经验规律：买方的最大损失是已付权利金（有上界），"
            "期货空头的亏损随标的上行线性扩大（无上界），所以期货更容易被一次性反弹触发止损。"
            "需要注意认沽买方的代价是时间价值持续消耗——方向看错仍会亏光权利金，"
            "「风险可控」指的是亏损有上限，不是不会亏。"
        ),
        "metrics": {},
    }


def _check_leverage(ctx: dict[str, Any]) -> dict[str, Any]:
    """原理核查：杠杆放大的是风险。"""
    return {
        "status": "confirmed",
        "text": (
            "成立，属定义层面的事实：杠杆按比例放大盈亏，盈利后加仓放大的是下行敞口。"
            "对期货而言还叠加保证金追缴，这是期货相对期权在「反弹毛刺」场景下更脆弱的原因。"
        ),
        "metrics": {},
    }


def _check_theta_selling(ctx: dict[str, Any]) -> dict[str, Any]:
    """原理核查：横盘做卖方赚 Theta。"""
    return {
        "status": "partial",
        "text": (
            "机制正确但不完整：横盘时卖方确实赚时间价值衰减（Theta 收租）。"
            "但卖方是负凸性持仓，Gamma 为负——横盘稳定收租，跳空时亏损随行权价偏离平方放大，"
            "属小概率大亏损。原文只说了收租机制，没有覆盖这条策略的尾部风险特征。"
        ),
        "metrics": {},
    }


def _check_naked_short(ctx: dict[str, Any]) -> dict[str, Any]:
    """原理核查：非裸卖风险是否很低。"""
    return {
        "status": "partial",
        "text": (
            "对期权买方完全成立（不会穿仓，最大损失是权利金）。对卖方需要分档："
            "naked short 亏损无上界，非裸卖（有担保/备兑）才把风险约束在权利金范围内。"
            "原文结论方向对，但适用条件需要写明，否则容易被读成「卖期权都安全」。"
        ),
        "metrics": {},
    }


def _check_trend_following(ctx: dict[str, Any]) -> dict[str, Any]:
    """经验核查：跌时做空、涨时做多是否「怎么着都能赚」。"""
    return {
        "status": "partial",
        "text": (
            "条件性成立。趋势跟随在单边行情中有效，但原文漏了两项成本："
            "做空有负 carry（融券费或期货基差），以及横盘期反复止损的磨损。"
            "「怎么着都能赚」不成立——只有「有趋势时赚钱」成立。"
        ),
        "metrics": {},
    }


CLAIM_CHECKS: list[dict[str, Any]] = [
    {
        "id": "market_direction",
        "title": "市场继续往下走、且有加速态势",
        "factor": "指数方向",
        "type": "data",
        "check": _check_market_direction,
    },
    {
        "id": "level_3741",
        "title": "3741 肯定守不住",
        "factor": "点位断言",
        "type": "data",
        "check": _check_level_3741,
    },
    {
        "id": "breadth_broken",
        "title": "市场宽广度已经废掉了",
        "factor": "市场广度",
        "type": "data",
        "check": _check_breadth,
    },
    {
        "id": "us_options_expensive",
        "title": "美股期权非常贵、Vega 非常高",
        "factor": "IV 绝对水平",
        "type": "data",
        "check": _check_us_options_expensive,
    },
    {
        "id": "cn_options_cheap",
        "title": "A股期权不贵",
        "factor": "IV 绝对水平",
        "type": "data",
        "check": _check_cn_options_cheap,
    },
    {
        "id": "term_structure",
        "title": "（原文未提）期限结构：近月 vs 次月",
        "factor": "期限结构",
        "type": "data",
        "check": _check_term_structure,
    },
    {
        "id": "vega_is_vix",
        "title": "Vega 处于低位，就是 VIX",
        "factor": "Vega / VIX",
        "type": "concept",
        "check": _check_vega_vix_confusion,
    },
    {
        "id": "vix_not_risk",
        "title": "VIX 只判断期权贵不贵，不判断股市风险",
        "factor": "VIX 用途",
        "type": "concept",
        "check": _check_vix_usage,
    },
    {
        "id": "put_vs_futures",
        "title": "买认沽风险可控，不会被反弹毛刺打掉",
        "factor": "期权 vs 期货",
        "type": "principle",
        "check": _check_put_vs_futures,
    },
    {
        "id": "leverage_is_risk",
        "title": "加杠杆加的是风险",
        "factor": "杠杆",
        "type": "principle",
        "check": _check_leverage,
    },
    {
        "id": "theta_selling",
        "title": "横盘就做期权卖方赚时间价值",
        "factor": "Theta",
        "type": "principle",
        "check": _check_theta_selling,
    },
    {
        "id": "naked_short",
        "title": "不是 naked short 风险就很低、不会穿仓",
        "factor": "裸卖风险",
        "type": "principle",
        "check": _check_naked_short,
    },
    {
        "id": "trend_following",
        "title": "跌时做空、涨时做多，怎么着都能赚",
        "factor": "顺势策略",
        "type": "principle",
        "check": _check_trend_following,
    },
]

STATUS_TEXT = {
    "confirmed": "已验证",
    "partial": "部分成立",
    "refuted": "不成立",
    "pending": "待验证",
}


# ------------------------------------------------------------------- 组装


def _fetch_options_bounded(seconds: float) -> dict[str, Any]:
    """在时间预算内取期权情绪数据，超时按「暂无数据」处理。

    冷启动时东财若被 WAF 封禁，期权链拉取会轮询 5 个主机各 15 秒。
    这个端点还包含 VIX、广度、指数日K 等其他数据源，不应该被一个源拖住，
    所以这里给期权拉取单独设预算：超时就先出其余读数，期权部分留待下一轮。
    """
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(options_sentiment_module.get_options_sentiment)
        return future.result(timeout=seconds)
    except FuturesTimeoutError:
        return {
            "available": False,
            "items": [],
            "error": f"期权数据拉取超过 {seconds:.0f} 秒预算，本轮先跳过",
            "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        executor.shutdown(wait=False)


def _build_context() -> dict[str, Any]:
    ctx: dict[str, Any] = {"errors": []}

    try:
        ctx["sse"] = fetch_index_history("sh000001", days=120)
    except Exception as exc:
        ctx["sse"] = {"bars": []}
        ctx["errors"].append(f"上证日K: {type(exc).__name__}: {exc}")

    try:
        ctx["breadth"] = fetch_breadth()
    except Exception as exc:
        ctx["breadth"] = {}
        ctx["errors"].append(f"市场广度: {type(exc).__name__}: {exc}")

    try:
        ctx["vix_quote"] = fetch_vix_quote()
    except Exception as exc:
        ctx["vix_quote"] = {}
        ctx["errors"].append(f"VIX 报价: {type(exc).__name__}: {exc}")

    try:
        ctx["vix_history"] = fetch_vix_history(count=500)
    except Exception as exc:
        ctx["vix_history"] = {"series": []}
        ctx["errors"].append(f"VIX 历史: {type(exc).__name__}: {exc}")

    try:
        options = _fetch_options_bounded(seconds=_OPTIONS_FETCH_BUDGET_SEC)
        ctx["option_items"] = options.get("items") or []
        ctx["options_meta"] = {
            "available": bool(options.get("available")) and bool(options.get("items")),
            "computed_at": options.get("computed_at"),
            "stale": bool(options.get("stale")),
            "error": options.get("error") or "; ".join(options.get("errors") or []),
        }
    except Exception as exc:
        ctx["option_items"] = []
        ctx["options_meta"] = {
            "available": False, "computed_at": None, "stale": False, "error": str(exc),
        }
        ctx["errors"].append(f"期权情绪: {type(exc).__name__}: {exc}")

    store = RegimeHistoryStore()
    ctx["iv_history_sample"] = [v for _, v in store.series("options", "iv_near")]
    ctx["iv_history_days"] = store.sample_days()
    ctx["pcr_history_sample"] = [v for _, v in store.series("options", "oi_pcr")]
    return ctx


def _snapshot_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """从期权 items 提取每日留档指标。"""
    if not items:
        return {}
    item = items[0]
    near = item.get("iv_near") or {}
    return {
        "iv_near": near.get("iv"),
        "iv_next": (item.get("iv_next") or {}).get("iv"),
        "oi_pcr": item.get("oi_pcr"),
        "vol_pcr": item.get("vol_pcr"),
        "rr_25d": item.get("rr_25d"),
        "bf_25d": item.get("bf_25d"),
        "term_shape": item.get("term_shape"),
    }


def compute_derivatives_regime() -> dict[str, Any]:
    """完整计算一次（数秒级）。调用方应走 get_derivatives_regime 缓存。"""
    ctx = _build_context()

    # 每日留档（仅在有有效读数时写）
    snapshot = _snapshot_from_items(ctx.get("option_items") or [])
    if snapshot.get("iv_near") is not None:
        try:
            RegimeHistoryStore().record(date.today().isoformat(), snapshot)
        except Exception:
            pass  # 留档失败不影响本次读数

    checks: list[dict[str, Any]] = []
    for claim in CLAIM_CHECKS:
        try:
            verdict = claim["check"](ctx)
        except Exception as exc:
            verdict = {
                "status": "pending",
                "text": f"核查执行失败：{type(exc).__name__}: {exc}",
                "metrics": {},
            }
        checks.append({
            "id": claim["id"],
            "title": claim["title"],
            "factor": claim["factor"],
            "type": claim["type"],
            "status": verdict.get("status", "pending"),
            "status_text": STATUS_TEXT.get(verdict.get("status", "pending"), "待验证"),
            "text": verdict.get("text", ""),
            "metrics": verdict.get("metrics", {}),
        })

    # 实时衍生品读数（前端卡片）
    readings: list[dict[str, Any]] = []
    for item in ctx.get("option_items") or []:
        near = item.get("iv_near") or {}
        readings.append({
            "label": item.get("label") or item.get("key"),
            "underlying_price": item.get("underlying_price"),
            "underlying_change_pct": item.get("underlying_change_pct"),
            "iv_near": near.get("iv"),
            "iv_near_days": near.get("days"),
            "iv_next": (item.get("iv_next") or {}).get("iv"),
            "iv_next_days": (item.get("iv_next") or {}).get("days"),
            "rr_25d": item.get("rr_25d"),
            "bf_25d": item.get("bf_25d"),
            "skew_expiry_days": item.get("skew_expiry_days"),
            "skew_tenor": item.get("skew_tenor"),
            "term_shape": item.get("term_shape"),
            "term_slope": item.get("term_slope"),
            "iv_term": item.get("iv_term") or [],
            "oi_pcr": item.get("oi_pcr"),
            "vol_pcr": item.get("vol_pcr"),
            "oi_change_ratio": item.get("oi_change_ratio"),
            "max_pain": item.get("max_pain"),
            "oi_change_call": item.get("oi_change_call"),
            "oi_change_put": item.get("oi_change_put"),
        })

    # VIX 读数 + 百分位
    quote = ctx.get("vix_quote") or {}
    series = [row["close"] for row in (ctx.get("vix_history") or {}).get("series") or []]
    vix = {
        "last": quote.get("last"),
        "change": quote.get("change"),
        "change_pct": quote.get("change_pct"),
        "prev_close": quote.get("prev_close"),
        "quote_date": quote.get("quote_date"),
        "quote_time": quote.get("quote_time"),
        "percentile": _percentile_of(quote["last"], series) if quote.get("last") and series else None,
        "sample_days": len(series),
        "sample_start": (ctx.get("vix_history", {}).get("series") or [{}])[0].get("date", ""),
        "median": round(sorted(series)[len(series) // 2], 2) if series else None,
        "min": round(min(series), 2) if series else None,
        "max": round(max(series), 2) if series else None,
    }

    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claim_source": CLAIM_SOURCE,
        "factor_review": FACTOR_REVIEW,
        "checks": checks,
        "readings": readings,
        "vix": vix,
        "options_meta": ctx.get("options_meta") or {},
        "breadth": ctx.get("breadth") or {},
        "history": {
            "iv_history_days": ctx.get("iv_history_days", 0),
            "iv_percentile": _percentile_of(
                (readings[0].get("iv_near") if readings else None),
                ctx.get("iv_history_sample") or [],
            ) if readings and readings[0].get("iv_near") and ctx.get("iv_history_sample") else None,
            "pcr_percentile": _percentile_of(
                (readings[0].get("oi_pcr") if readings else None),
                ctx.get("pcr_history_sample") or [],
            ) if readings and readings[0].get("oi_pcr") and ctx.get("pcr_history_sample") else None,
        },
        "errors": ctx.get("errors") or [],
    }


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}


def get_derivatives_regime() -> dict[str, Any]:
    """5 分钟 TTL 缓存；失败时回退最近一次成功结果并标 stale。"""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["data"]
        if cached is not None and now - _cache["at"] < _CACHE_TTL_SEC:
            return cached
    try:
        data = compute_derivatives_regime()
    except Exception as exc:
        with _cache_lock:
            cached = _cache["data"]
        if cached is not None:
            stale = dict(cached)
            stale["stale"] = True
            stale["refresh_error"] = f"{type(exc).__name__}: {exc}"
            return stale
        return {
            "ok": False,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
            "claim_source": CLAIM_SOURCE,
            "factor_review": FACTOR_REVIEW,
            "checks": [],
            "readings": [],
            "vix": {},
        }
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["data"] = data
    return data


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(compute_derivatives_regime(), ensure_ascii=False, indent=2))
