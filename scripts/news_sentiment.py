"""新闻情绪量化：把市场新闻流按持仓标的聚合为情绪因子。

设计
----
* 纯规则词库（正面/负面词 + 强度修饰 + 否定翻转），离线可用、零外部依赖。
* 时间衰减：半衰期 24 小时，旧新闻影响递减。
* 来源权重：财联社 1.0、华尔街见闻 0.9、东方财富 0.85、Google News 0.8。
* 两个层次：标的专属情绪（symbols 命中该持仓，全权重）与大盘情绪
  （全部条目聚合）；标的当日无专属新闻时按 0.6 权重折算大盘情绪。
* 诚实声明：新闻没有历史归档，情绪因子无法像价格因子那样 walk-forward
  回测——在 nextday_model 中权重保守（news_weight=0.15），并自 2026-09-30
  把每日情绪落盘 data/news-sentiment-log.jsonl，为未来回测积累样本。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

NEWS_SENTIMENT_VERSION = "news-sentiment.2026-09-30.1"
HALF_LIFE_HOURS = 24.0
MARKET_FALLBACK_WEIGHT = 0.6
DEFAULT_SOURCE_WEIGHT = 0.7
SOURCE_WEIGHTS: dict[str, float] = {
    "cls": 1.0,
    "wallstreetcn": 0.9,
    "eastmoney": 0.85,
    # 金十数据/同花顺快讯：2026-10-20 新增的备援源，与东财快讯同属专业财经快讯。
    "jin10": 0.85,
    "ths_news": 0.8,
    "google_news": 0.8,
}
NEGATORS = ("不", "未", "无", "难以", "取消", "终止", "暂缓")
INTENSIFIERS: tuple[tuple[str, float], ...] = (
    ("大幅", 1.5),
    ("显著", 1.5),
    ("严重", 1.6),
    ("超预期", 1.5),
    ("高于预期", 1.4),
    ("低于预期", 1.4),
    ("略有", 0.7),
    ("小幅", 0.8),
)
POSITIVE_TERMS: dict[str, float] = {
    "利好": 1.0, "中标": 0.9, "超预期": 0.9, "增持": 0.9, "回购": 0.8,
    "获批": 0.8, "预增": 0.9, "签约": 0.7, "涨价": 0.7, "订单": 0.7,
    "增长": 0.7, "盈利": 0.6, "扩产": 0.6, "分红": 0.5, "派息": 0.5,
    "上涨": 0.5, "突破": 0.5, "合作": 0.5, "回暖": 0.6, "创新高": 0.6,
}
NEGATIVE_TERMS: dict[str, float] = {
    "利空": 1.0, "立案": 1.0, "预亏": 1.0, "违约": 1.0, "退市": 1.0,
    "处罚": 0.9, "减持": 0.9, "商誉减值": 0.9, "亏损": 0.9, "调查": 0.8,
    "质押": 0.7, "诉讼": 0.7, "下滑": 0.7, "终止": 0.7, "失败": 0.7,
    "问询": 0.6, "解禁": 0.6, "警告": 0.6, "裁员": 0.6, "风险提示": 0.6,
    "下跌": 0.5, "降价": 0.5, "搁置": 0.6,
}
_THSCODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


def _parse_published(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recency_weight(published: datetime | None, now: datetime) -> float:
    if published is None:
        return 0.6  # 缺时间戳的条目按中等新鲜度处理
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    return math.pow(0.5, age_hours / HALF_LIFE_HOURS)


def _source_weight(item: Mapping[str, Any]) -> float:
    source_ids = item.get("source_ids") or [item.get("source_id")]
    weights = [
        SOURCE_WEIGHTS.get(str(sid), DEFAULT_SOURCE_WEIGHT)
        for sid in source_ids
        if sid
    ]
    return max(weights) if weights else DEFAULT_SOURCE_WEIGHT


def _term_polarity(text: str, terms: Mapping[str, float]) -> float:
    """统计一个方向词库的加权命中（含否定翻转与强度修饰）。"""
    total = 0.0
    for term, weight in terms.items():
        start = 0
        while True:
            hit = text.find(term, start)
            if hit < 0:
                break
            start = hit + len(term)
            window = text[max(0, hit - 2):hit]
            if any(neg in window for neg in NEGATORS):
                total -= weight  # 否定翻转：利好→非利好
                continue
            intensity = 1.0
            prefix = text[max(0, hit - 4):hit]
            for word, factor in INTENSIFIERS:
                if word in prefix:
                    intensity = factor
                    break
            total += weight * intensity
    return total


def score_text(title: str, summary: str = "") -> float:
    """单条新闻的极性得分 ∈ [-1, 1]（词库加权命中，tanh 饱和）。"""
    text = f"{title} {summary}".strip()
    if not text:
        return 0.0
    positive = _term_polarity(text, POSITIVE_TERMS)
    negative = _term_polarity(text, NEGATIVE_TERMS)
    return round(math.tanh((positive - negative) / 2.0), 3)


def _item_symbols(item: Mapping[str, Any]) -> set[str]:
    raw = item.get("symbols")
    if isinstance(raw, str):
        raw = [raw]
    return {
        str(value).strip().upper()
        for value in (raw or [])
        if _THSCODE_RE.match(str(value).strip().upper())
    }


def item_score(item: Mapping[str, Any], *, now: datetime) -> dict[str, Any] | None:
    # 付费墙条目正文不完整（金十 VIP/PLUS 专享快讯）：标题可能带情绪词但正文被锁，
    # 打分不可靠，按项目"宁可丢弃也不要用残缺数据"的原则直接不计入。
    if item.get("locked"):
        return None
    polarity = score_text(str(item.get("title") or ""), str(item.get("summary") or ""))
    if polarity == 0.0:
        return None
    published = _parse_published(item.get("published_at"))
    weight = _recency_weight(published, now) * _source_weight(item)
    return {
        "polarity": polarity,
        "weight": round(weight, 4),
        "score": round(polarity * weight, 4),
        "title": str(item.get("title") or "")[:80],
        "published_at": str(item.get("published_at") or ""),
    }


def _aggregate(scored: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not scored:
        return {"sentiment": None, "positive": 0, "negative": 0, "headlines": []}
    total_weight = sum(float(row["weight"]) for row in scored) or 1.0
    sentiment = round(sum(float(row["score"]) for row in scored) / total_weight, 3)
    positive = sum(1 for row in scored if float(row["polarity"]) > 0)
    negative = sum(1 for row in scored if float(row["polarity"]) < 0)
    headlines = [
        {"title": row["title"], "polarity": row["polarity"]}
        for row in sorted(scored, key=lambda row: abs(float(row["score"])), reverse=True)[:2]
    ]
    return {
        "sentiment": max(-1.0, min(1.0, sentiment)),
        "positive": positive,
        "negative": negative,
        "headlines": headlines,
    }


def summarize(
    items: Iterable[Mapping[str, Any]],
    symbols: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按持仓聚合新闻情绪。

    返回 {"available", "symbols": {thscode: {...}}, "market": {...}}：
    标的专属情绪（全权重）与大盘情绪（供标的缺新闻时按折算权重使用）。
    """
    now = now or datetime.now(timezone(timedelta(hours=8)))
    want = {str(symbol).strip().upper() for symbol in symbols}
    per_symbol_scores: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in want}
    market_scores: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        scored = item_score(item, now=now)
        if scored is None:
            continue
        market_scores.append(scored)
        hit = _item_symbols(item) & want
        for symbol in hit:
            per_symbol_scores[symbol].append(scored)

    result: dict[str, Any] = {
        "version": NEWS_SENTIMENT_VERSION,
        "available": bool(market_scores),
        "items_scored": len(market_scores),
        "symbols": {},
        "market": _aggregate(market_scores),
    }
    for symbol in want:
        own = _aggregate(per_symbol_scores[symbol])
        market = _aggregate(market_scores)
        # 标的当日无专属新闻时，按 0.6 权重折算大盘情绪
        if own["sentiment"] is None and market["sentiment"] is not None:
            own = {
                **own,
                "sentiment": round(market["sentiment"] * MARKET_FALLBACK_WEIGHT, 3),
                "positive": market["positive"],
                "negative": market["negative"],
                "fallback_to_market": True,
            }
        else:
            own["fallback_to_market"] = False
        result["symbols"][symbol] = own
    return result


def append_sentiment_log(
    log_path: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> int:
    """把当日情绪落盘（按 date+symbol 去重），供未来回测积累样本。"""
    now = now or datetime.now(timezone(timedelta(hours=8)))
    seen: set[tuple[str, str]] = set()
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                seen.add((str(obj.get("date")), str(obj.get("symbol"))))
            except ValueError:
                continue
    appended = 0
    lines: list[str] = []
    for row in rows:
        key = (str(row.get("date")), str(row.get("symbol")))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        record = {
            "logged_at": now.isoformat(timespec="seconds"),
            "version": NEWS_SENTIMENT_VERSION,
            **row,
        }
        lines.append(json.dumps(record, ensure_ascii=False))
        appended += 1
    if lines:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return appended
