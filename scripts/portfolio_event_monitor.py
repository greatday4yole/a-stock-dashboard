#!/usr/bin/env python3
"""Rule-based, fail-closed event monitor for the local A-share portfolio.

The monitor deliberately consumes JSON produced by existing quote/news collectors.  It
does not fetch data, send notifications, or place orders.  All alert-generation
functions accept an explicit ``now`` so the evidence and time rules are reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTFOLIO = ROOT / "config" / "portfolio.json"
DEFAULT_POLICY = ROOT / "config" / "monitoring-policy.json"
DEFAULT_NEWS = ROOT / "data" / "dashboard-news-cache.json"
DEFAULT_OUTPUT = ROOT / "data" / "event-alerts.json"

SEVERITIES = ("info", "low", "medium", "high", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def parse_datetime(value: Any) -> datetime | None:
    """Parse common API timestamps and normalize them to Asia/Shanghai (+08:00)."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        if number > 10_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if re.fullmatch(r"\d{14}", raw):
            try:
                parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
            except ValueError:
                return None
        else:
            candidate = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                parsed = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def iso_time(value: datetime) -> str:
    return value.astimezone(CN_TZ).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = re.fullmatch(r"(\d{6})[.\-]?(SH|SZ|BJ)", raw)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    if re.fullmatch(r"\d{6}", raw):
        exchange = "SH" if raw.startswith(("5", "6", "9")) else "BJ" if raw.startswith(("4", "8")) else "SZ"
        return f"{raw}.{exchange}"
    return raw


def _severity_at_least(first: str, second: str) -> str:
    return first if SEVERITY_RANK.get(first, 0) >= SEVERITY_RANK.get(second, 0) else second


def _raise_severity(severity: str, steps: int) -> str:
    rank = min(len(SEVERITIES) - 1, SEVERITY_RANK.get(severity, 0) + max(0, steps))
    return SEVERITIES[rank]


def _cap_severity(severity: str, maximum: str) -> str:
    rank = min(SEVERITY_RANK.get(severity, 0), SEVERITY_RANK.get(maximum, len(SEVERITIES) - 1))
    return SEVERITIES[rank]


def _source_metadata(item: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[str, float]:
    source = str(item.get("source") or item.get("data_source") or "未知来源").strip()
    overrides = policy.get("source_name_overrides") or {}
    override = overrides.get(source) if isinstance(overrides, Mapping) else None
    if isinstance(override, Mapping):
        return str(override.get("kind") or "unknown"), float(override.get("confidence") or 0.5)

    explicit = str(item.get("source_kind") or "").strip().lower()
    source_id = str(item.get("source_id") or "").strip().lower()
    if not explicit:
        if source_id == "cninfo" or "巨潮" in source:
            explicit = "official"
        elif any(marker in source for marker in ("证券交易所", "上交所", "深交所", "北交所")):
            explicit = "exchange"
        elif "证监会" in source:
            explicit = "regulator"
        elif source_id == "sina" and "新闻" in source:
            explicit = "wire"
        elif source_id == "eastmoney":
            explicit = "media"
        elif any(marker in source for marker in ("腾讯财经", "新浪财经", "行情")):
            explicit = "market_feed"
        else:
            explicit = "unknown"
    confidences = policy.get("source_confidence") or {}
    confidence = _number(confidences.get(explicit) if isinstance(confidences, Mapping) else None)
    return explicit, float(confidence if confidence is not None else 0.5)


def _stable_id(scope: str, category: str, affected_symbols: Sequence[str], seed: str) -> str:
    raw = "|".join((scope, category, ",".join(sorted(set(affected_symbols))), seed))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def make_alert(
    *,
    scope: str,
    severity: str,
    category: str,
    title: str,
    evidence_time: datetime | str | None,
    observed_at: datetime,
    source: str,
    confidence: float,
    affected_symbols: Sequence[str],
    recommended_action: str,
    seed: str,
    **extra: Any,
) -> dict[str, Any]:
    symbols = sorted({_normalize_symbol(value) for value in affected_symbols if str(value or "").strip()})
    evidence = parse_datetime(evidence_time)
    alert = {
        "id": _stable_id(scope, category, symbols, seed),
        "scope": scope,
        "severity": severity if severity in SEVERITY_RANK else "medium",
        "category": category,
        "title": str(title).strip(),
        "evidence_time": iso_time(evidence) if evidence else "",
        "observed_at": iso_time(observed_at),
        "source": str(source or "未知来源"),
        "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
        "affected_symbols": symbols,
        "recommended_action": str(recommended_action).strip(),
        "acknowledged": False,
    }
    alert.update({key: value for key, value in extra.items() if value is not None})
    return alert


def portfolio_positions(portfolio: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Aggregate configuration rows by security; safe for the same symbol in two accounts."""

    result: dict[str, dict[str, Any]] = {}
    for account in portfolio.get("accounts") or []:
        if not isinstance(account, Mapping):
            continue
        for row in account.get("positions") or []:
            if not isinstance(row, Mapping):
                continue
            symbol = _normalize_symbol(row.get("thscode") or row.get("symbol") or row.get("code"))
            if not symbol:
                continue
            quantity = _number(row.get("quantity")) or 0.0
            current = result.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": str(row.get("name") or symbol),
                    "quantity": 0.0,
                    "snapshot_value": 0.0,
                    "theme": str(row.get("theme") or ""),
                    "industry": str(row.get("industry") or ""),
                    "daily_limit_pct": _number(row.get("daily_limit_pct")) or 10.0,
                },
            )
            current["quantity"] += quantity
            current["snapshot_value"] += quantity * (_number(row.get("snapshot_price")) or 0.0)
    return result


def _extract_quote_rows(quotes: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    if quotes is None:
        return []
    if isinstance(quotes, Sequence) and not isinstance(quotes, (str, bytes, bytearray)):
        return [dict(row) for row in quotes if isinstance(row, Mapping)]
    if not isinstance(quotes, Mapping):
        return []
    for key in ("positions", "quotes", "items"):
        rows = quotes.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    data = quotes.get("data")
    if isinstance(data, Mapping):
        return _extract_quote_rows(data)
    return []


def _quote_row_symbol(row: Mapping[str, Any]) -> str:
    return _normalize_symbol(row.get("thscode") or row.get("symbol") or row.get("code"))


def calculate_position_weights(
    portfolio: Mapping[str, Any], quotes: Mapping[str, Any] | Sequence[Any] | None
) -> dict[str, float]:
    configured = portfolio_positions(portfolio)
    rows = {_quote_row_symbol(row): row for row in _extract_quote_rows(quotes) if _quote_row_symbol(row)}
    explicit: dict[str, float] = {}
    values: dict[str, float] = {}
    for symbol, context in configured.items():
        row = rows.get(symbol) or {}
        weight = _number(row.get("weight_pct"))
        if weight is not None and weight >= 0:
            explicit[symbol] = weight
        value = _number(row.get("market_value"))
        if value is None:
            price = _number(row.get("price"))
            value = float(context["quantity"]) * price if price is not None else float(context["snapshot_value"])
        values[symbol] = max(0.0, value)
    net_equity = None
    if isinstance(quotes, Mapping) and isinstance(quotes.get("portfolio"), Mapping):
        net_equity = _number(quotes["portfolio"].get("net_equity"))
    denominator = net_equity if net_equity and net_equity > 0 else sum(values.values())
    result = {symbol: round(value / denominator * 100, 4) if denominator > 0 else 0.0 for symbol, value in values.items()}
    result.update(explicit)
    return result


def _data_action(policy: Mapping[str, Any], key: str) -> str:
    actions = policy.get("actions") or {}
    if isinstance(actions, Mapping) and actions.get(key):
        return str(actions[key])
    return "停止基于该数据新增风险敞口；恢复数据并核验原始时间戳。"


def _age_seconds(timestamp: datetime | None, now: datetime) -> float | None:
    return (now - timestamp).total_seconds() if timestamp else None


def _is_timestamp_usable(timestamp: datetime | None, now: datetime, ttl: float, future_tolerance: float) -> bool:
    age = _age_seconds(timestamp, now)
    return age is not None and -future_tolerance <= age <= ttl


def _latest_position_quote_time(quotes: Mapping[str, Any] | Sequence[Any] | None) -> datetime | None:
    timestamps = [
        parse_datetime(row.get("quote_time") or row.get("time") or row.get("timestamp") or row.get("quote_time_raw"))
        for row in _extract_quote_rows(quotes)
    ]
    usable = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(usable) if usable else None


def account_freshness_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    quality = policy.get("data_quality") or {}
    ttl = float(quality.get("account_snapshot_max_age_hours") or 96) * 3600
    max_trading_day_lag = int(quality.get("account_snapshot_max_trading_day_lag", 0))
    as_of = parse_datetime(portfolio.get("as_of"))
    quote_time = _latest_position_quote_time(quotes)
    trading_day_lag = 0
    if as_of and quote_time and quote_time.date() > as_of.date():
        # Position quote evidence proves at least one newer A-share trading day.  A
        # holiday calendar is unnecessary for the strict zero-lag gate.
        trading_day_lag = 1
    if as_of and trading_day_lag > max_trading_day_lag:
        title = f"账户快照停留在{as_of.date().isoformat()}，行情已进入{quote_time.date().isoformat()}"
        category = "account_snapshot_trading_day_stale"
        severity = "high"
        evidence_time: datetime | None = as_of
    elif as_of and _age_seconds(as_of, now) is not None and _age_seconds(as_of, now) <= ttl:
        return []
    elif as_of:
        age_hours = max(0.0, float(_age_seconds(as_of, now) or 0) / 3600)
        title = f"账户持仓快照已过期（约{age_hours:.1f}小时）"
        category = "account_snapshot_stale"
        severity = "high"
        evidence_time: datetime | None = as_of
    else:
        title = "账户持仓快照缺少可验证时间"
        category = "account_snapshot_missing"
        severity = "critical"
        evidence_time = now
    return [
        make_alert(
            scope="data_quality",
            severity=severity,
            category=category,
            title=title,
            evidence_time=evidence_time,
            observed_at=now,
            source="持仓配置",
            confidence=1.0,
            affected_symbols=list(portfolio_positions(portfolio)),
            recommended_action="先与券商账户核对持仓数量、现金、负债和可用数量；确认前停止新增风险敞口。",
            seed=category,
            direction="negative",
            trading_day_lag=trading_day_lag,
        )
    ]


def quote_freshness_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    now: datetime,
) -> tuple[list[dict[str, Any]], set[str]]:
    configured = portfolio_positions(portfolio)
    all_symbols = set(configured)
    if quotes is None:
        return (
            [
                make_alert(
                    scope="data_quality",
                    severity="critical",
                    category="quote_data_missing",
                    title="持仓行情数据缺失",
                    evidence_time=now,
                    observed_at=now,
                    source="事件监控器",
                    confidence=1.0,
                    affected_symbols=sorted(all_symbols),
                    recommended_action=_data_action(policy, "data_missing"),
                    seed="quote-data-missing",
                    direction="negative",
                )
            ],
            set(),
        )

    rows = _extract_quote_rows(quotes)
    by_symbol = {_quote_row_symbol(row): row for row in rows if _quote_row_symbol(row) in all_symbols}
    if not by_symbol:
        return (
            [
                make_alert(
                    scope="data_quality",
                    severity="critical",
                    category="quote_data_missing",
                    title="行情请求未提供任何持仓报价；成功状态不能证明数据可用",
                    evidence_time=now,
                    observed_at=now,
                    source="事件监控器",
                    confidence=1.0,
                    affected_symbols=sorted(all_symbols),
                    recommended_action=_data_action(policy, "data_missing"),
                    seed="quote-positions-empty",
                    direction="negative",
                )
            ],
            set(),
        )

    quality = policy.get("data_quality") or {}
    state = ""
    if isinstance(quotes, Mapping) and isinstance(quotes.get("market_status"), Mapping):
        state = str(quotes["market_status"].get("state") or "").lower()
    live = state in ("open", "auction", "continuous", "trading") or not state
    ttl = float(quality.get("quote_live_max_age_seconds") or 300) if live else float(
        quality.get("quote_closed_max_age_seconds") or 259200
    )
    future_tolerance = float(quality.get("quote_future_tolerance_seconds") or 120)
    fresh_symbols: set[str] = set()
    stale_symbols: list[str] = []
    for symbol, row in by_symbol.items():
        timestamp = parse_datetime(
            row.get("quote_time") or row.get("time") or row.get("timestamp") or row.get("quote_time_raw")
        )
        stale_flag = bool(row.get("is_stale")) or str(row.get("data_source") or "") == "持仓快照"
        if not stale_flag and _is_timestamp_usable(timestamp, now, ttl, future_tolerance):
            fresh_symbols.add(symbol)
        else:
            stale_symbols.append(symbol)

    alerts: list[dict[str, Any]] = []
    missing = sorted(all_symbols - set(by_symbol))
    minimum_coverage = float(quality.get("minimum_quote_coverage_pct") or 95)
    coverage = 100 * len(by_symbol) / len(all_symbols) if all_symbols else 100.0
    if missing or coverage < minimum_coverage:
        severity = "critical" if coverage == 0 else "high"
        alerts.append(
            make_alert(
                scope="data_quality",
                severity=severity,
                category="quote_coverage_incomplete",
                title=f"持仓报价覆盖不足：{len(by_symbol)}/{len(all_symbols)}",
                evidence_time=now,
                observed_at=now,
                source="事件监控器",
                confidence=1.0,
                affected_symbols=missing,
                recommended_action=_data_action(policy, "data_missing"),
                seed="quote-coverage",
                direction="negative",
                coverage_pct=round(coverage, 2),
            )
        )
    if stale_symbols:
        weights = calculate_position_weights(portfolio, quotes)
        stale_weight = sum(weights.get(symbol, 0.0) for symbol in stale_symbols)
        severity = "critical" if len(stale_symbols) == len(all_symbols) or stale_weight >= 50 else "high"
        evidence_times = [
            parse_datetime(
                by_symbol[symbol].get("quote_time")
                or by_symbol[symbol].get("time")
                or by_symbol[symbol].get("timestamp")
                or by_symbol[symbol].get("quote_time_raw")
            )
            for symbol in stale_symbols
        ]
        usable_times = [value for value in evidence_times if value]
        alerts.append(
            make_alert(
                scope="data_quality",
                severity=severity,
                category="quote_data_stale",
                title=f"{len(stale_symbols)}只持仓的行情时间戳缺失或过期",
                evidence_time=max(usable_times) if usable_times else now,
                observed_at=now,
                source="持仓行情",
                confidence=1.0,
                affected_symbols=stale_symbols,
                recommended_action=_data_action(policy, "data_stale"),
                seed="quote-stale",
                direction="negative",
                stale_weight_pct=round(stale_weight, 3),
            )
        )
    return alerts, fresh_symbols


def _normalize_title(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b\d{6}(?:\.(?:sh|sz|bj))?\b", "", text)
    text = re.sub(r"(?:关于|公司|股份有限公司|公告|提示性)", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _news_symbols(item: Mapping[str, Any], contexts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    direct = _normalize_symbol(item.get("thscode") or item.get("symbol") or item.get("code"))
    if direct in contexts:
        return [direct]
    haystack = " ".join(str(item.get(key) or "") for key in ("title", "summary", "content"))
    matched = []
    for symbol, context in contexts.items():
        code = symbol[:6]
        name = str(context.get("name") or "")
        if code in haystack or (name and name in haystack):
            matched.append(symbol)
    return sorted(set(matched))


def _match_news_rule(text: str, policy: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, list[str]]:
    for rule in policy.get("news_rules") or []:
        if not isinstance(rule, Mapping):
            continue
        matched = [str(word) for word in rule.get("keywords") or [] if str(word) and str(word).lower() in text]
        if matched:
            return rule, matched
    return None, []


def _news_candidates_equivalent(
    first: Mapping[str, Any], second: Mapping[str, Any], policy: Mapping[str, Any]
) -> bool:
    if first.get("source_event_id") and first.get("source_event_id") == second.get("source_event_id"):
        return True
    if first.get("category") != second.get("category") or set(first.get("symbols") or []) != set(second.get("symbols") or []):
        return False
    first_time = first.get("event_time")
    second_time = second.get("event_time")
    if not isinstance(first_time, datetime) or not isinstance(second_time, datetime):
        return False
    config = policy.get("news_deduplication") or {}
    max_gap = float(config.get("maximum_event_time_gap_hours") or 72) * 3600
    if abs((first_time - second_time).total_seconds()) > max_gap:
        return False
    first_title = _normalize_title(first.get("title"))
    second_title = _normalize_title(second.get("title"))
    if not first_title or not second_title:
        return False
    if first_title in second_title or second_title in first_title:
        return True
    similarity = SequenceMatcher(None, first_title, second_title).ratio()
    return similarity >= float(config.get("minimum_title_similarity") or 0.68)


def _official_rank(kind: str) -> int:
    return {"regulator": 5, "exchange": 5, "official": 5, "wire": 3, "media": 2, "research": 2, "social": 0}.get(kind, 1)


def _adjust_for_weight(
    severity: str, direction: str, weight: float, policy: Mapping[str, Any]
) -> str:
    if direction != "negative":
        return severity
    config = policy.get("portfolio_priority") or {}
    if weight >= float(config.get("critical_weight_pct") or 15):
        return _raise_severity(severity, int(config.get("negative_event_severity_steps_at_critical") or 2))
    if weight >= float(config.get("elevated_weight_pct") or 8):
        return _raise_severity(severity, int(config.get("negative_event_severity_steps_at_elevated") or 1))
    return severity


def news_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    news: Mapping[str, Any] | Sequence[Any] | None,
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    contexts = portfolio_positions(portfolio)
    all_symbols = list(contexts)
    if news is None:
        return [
            make_alert(
                scope="data_quality",
                severity="critical",
                category="news_data_missing",
                title="持仓新闻与公告数据缺失",
                evidence_time=now,
                observed_at=now,
                source="事件监控器",
                confidence=1.0,
                affected_symbols=all_symbols,
                recommended_action=_data_action(policy, "data_missing"),
                seed="news-data-missing",
                direction="negative",
            )
        ]

    if isinstance(news, Sequence) and not isinstance(news, (str, bytes, bytearray)):
        items = [dict(item) for item in news if isinstance(item, Mapping)]
        last_success = None
        source_status: Mapping[str, Any] = {}
    elif isinstance(news, Mapping):
        items = [dict(item) for item in (news.get("items") or news.get("news") or []) if isinstance(item, Mapping)]
        # Deliberately do not use last_attempt_at/generated_at: request success is not data freshness.
        last_success = parse_datetime(news.get("last_success_at"))
        source_status = news.get("source_status") if isinstance(news.get("source_status"), Mapping) else {}
    else:
        items = []
        last_success = None
        source_status = {}

    quality = policy.get("data_quality") or {}
    feed_ttl = float(quality.get("news_feed_max_age_seconds") or 21600)
    alerts: list[dict[str, Any]] = []
    if last_success is None or not _is_timestamp_usable(last_success, now, feed_ttl, 120):
        alerts.append(
            make_alert(
                scope="data_quality",
                severity="high" if items else "critical",
                category="news_data_stale" if last_success else "news_success_time_missing",
                title="新闻源最近成功取数时间缺失或过期；请求成功状态不视为新鲜数据",
                evidence_time=last_success or now,
                observed_at=now,
                source="新闻聚合",
                confidence=1.0,
                affected_symbols=all_symbols,
                recommended_action=_data_action(policy, "data_stale"),
                seed="news-feed-stale",
                direction="negative",
            )
        )
    if not items:
        alerts.append(
            make_alert(
                scope="data_quality",
                severity="critical",
                category="news_items_empty",
                title="新闻请求没有可验证条目，不能据此判定持仓无事件",
                evidence_time=last_success or now,
                observed_at=now,
                source="新闻聚合",
                confidence=1.0,
                affected_symbols=all_symbols,
                recommended_action=_data_action(policy, "data_missing"),
                seed="news-items-empty",
                direction="negative",
            )
        )

    official_status = source_status.get("cninfo") if isinstance(source_status, Mapping) else None
    if isinstance(official_status, Mapping) and official_status.get("ok") is False:
        alerts.append(
            make_alert(
                scope="data_quality",
                severity="high",
                category="official_news_source_degraded",
                title="巨潮官方公告源异常，媒体新闻不能替代官方核验",
                evidence_time=last_success or now,
                observed_at=now,
                source="巨潮资讯",
                confidence=1.0,
                affected_symbols=all_symbols,
                recommended_action="恢复巨潮公告源或直接到交易所核验；官方源恢复前停止依据媒体标题新增风险敞口。",
                seed="official-source-degraded",
                direction="negative",
            )
        )

    weights = calculate_position_weights(portfolio, quotes)
    event_max_age = float(quality.get("news_event_max_age_days") or 14) * 86400
    candidates: list[dict[str, Any]] = []
    malformed_times = 0
    for item in items:
        event_time = parse_datetime(
            item.get("evidence_time") or item.get("time") or item.get("publish_time") or item.get("datetime")
        )
        if event_time is None or not _is_timestamp_usable(event_time, now, event_max_age, 300):
            if event_time is None:
                malformed_times += 1
            continue
        text = " ".join(str(item.get(key) or "") for key in ("title", "summary", "content")).lower()
        rule, matched = _match_news_rule(text, policy)
        if not rule:
            continue
        symbols = _news_symbols(item, contexts)
        if not symbols:
            continue
        source_kind, source_confidence = _source_metadata(item, policy)
        candidates.append(
            {
                "item": item,
                "event_time": event_time,
                "title": str(item.get("title") or "未命名事件"),
                "category": str(rule.get("category") or "portfolio_news"),
                "direction": str(rule.get("direction") or "neutral"),
                "base_severity": str(rule.get("base_severity") or "medium"),
                "recommended_action": str(rule.get("recommended_action") or "核验原始信息后再评估。"),
                "matched_keywords": matched,
                "symbols": symbols,
                "source": str(item.get("source") or "未知来源"),
                "source_kind": source_kind,
                "source_confidence": source_confidence,
                "source_event_id": str(item.get("id") or ""),
            }
        )

    # Official/regulator/exchange candidates are visited first and win semantic duplicates.
    candidates.sort(
        key=lambda row: (_official_rank(str(row["source_kind"])), float(row["source_confidence"]), row["event_time"]),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(_news_candidates_equivalent(candidate, existing, policy) for existing in selected):
            continue
        selected.append(candidate)

    for candidate in selected:
        symbols = candidate["symbols"]
        weight = sum(weights.get(symbol, 0.0) for symbol in symbols)
        severity = _adjust_for_weight(
            candidate["base_severity"], candidate["direction"], weight, policy
        )
        if candidate["direction"] == "negative" and _official_rank(candidate["source_kind"]) < 5:
            maximum = str(
                (policy.get("portfolio_priority") or {}).get("unverified_negative_max_severity")
                or "high"
            )
            severity = _cap_severity(severity, maximum)
        age = max(0.0, float(_age_seconds(candidate["event_time"], now) or 0))
        recency_factor = max(0.75, 1 - 0.25 * age / max(1.0, event_max_age))
        confidence = float(candidate["source_confidence"]) * recency_factor
        priority_score = (
            SEVERITY_RANK.get(severity, 0) * 20
            + min(weight, 30) * 0.7
            + _official_rank(candidate["source_kind"]) * 2
            + confidence * 10
        )
        normalized_title = _normalize_title(candidate["title"])
        seed = "|".join(
            (
                candidate["category"],
                candidate["event_time"].date().isoformat(),
                normalized_title,
            )
        )
        item = candidate["item"]
        alerts.append(
            make_alert(
                scope="position",
                severity=severity,
                category=candidate["category"],
                title=candidate["title"],
                evidence_time=candidate["event_time"],
                observed_at=now,
                source=candidate["source"],
                confidence=confidence,
                affected_symbols=symbols,
                recommended_action=candidate["recommended_action"],
                seed=seed,
                direction=candidate["direction"],
                source_kind=candidate["source_kind"],
                matched_keywords=candidate["matched_keywords"],
                position_weight_pct=round(weight, 3),
                priority_score=round(priority_score, 2),
                evidence_url=str(item.get("url") or ""),
                evidence_summary=str(item.get("summary") or "")[:500],
            )
        )
    if malformed_times:
        alerts.append(
            make_alert(
                scope="data_quality",
                severity="medium",
                category="news_event_time_missing",
                title=f"{malformed_times}条新闻缺少可验证事件时间，已排除出信号",
                evidence_time=last_success or now,
                observed_at=now,
                source="新闻聚合",
                confidence=1.0,
                affected_symbols=all_symbols,
                recommended_action="修复逐条新闻的发布时间字段；在此之前不要把这些条目解释为最新事件。",
                seed="news-event-time-missing",
                direction="negative",
            )
        )
    return alerts


def price_anomaly_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    fresh_symbols: set[str],
    now: datetime,
) -> list[dict[str, Any]]:
    if quotes is None:
        return []
    contexts = portfolio_positions(portfolio)
    weights = calculate_position_weights(portfolio, quotes)
    config = policy.get("price_anomaly") or {}
    large = float(config.get("large_abs_change_pct") or 5)
    extreme = float(config.get("extreme_abs_change_pct") or 9)
    amplitude_limit = float(config.get("large_intraday_amplitude_pct") or 8)
    proximity = float(config.get("daily_limit_proximity_ratio") or 0.92)
    actions = policy.get("actions") or {}
    alerts: list[dict[str, Any]] = []
    for row in _extract_quote_rows(quotes):
        symbol = _quote_row_symbol(row)
        if symbol not in contexts or symbol not in fresh_symbols:
            continue
        change = _number(row.get("change_pct") if row.get("change_pct") is not None else row.get("pct_change"))
        prev_close = _number(row.get("prev_close"))
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        amplitude = ((high - low) / prev_close * 100) if high is not None and low is not None and prev_close and prev_close > 0 else None
        daily_limit = float(contexts[symbol].get("daily_limit_pct") or 10)
        near_limit = change is not None and abs(change) >= daily_limit * proximity
        large_move = change is not None and abs(change) >= large
        large_amplitude = amplitude is not None and amplitude >= amplitude_limit
        if not (large_move or large_amplitude or near_limit):
            continue
        direction = "negative" if change is not None and change < 0 else "positive" if change is not None and change > 0 else "neutral"
        severity = "medium"
        if change is not None and abs(change) >= extreme:
            severity = "high"
        if near_limit:
            severity = "critical" if direction == "negative" else "high"
        weight = weights.get(symbol, 0.0)
        severity = _adjust_for_weight(severity, direction, weight, policy)
        reasons = []
        if large_move:
            reasons.append(f"涨跌幅{change:+.2f}%")
        if large_amplitude:
            reasons.append(f"振幅{amplitude:.2f}%")
        if near_limit:
            reasons.append("接近日涨跌幅限制")
        timestamp = parse_datetime(
            row.get("quote_time") or row.get("time") or row.get("timestamp") or row.get("quote_time_raw")
        )
        source_kind, source_confidence = _source_metadata(row, policy)
        name = str(row.get("name") or contexts[symbol].get("name") or symbol)
        movement = "异动" if change is None else "上涨" if change > 0 else "下跌"
        title = f"{name}{movement}{abs(change):.2f}%：价格异常" if change is not None else f"{name}盘中振幅异常"
        alerts.append(
            make_alert(
                scope="position",
                severity=severity,
                category="price_anomaly",
                title=title,
                evidence_time=timestamp,
                observed_at=now,
                source=str(row.get("data_source") or row.get("source") or "持仓行情"),
                confidence=source_confidence,
                affected_symbols=[symbol],
                recommended_action=str(
                    actions.get("price_down" if direction == "negative" else "price_up")
                    or "核验价格、公告与成交信息后再评估。"
                ),
                seed=f"{symbol}|{timestamp.date().isoformat() if timestamp else now.date().isoformat()}|{direction}",
                direction=direction,
                source_kind=source_kind,
                change_pct=change,
                amplitude_pct=round(amplitude, 3) if amplitude is not None else None,
                triggers=reasons,
                position_weight_pct=round(weight, 3),
                priority_score=round(SEVERITY_RANK.get(severity, 0) * 20 + min(weight, 30), 2),
            )
        )
    return alerts


def _normalize_alias(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _macro_rows(macro: Mapping[str, Any] | Sequence[Any] | None) -> list[dict[str, Any]]:
    if macro is None:
        return []
    if isinstance(macro, Sequence) and not isinstance(macro, (str, bytes, bytearray)):
        return [dict(row) for row in macro if isinstance(row, Mapping)]
    if not isinstance(macro, Mapping):
        return []
    for key in ("observations", "items", "data"):
        rows = macro.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    rows = []
    ignored = {"generated_at", "last_success_at", "source", "ok", "errors"}
    for key, value in macro.items():
        if key in ignored:
            continue
        if isinstance(value, Mapping):
            rows.append({"key": key, **dict(value)})
        elif _number(value) is not None:
            rows.append({"key": key, "value": value})
    return rows


def _classify_macro(row: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    candidates = {
        _normalize_alias(row.get("key")),
        _normalize_alias(row.get("symbol")),
        _normalize_alias(row.get("name")),
        _normalize_alias(row.get("label")),
    }
    candidates.discard("")
    for rule in policy.get("macro_rules") or []:
        if not isinstance(rule, Mapping):
            continue
        aliases = {_normalize_alias(rule.get("key")), *(_normalize_alias(value) for value in rule.get("aliases") or [])}
        if candidates & aliases:
            return str(rule.get("key") or "")
    return ""


def _macro_affected_symbols(key: str, contexts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    if key in ("sp500_futures", "nasdaq_futures"):
        selected = [symbol for symbol, row in contexts.items() if row.get("theme") == "high_beta_tech"]
        return selected or list(contexts)
    if key == "crude_oil":
        selected = [
            symbol
            for symbol, row in contexts.items()
            if row.get("theme") in ("property_transport", "industrial_environmental", "utilities")
        ]
        return selected or list(contexts)
    return list(contexts)


def _compare(value: float, operator: str, threshold: float) -> bool:
    if operator == ">=":
        return value >= threshold
    if operator == ">":
        return value > threshold
    if operator == "<=":
        return value <= threshold
    if operator == "<":
        return value < threshold
    if operator == "==":
        return value == threshold
    return False


def _intelligence_affected_symbols(
    signal: Mapping[str, Any], contexts: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    related = {
        str(value).strip().casefold()
        for value in signal.get("related_themes") or []
        if str(value).strip()
    }
    if not related:
        return []
    if "broad_market" in related:
        return list(contexts)
    selected = []
    for symbol, row in contexts.items():
        tags = {
            str(row.get("theme") or "").strip().casefold(),
            str(row.get("industry") or "").strip().casefold(),
        }
        if related.intersection(tags):
            selected.append(symbol)
    return selected


def global_intelligence_alerts(
    portfolio: Mapping[str, Any],
    market_news: Mapping[str, Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Convert fresh, threshold-crossing global intelligence into portfolio alerts."""

    if not isinstance(market_news, Mapping):
        return []
    intelligence = market_news.get("intelligence")
    if not isinstance(intelligence, Mapping):
        return []
    contexts = portfolio_positions(portfolio)
    alerts: list[dict[str, Any]] = []
    for raw in intelligence.get("signals") or []:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "").lower()
        strength = str(raw.get("signal_strength") or "").lower()
        if status not in {"available", "degraded"} or strength not in {"medium", "high"}:
            continue
        freshness = raw.get("freshness") if isinstance(raw.get("freshness"), Mapping) else {}
        freshness_status = str(freshness.get("status") or "").lower()
        if raw.get("stale") is True or freshness_status in {"stale", "future", "missing", "unavailable"}:
            continue
        topic = str(raw.get("topic") or "").strip()
        metric = raw.get("metric") if isinstance(raw.get("metric"), Mapping) else {}
        evidence_time = parse_datetime(raw.get("evidence_time"))
        if topic == "index_futures_expiry":
            days_until = _number(metric.get("days_until"))
            if days_until is None or not 0 <= days_until <= 5:
                continue
        elif topic == "federal_reserve" and _number(metric.get("days_until")) is not None:
            days_until = float(_number(metric.get("days_until")) or 0)
            if not 0 <= days_until <= 5:
                continue
        else:
            if evidence_time is None:
                continue
            max_age = _number(freshness.get("max_age_seconds")) or 7 * 86400
            if not _is_timestamp_usable(evidence_time, now, max_age, 300):
                continue
        affected = _intelligence_affected_symbols(raw, contexts)
        if not affected:
            continue
        direction = str(raw.get("direction") or "neutral").lower()
        if direction in {"negative", "mixed"}:
            severity = "high" if strength == "high" else "medium"
        else:
            severity = "medium" if strength == "high" else "low"
        relation_score = int(_number(raw.get("relation_score")) or 0)
        confidence = float(_number(raw.get("confidence")) or 0)
        label = str(raw.get("label") or topic or "全球风险情报")
        forecast = str(raw.get("forecast") or "达到监控阈值，需人工核验传导路径。")
        action = (
            "仅按波动窗口管理仓位与流动性，不据此预测方向或追涨杀跌。"
            if topic == "index_futures_expiry"
            else f"{forecast} 先核验原始来源与A股盘面是否同向，再调整仓位。"
        )
        evidence_rows = [row for row in raw.get("evidence") or [] if isinstance(row, Mapping)]
        alerts.append(
            make_alert(
                scope="macro",
                severity=severity,
                category=f"global_intelligence_{topic}",
                title=f"{label}达到{strength}关注阈值",
                evidence_time=evidence_time or now,
                observed_at=now,
                source=str(raw.get("source") or "全球情报聚合"),
                confidence=confidence,
                affected_symbols=affected,
                recommended_action=action,
                seed=str(raw.get("id") or f"{topic}|{raw.get('evidence_time') or ''}"),
                direction=direction,
                source_kind=str(raw.get("source_kind") or "unknown"),
                intelligence_topic=topic,
                intelligence_status=status,
                fact_kind=str(raw.get("fact_kind") or ""),
                relation_score=relation_score,
                relation_method=str(raw.get("relation_method") or "rule_based_prior_v1"),
                signal_strength=strength,
                metric=dict(metric),
                threshold=str(raw.get("threshold") or ""),
                forecast=forecast,
                limitations=[str(value) for value in raw.get("limitations") or []][:4],
                evidence_url=str((evidence_rows[0] if evidence_rows else {}).get("url") or ""),
                priority_score=round(
                    SEVERITY_RANK.get(severity, 0) * 20
                    + relation_score * 0.2
                    + confidence * 10,
                    2,
                ),
            )
        )
    return alerts


def macro_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    macro: Mapping[str, Any] | Sequence[Any] | None,
    now: datetime,
) -> list[dict[str, Any]]:
    contexts = portfolio_positions(portfolio)
    observations = _macro_rows(macro)
    if isinstance(quotes, Mapping):
        markets = quotes.get("markets")
        if isinstance(markets, Mapping):
            observations.extend(_macro_rows(markets.get("global")))

    classified: dict[str, list[dict[str, Any]]] = {}
    for row in observations:
        key = _classify_macro(row, policy)
        if key:
            normalized = dict(row)
            normalized["_event_time"] = parse_datetime(
                row.get("evidence_time")
                or row.get("quote_time")
                or row.get("time")
                or row.get("timestamp")
                or row.get("observed_at")
            )
            classified.setdefault(key, []).append(normalized)

    quality = policy.get("data_quality") or {}
    ttl = float(quality.get("macro_max_age_seconds") or 900)
    future_tolerance = float(quality.get("macro_future_tolerance_seconds") or 120)
    alerts: list[dict[str, Any]] = []
    required_rules = [rule for rule in policy.get("macro_rules") or [] if isinstance(rule, Mapping) and rule.get("required")]
    missing = [str(rule.get("key")) for rule in required_rules if str(rule.get("key")) not in classified]
    if missing:
        alerts.append(
            make_alert(
                scope="data_quality",
                severity="critical" if len(missing) == len(required_rules) else "high",
                category="macro_data_missing",
                title=f"关键宏观观测缺失：{', '.join(missing)}",
                evidence_time=now,
                observed_at=now,
                source="宏观行情",
                confidence=1.0,
                affected_symbols=list(contexts),
                recommended_action=_data_action(policy, "data_missing"),
                seed="macro-missing|" + ",".join(sorted(missing)),
                direction="negative",
                missing_observations=missing,
            )
        )

    rule_by_key = {str(rule.get("key")): rule for rule in policy.get("macro_rules") or [] if isinstance(rule, Mapping)}
    for key, rows in classified.items():
        # A usable timestamp wins over a newer-but-invalid row; recency and source
        # confidence then select the best available fallback.
        rows.sort(
            key=lambda row: (
                _is_timestamp_usable(
                    row.get("_event_time") if isinstance(row.get("_event_time"), datetime) else None,
                    now,
                    ttl,
                    future_tolerance,
                ),
                row.get("_event_time") or datetime.min.replace(tzinfo=CN_TZ),
                _source_metadata(row, policy)[1],
            ),
            reverse=True,
        )
        row = rows[0]
        timestamp = row.get("_event_time")
        rule = rule_by_key.get(key) or {}
        affected = _macro_affected_symbols(key, contexts)
        if not _is_timestamp_usable(timestamp if isinstance(timestamp, datetime) else None, now, ttl, future_tolerance):
            alerts.append(
                make_alert(
                    scope="data_quality",
                    severity="high",
                    category="macro_data_stale",
                    title=f"{rule.get('label') or key}观测时间戳缺失或过期",
                    evidence_time=timestamp if isinstance(timestamp, datetime) else now,
                    observed_at=now,
                    source=str(row.get("source") or "宏观行情"),
                    confidence=1.0,
                    affected_symbols=affected,
                    recommended_action=_data_action(policy, "data_stale"),
                    seed=f"macro-stale|{key}",
                    direction="negative",
                    macro_key=key,
                )
            )
            continue

        source_kind, source_confidence = _source_metadata(row, policy)
        for threshold in rule.get("thresholds") or []:
            if not isinstance(threshold, Mapping):
                continue
            field = str(threshold.get("field") or "")
            value = _number(
                row.get(field)
                if row.get(field) is not None
                else row.get("price") if field == "value" else row.get("pct_change") if field == "change_pct" else None
            )
            limit = _number(threshold.get("value"))
            operator = str(threshold.get("operator") or "")
            if value is None or limit is None or not _compare(value, operator, limit):
                continue
            severity = str(threshold.get("severity") or "medium")
            alerts.append(
                make_alert(
                    scope="macro",
                    severity=severity,
                    category=f"macro_{key}",
                    title=str(threshold.get("title") or f"{rule.get('label') or key}触发阈值"),
                    evidence_time=timestamp,
                    observed_at=now,
                    source=str(row.get("source") or "宏观行情"),
                    confidence=source_confidence,
                    affected_symbols=affected,
                    recommended_action=str(rule.get("recommended_action") or "复核宏观风险传导后再评估。"),
                    seed=f"{key}|{field}|{timestamp.date().isoformat()}",
                    direction="negative",
                    source_kind=source_kind,
                    macro_key=key,
                    metric=field,
                    observed_value=value,
                    operator=operator,
                    threshold=limit,
                    priority_score=round(SEVERITY_RANK.get(severity, 0) * 20 + source_confidence * 10, 2),
                )
            )
            break
    return alerts


def _alerts_equivalent(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    if str(first.get("id") or "") == str(second.get("id") or ""):
        return True
    if first.get("scope") != second.get("scope") or first.get("category") != second.get("category"):
        return False
    if set(first.get("affected_symbols") or []) != set(second.get("affected_symbols") or []):
        return False
    first_time = parse_datetime(first.get("evidence_time"))
    second_time = parse_datetime(second.get("evidence_time"))
    if first_time and second_time and first_time.date() != second_time.date():
        return False
    if str(first.get("scope")) in ("data_quality", "macro"):
        return True
    first_title = _normalize_title(first.get("title"))
    second_title = _normalize_title(second.get("title"))
    return bool(first_title and second_title and SequenceMatcher(None, first_title, second_title).ratio() >= 0.68)


def _prefer_alert(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    def rank(alert: Mapping[str, Any]) -> tuple[int, int, float, datetime]:
        kind = str(alert.get("source_kind") or "unknown")
        return (
            _official_rank(kind),
            SEVERITY_RANK.get(str(alert.get("severity")), 0),
            float(_number(alert.get("confidence")) or 0),
            parse_datetime(alert.get("evidence_time")) or datetime.min.replace(tzinfo=CN_TZ),
        )

    return first if rank(first) >= rank(second) else second


def deduplicate_alerts(alerts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in alerts:
        candidate = dict(raw)
        duplicate_index = next((index for index, current in enumerate(result) if _alerts_equivalent(candidate, current)), None)
        if duplicate_index is None:
            result.append(candidate)
        else:
            result[duplicate_index] = _prefer_alert(result[duplicate_index], candidate)
    return result


def merge_with_previous(
    current_alerts: Iterable[Mapping[str, Any]], previous_payload: Mapping[str, Any] | None, now: datetime
) -> list[dict[str, Any]]:
    """Merge a current snapshot, preserving acknowledgement and first-seen metadata."""

    previous = [dict(alert) for alert in (previous_payload or {}).get("alerts") or [] if isinstance(alert, Mapping)]
    merged: list[dict[str, Any]] = []
    for current in deduplicate_alerts(current_alerts):
        old = next((alert for alert in previous if _alerts_equivalent(current, alert)), None)
        item = dict(current)
        if old:
            preferred = _prefer_alert(dict(old), item)
            if preferred is old:
                # Keep fresher observation metadata even when the old official evidence wins.
                preferred = {**item, **old}
            item = dict(preferred)
            item["id"] = str(old.get("id") or item["id"])
            item["acknowledged"] = bool(old.get("acknowledged", False))
            item["first_seen_at"] = str(old.get("first_seen_at") or old.get("observed_at") or iso_time(now))
            item["occurrences"] = int(_number(old.get("occurrences")) or 1) + 1
        else:
            item["first_seen_at"] = iso_time(now)
            item["occurrences"] = 1
        item["last_seen_at"] = iso_time(now)
        item["observed_at"] = iso_time(now)
        merged.append(item)
    merged.sort(
        key=lambda alert: (
            SEVERITY_RANK.get(str(alert.get("severity")), 0),
            float(_number(alert.get("priority_score")) or 0),
            parse_datetime(alert.get("evidence_time")) or datetime.min.replace(tzinfo=CN_TZ),
        ),
        reverse=True,
    )
    return merged


def generate_alerts(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    news: Mapping[str, Any] | Sequence[Any] | None,
    macro: Mapping[str, Any] | Sequence[Any] | None = None,
    market_news: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Pure rule evaluation: no file, network, notification, or trading side effects."""

    observed_at = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    quote_health, fresh_symbols = quote_freshness_alerts(portfolio, policy, quotes, observed_at)
    alerts = [
        *account_freshness_alerts(portfolio, policy, quotes, observed_at),
        *quote_health,
        *news_alerts(portfolio, policy, news, quotes, observed_at),
        *price_anomaly_alerts(portfolio, policy, quotes, fresh_symbols, observed_at),
        *macro_alerts(portfolio, policy, quotes, macro, observed_at),
        *global_intelligence_alerts(portfolio, market_news, observed_at),
    ]
    return deduplicate_alerts(alerts)


def build_snapshot(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    quotes: Mapping[str, Any] | Sequence[Any] | None,
    news: Mapping[str, Any] | Sequence[Any] | None,
    macro: Mapping[str, Any] | Sequence[Any] | None = None,
    market_news: Mapping[str, Any] | None = None,
    previous_payload: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    input_errors: Sequence[str] = (),
) -> dict[str, Any]:
    observed_at = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    alerts = merge_with_previous(
        generate_alerts(
            portfolio,
            policy,
            quotes=quotes,
            news=news,
            macro=macro,
            market_news=market_news,
            now=observed_at,
        ),
        previous_payload,
        observed_at,
    )
    counts = {severity: sum(1 for alert in alerts if alert.get("severity") == severity) for severity in SEVERITIES}
    if counts["critical"]:
        state = "red"
    elif counts["high"]:
        state = "orange"
    elif counts["medium"]:
        state = "yellow"
    else:
        state = "green"
    return {
        "schema_version": 1,
        "generated_at": iso_time(observed_at),
        "portfolio_id": str(portfolio.get("portfolio_id") or ""),
        "policy_version": str(policy.get("policy_version") or ""),
        "risk_state": state,
        "alert_count": len(alerts),
        "counts_by_severity": counts,
        "input_errors": list(input_errors),
        "notifications_sent": 0,
        "notes": [
            "告警用于人工核验和风险控制，不保证收益，也不自动下单。",
            "数据缺失或过期时按 fail-closed 原则冻结基于该数据的新增风险。",
        ],
        "alerts": alerts,
    }


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_optional(path: Path | None, label: str) -> tuple[Any, str | None]:
    if path is None:
        return None, None
    try:
        return _read_json(path), None
    except Exception as exc:  # fail closed through None, but record the operational cause
        return None, f"{label}: {type(exc).__name__}: {exc}"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _parse_now(value: str | None) -> datetime:
    parsed = parse_datetime(value) if value else datetime.now(CN_TZ)
    if parsed is None:
        raise ValueError(f"无法解析 --now: {value}")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成持仓、新闻、行情与宏观事件告警快照")
    parser.add_argument("--portfolio-file", type=Path, default=DEFAULT_PORTFOLIO)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--quotes-file", type=Path, help="/api/quotes 等价 JSON；缺失时明确触发 fail-closed")
    parser.add_argument("--news-file", type=Path, default=DEFAULT_NEWS, help="/api/news 等价 JSON")
    parser.add_argument("--macro-file", type=Path, help="可选宏观 observation JSON；也会读取 quotes.markets.global")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--now", help="测试/回放时间（ISO 8601），默认当前上海时间")
    args = parser.parse_args(argv)

    portfolio = _read_json(args.portfolio_file)
    policy = _read_json(args.policy_file)
    quotes, quote_error = _read_optional(args.quotes_file, "quotes")
    news, news_error = _read_optional(args.news_file, "news")
    macro, macro_error = _read_optional(args.macro_file, "macro")
    previous, previous_error = _read_optional(args.output if args.output.exists() else None, "previous-output")
    errors = [error for error in (quote_error, news_error, macro_error, previous_error) if error]
    snapshot = build_snapshot(
        portfolio,
        policy,
        quotes=quotes,
        news=news,
        macro=macro,
        previous_payload=previous if isinstance(previous, Mapping) else None,
        now=_parse_now(args.now),
        input_errors=errors,
    )
    _atomic_write_json(args.output, snapshot)
    counts = snapshot["counts_by_severity"]
    print(
        f"event monitor: state={snapshot['risk_state']} alerts={snapshot['alert_count']} "
        f"critical={counts['critical']} high={counts['high']} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
