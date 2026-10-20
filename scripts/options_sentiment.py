# -*- coding: utf-8 -*-
"""期权情绪指标：50ETF/300ETF 期权的 PCR 与平值隐含波动率（IV 恐慌指数）。

数据源：东方财富 ETF 期权行情（沪市 m:10 标准合约）。
IV 用 Black-Scholes 反解（二分法，无 scipy 依赖）；PCR = 认沽/认购。

结果带 10 分钟内存缓存；拉取失败时返回最近一次成功结果（stale 标记）。
"""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from typing import Any, Optional

_EM_CLIST_HOSTS = [
    "23.push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "90.push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "19.push2.eastmoney.com",
    "29.push2.eastmoney.com",
    "push2.eastmoney.com",
]
_EM_QUOTE_HOSTS = [
    "90.push2.eastmoney.com",
    "23.push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "48.push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "19.push2.eastmoney.com",
    "29.push2.eastmoney.com",
    "push2.eastmoney.com",
]
_EM_CLIST_PATH = "/api/qt/clist/get"
_EM_QUOTE_PATH = "/api/qt/stock/get"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
_UT = "bd1d9ddb04089700cf9c27f6f7426281"

_RISK_FREE_RATE = 0.015  # 无风险利率近似
_CACHE_TTL_SEC = 600.0

# 数据源熔断：东财 WAF 封禁期间，单次拉取要轮询 5 个主机各 15 秒。
# 一次完整轮询全失败已经是充分证据（不是偶发单点故障），所以失败一次即冷却，
# 冷却期内直接返回上次缓存，不再发起网络请求。
_BREAKER_MIN_FAILURES = 1
_BREAKER_COOLDOWN_SEC = 180.0

# 标的：名称前缀 -> (展示名, 东财 secid)
_UNDERLYINGS = {
    "50ETF": ("50ETF期权", "1.510050"),
    "300ETF": ("300ETF期权", "1.510300"),
}

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_breaker: dict[str, Any] = {"failures": 0, "last_failure_at": 0.0}


def _curl_get_json(url: str, timeout: float = 15.0) -> dict[str, Any]:
    """curl 子进程取 JSON：东财只放行 HTTP/2 握手，requests/urllib3 会被断连。"""
    command = [
        "/usr/bin/curl",
        "--silent", "--show-error", "--fail", "--compressed",
        "--max-time", str(int(timeout)),
        "-H", f"User-Agent: {_HEADERS['User-Agent']}",
        "-H", f"Referer: {_HEADERS['Referer']}",
        url,
    ]
    result = subprocess.run(
        command, capture_output=True, check=True, timeout=timeout + 4.0
    )
    payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("响应不是 JSON 对象")
    return payload


def _curl_get_json_multi_host(path_and_query: str, hosts: list[str]) -> dict[str, Any]:
    """东财 WAF 会临时封禁单个编号主机，按候选列表轮换重试。

    起点按时间偏移轮换：若每次都从列表首台开始，连续轮询会把同一台主机
    反复打满，反而是最先被 WAF 封掉的那台。
    """
    last_error: Optional[Exception] = None
    ordered = list(hosts)
    if len(ordered) > 1:
        offset = int(time.monotonic()) % len(ordered)
        ordered = ordered[offset:] + ordered[:offset]
    for host in ordered:
        try:
            return _curl_get_json(f"https://{host}{path_and_query}")
        except Exception as exc:  # 记录后换下一台主机
            last_error = exc
            time.sleep(0.3)
    raise RuntimeError(f"东财主机全部失败: {last_error}")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _fetch_all_options() -> list[dict[str, Any]]:
    """分页拉取沪深两市全部 ETF 期权合约行情。"""
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _curl_get_json_multi_host(
            f"{_EM_CLIST_PATH}?pn={page}&pz=500&po=1&np=1&ut={_UT}"
            "&fltt=2&invt=2&fid=f3&fs=m:10,m:12"
            "&fields=f2,f3,f5,f12,f13,f14,f108,f161,f162,f163",
            _EM_CLIST_HOSTS,
        )
        data = payload.get("data")
        if not data or not data.get("diff"):
            break
        rows.extend(data["diff"])
        if len(rows) >= int(data.get("total") or 0):
            break
        page += 1
        if page > 20:  # 安全上限
            break
        time.sleep(0.4)  # 东财 WAF 对突发请求敏感，分页间稍作停顿
    return rows


def _fetch_underlying(secid: str) -> dict[str, Any]:
    payload = _curl_get_json_multi_host(
        f"{_EM_QUOTE_PATH}?secid={secid}&ut={_UT}&fields=f43,f57,f58,f60",
        _EM_QUOTE_HOSTS,
    )
    data = payload.get("data") or {}
    price = _to_float(data.get("f43"))
    prev = _to_float(data.get("f60"))
    if price is None or prev in (None, 0):
        raise RuntimeError(f"标的行情不可用: {secid}")
    return {
        "name": str(data.get("f58") or ""),
        "price": price / 1000.0,
        "prev": prev / 1000.0,
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(s: float, k: float, t: float, r: float, sigma: float, cp: int) -> float:
    if t <= 0 or sigma <= 0:
        return max(cp * (s - k), 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if cp == 1:
        return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def _implied_vol(price: float, s: float, k: float, t: float, r: float, cp: int) -> Optional[float]:
    intrinsic = max(cp * (s - k), 0.0)
    if price <= 0 or price <= intrinsic + 1e-4:
        return None
    lo, hi = 1e-4, 5.0
    if _bs_price(s, k, t, r, lo, cp) - price > 0:
        return None
    if _bs_price(s, k, t, r, hi, cp) - price < 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if _bs_price(s, k, t, r, mid, cp) - price > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def _atm_iv(contracts: list[dict[str, Any]], s: float, days: float, n: int = 3) -> Optional[float]:
    """指定到期剩余天数下，平值附近 n 档认购+认沽的平均 IV。"""
    ivs = []
    for cp in (1, -1):
        leg = [c for c in contracts if c["days"] == days and c["cp"] == cp]
        leg.sort(key=lambda c: abs(c["strike"] - s))
        for row in leg[:n]:
            iv = _implied_vol(row["price"], s, row["strike"], days / 365.0, _RISK_FREE_RATE, cp)
            if iv:
                ivs.append(iv)
    return sum(ivs) / len(ivs) if ivs else None


# ---- Delta / Vega 口径的偏斜度量（25Δ 风险逆转与蝶式） ----
#
# 原先的 "iv_skew" 取的是「离标的最���的一档虚值 put 与 call 的 IV 之差」，
# 三个问题：行权价是离散网格，跨档差值不可比；近月深虚值合约时间价值趋零，
# IV 反解对报价的微小变化极其敏感（实测可偏离 40 个百分点以上）；
# 该口径无法跨到期日、跨标的比较。
# 行业标准做法是用 delta 定位行权价（25Δ RR / 25Δ BF），并剔除 vega 过低的合约。

_VEGA_FLOOR = 0.002  # vega 低于此值（近月深虚值）IV 反解不稳定，直接剔除


def _delta_vega(s: float, k: float, t: float, r: float, sigma: float, cp: int) -> tuple[float, float]:
    """返回 (delta, vega)。vega 为标的价格变动 1.0 对应的期权价格变动。"""
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return (0.0, 0.0)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    delta = _norm_cdf(d1) if cp == 1 else _norm_cdf(d1) - 1.0
    vega = s * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * math.sqrt(t)
    return (delta, vega)


def _chain_with_greeks(
    contracts: list[dict[str, Any]], s: float, days: float, cp: int
) -> list[dict[str, float]]:
    """到期日 days、方向 cp 的期权链，带 IV/delta/vega，已剔除 vega 过低的合约。"""
    chain: list[dict[str, float]] = []
    for c in contracts:
        if c["days"] != days or c["cp"] != cp:
            continue
        iv = _implied_vol(c["price"], s, c["strike"], days / 365.0, _RISK_FREE_RATE, cp)
        if not iv:
            continue
        delta, vega = _delta_vega(s, c["strike"], days / 365.0, _RISK_FREE_RATE, iv, cp)
        if vega < _VEGA_FLOOR:
            continue
        chain.append({"strike": c["strike"], "iv": iv, "delta": delta, "vega": vega})
    chain.sort(key=lambda x: x["strike"])
    return chain


def _iv_at_delta(chain: list[dict[str, float]], target_abs_delta: float) -> Optional[float]:
    """在链上按 |delta| 线性插值取目标 delta 对应的 IV。"""
    if not chain:
        return None
    points = sorted(
        ((abs(x["delta"]), x["iv"]) for x in chain), key=lambda p: p[0]
    )
    if len(points) == 1:
        return points[0][1] if abs(points[0][0] - target_abs_delta) <= 0.12 else None
    for (d0, iv0), (d1, iv1) in zip(points, points[1:]):
        if d0 <= target_abs_delta <= d1:
            if abs(d1 - d0) < 1e-9:
                return iv0
            weight = (target_abs_delta - d0) / (d1 - d0)
            return iv0 + weight * (iv1 - iv0)
    nearest = min(points, key=lambda p: abs(p[0] - target_abs_delta))
    return nearest[1] if abs(nearest[0] - target_abs_delta) <= 0.10 else None


def _delta_skew(contracts: list[dict[str, Any]], s: float, days_sorted: list[float]) -> dict[str, Any]:
    """25Δ 风险逆转（RR）与 25Δ 蝶式（BF）。

    口径说明：选剩余天数最接近 30 天且不少于 14 天的到期月做恒定期限比较，
    近月合约时间价值趋零会放大 IV 反解误差，恒定期限（constant maturity）
    才是跨时点可比的读数。
    """
    candidates = sorted((d for d in days_sorted if d >= 14), key=lambda d: abs(d - 30.0))
    for days in candidates:
        iv25_call = _iv_at_delta(_chain_with_greeks(contracts, s, days, 1), 0.25)
        iv25_put = _iv_at_delta(_chain_with_greeks(contracts, s, days, -1), 0.25)
        atm = _atm_iv(contracts, s, days)
        if iv25_call and iv25_put and atm:
            return {
                "rr_25d": round((iv25_put - iv25_call) * 100, 2),
                "bf_25d": round((0.5 * (iv25_call + iv25_put) - atm) * 100, 2),
                "iv_25d_call": round(iv25_call * 100, 2),
                "iv_25d_put": round(iv25_put * 100, 2),
                "expiry_days": int(days),
                "tenor": "恒定期限(≈30天)",
            }
    return {"rr_25d": None, "bf_25d": None, "expiry_days": None, "tenor": "恒定期限(≈30天)"}


def _analyze_underlying(prefix: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    label, secid = _UNDERLYINGS[prefix]
    underlying = _fetch_underlying(secid)

    # 沪市期权 市场=10；剔除带 A 的非标准（分红调整）合约
    contracts: list[dict[str, Any]] = []
    for row in rows:
        if row.get("f13") != 10:
            continue
        name = str(row.get("f14") or "")
        if not name.startswith(prefix) or "A" in name:
            continue
        price = _to_float(row.get("f2"))
        strike = _to_float(row.get("f161"))
        days = _to_float(row.get("f162"))
        if price is None or strike is None or days is None:
            continue
        contracts.append({
            "name": name,
            "cp": 1 if "购" in name else -1,
            "price": price,
            "strike": strike,
            "days": days,
            "oi": _to_float(row.get("f108")) or 0.0,
            "volume": _to_float(row.get("f5")) or 0.0,
            "oi_change": _to_float(row.get("f163")) or 0.0,
        })

    calls = [c for c in contracts if c["cp"] == 1]
    puts = [c for c in contracts if c["cp"] == -1]
    call_oi = sum(c["oi"] for c in calls)
    put_oi = sum(c["oi"] for c in puts)
    call_vol = sum(c["volume"] for c in calls)
    put_vol = sum(c["volume"] for c in puts)

    s = underlying["price"]
    days_sorted = sorted({c["days"] for c in contracts if c["days"] > 0})
    iv_near = _atm_iv(contracts, s, days_sorted[0]) if days_sorted else None
    iv_next = _atm_iv(contracts, s, days_sorted[1]) if len(days_sorted) > 1 else None

    # ---- 以下为先行指标扩展字段，纯后处理，不新增网络请求 ----
    # 完整期限结构：每个到期日的 ATM IV
    iv_term = []
    for d in days_sorted:
        iv = _atm_iv(contracts, s, d)
        if iv:
            iv_term.append({"days": int(d), "iv": round(iv * 100, 1)})

    # IV skew：25Δ 近似——取最接近 spot 的 OTM put 与 OTM call 的 IV 差。
    # OTM put = strike < s 的认沽；OTM call = strike > s 的认购。
    iv_skew = None
    try:
        otm_puts = sorted([c for c in puts if c["strike"] < s], key=lambda c: abs(c["strike"] - s))
        otm_calls = sorted([c for c in calls if c["strike"] > s], key=lambda c: abs(c["strike"] - s))
        nearest_days = days_sorted[0] if days_sorted else None
        if nearest_days and otm_puts and otm_calls:
            iv_put = _implied_vol(otm_puts[0]["price"], s, otm_puts[0]["strike"], nearest_days / 365.0, _RISK_FREE_RATE, -1)
            iv_call = _implied_vol(otm_calls[0]["price"], s, otm_calls[0]["strike"], nearest_days / 365.0, _RISK_FREE_RATE, 1)
            if iv_put and iv_call:
                iv_skew = round((iv_put - iv_call) * 100, 2)
    except Exception:
        iv_skew = None

    # Max pain：使所有 call+put 持有者总收益最小化的到期价位。
    max_pain = None
    try:
        strikes = sorted({c["strike"] for c in contracts})
        if strikes:
            best_strike = strikes[0]
            best_pain = None
            for k in strikes:
                pain = 0.0
                for c in contracts:
                    if c["cp"] == 1:  # call：价高于 K 时卖方亏 = max(S-K,0)*OI
                        pain += max(k - c["strike"], 0.0) * c["oi"]
                    else:  # put：价低于 K 时卖方亏 = max(K-S,0)*OI
                        pain += max(c["strike"] - k, 0.0) * c["oi"]
                if best_pain is None or pain < best_pain:
                    best_pain = pain
                    best_strike = k
            max_pain = round(best_strike, 4)
    except Exception:
        max_pain = None

    # 期限结构形态：近月 IV vs 次月 IV。
    # contango（近月 < 次月）是常态；backwardation（近月 > 次月）表示近端避险需求
    # 集中，是期权口径里最可靠的压力信号——比 IV 绝对水平更早反映风险偏好切换。
    term_shape = None
    term_slope = None
    if iv_near and iv_next:
        term_slope = round(iv_next["iv"] - iv_near["iv"], 2)
        if term_slope > 1.0:
            term_shape = "contango"
        elif term_slope < -1.0:
            term_shape = "backwardation"
        else:
            term_shape = "flat"

    # 25Δ 口径偏斜（恒定期限）
    skew_25d = _delta_skew(contracts, s, days_sorted)

    # 按到期日分组的 OI PCR
    oi_pcr_by_expiry = []
    for d in days_sorted:
        calls_d = [c for c in calls if c["days"] == d]
        puts_d = [c for c in puts if c["days"] == d]
        co = sum(c["oi"] for c in calls_d)
        po = sum(c["oi"] for c in puts_d)
        if co > 0:
            oi_pcr_by_expiry.append({"days": int(d), "pcr": round(po / co, 3)})

    return {
        "key": prefix,
        "label": label,
        "underlying_price": round(s, 4),
        "underlying_prev": round(underlying["prev"], 4),
        "underlying_change_pct": round((s - underlying["prev"]) / underlying["prev"] * 100, 3),
        "contracts": len(contracts),
        "oi_pcr": round(put_oi / call_oi, 3) if call_oi > 0 else None,
        "vol_pcr": round(put_vol / call_vol, 3) if call_vol > 0 else None,
        "oi_change_call": int(sum(c["oi_change"] for c in calls)),
        "oi_change_put": int(sum(c["oi_change"] for c in puts)),
        "iv_near": {"days": int(days_sorted[0]), "iv": round(iv_near * 100, 1)} if iv_near and days_sorted else None,
        "iv_next": {"days": int(days_sorted[1]), "iv": round(iv_next * 100, 1)} if iv_next and len(days_sorted) > 1 else None,
        # 扩展字段（先行指标区用）
        "iv_term": iv_term,
        "iv_skew": iv_skew,
        "max_pain": max_pain,
        "oi_pcr_by_expiry": oi_pcr_by_expiry,
        # 期限结构与 25Δ 口径偏斜（衍生品风险仪表盘用）
        "term_shape": term_shape,
        "term_slope": term_slope,
        "rr_25d": skew_25d["rr_25d"],
        "bf_25d": skew_25d["bf_25d"],
        "iv_25d_call": skew_25d.get("iv_25d_call"),
        "iv_25d_put": skew_25d.get("iv_25d_put"),
        "skew_expiry_days": skew_25d["expiry_days"],
        "skew_tenor": skew_25d["tenor"],
        # 持仓变化比：认沽/认购的净增仓比，比持仓量水平更早反映资金流向
        "oi_change_ratio": (
            round(sum(c["oi_change"] for c in puts) / sum(c["oi_change"] for c in calls), 3)
            if sum(c["oi_change"] for c in calls) > 0
            else None
        ),
    }


def compute_options_sentiment() -> dict[str, Any]:
    """完整拉取并计算（约 2~5 秒），调用方应走 get_options_sentiment 缓存。"""
    rows = _fetch_all_options()
    items = []
    errors = []
    for prefix in _UNDERLYINGS:
        try:
            items.append(_analyze_underlying(prefix, rows))
        except Exception as exc:  # 单个标的失败不影响另一个
            errors.append(f"{prefix}: {type(exc).__name__}: {exc}")
    if not items:
        raise RuntimeError("期权情绪计算失败: " + "; ".join(errors or ["无数据"]))
    return {
        "available": True,
        "items": items,
        "errors": errors,
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "contracts_total": len(rows),
    }


def get_options_sentiment() -> dict[str, Any]:
    """10 分钟 TTL 缓存；失败时回退最近一次成功结果并标 stale。

    带熔断：东财 WAF 封禁时，_fetch_all_options 会依次重试 5 个主机、每个
    15 秒超时，单次失败要拖 75 秒以上。页面的先行指标每 8 秒轮询一次，
    封禁期间会不断堆积慢请求。这里在连续失败后进入冷却窗口，期间直接
    返回上一次成功结果（标 stale），不再发起网络请求。
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["data"]
        if cached is not None and now - _cache["at"] < _CACHE_TTL_SEC:
            return cached
        failures = _breaker["failures"]
        last_failure = _breaker["last_failure_at"]
        if failures >= _BREAKER_MIN_FAILURES and (now - last_failure) < _BREAKER_COOLDOWN_SEC:
            if cached is not None:
                stale = dict(cached)
                stale["stale"] = True
                stale["breaker"] = f"数据源熔断中（连续失败 {failures} 次），展示上一次缓存"
                return stale
            return {
                "available": False,
                "items": [],
                "error": f"数据源熔断中（连续失败 {failures} 次）",
                "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
    try:
        data = compute_options_sentiment()
    except Exception as exc:
        with _cache_lock:
            cached = _cache["data"]
            _breaker["failures"] += 1
            _breaker["last_failure_at"] = time.monotonic()
        if cached is not None:
            stale = dict(cached)
            stale["stale"] = True
            stale["refresh_error"] = f"{type(exc).__name__}: {exc}"
            return stale
        return {
            "available": False,
            "items": [],
            "error": f"{type(exc).__name__}: {exc}",
            "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["data"] = data
        _breaker["failures"] = 0
    return data
