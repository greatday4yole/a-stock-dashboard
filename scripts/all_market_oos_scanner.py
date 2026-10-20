#!/usr/bin/env python3
"""Fail-closed A-share cross-sectional scan with a chronological holdout.

The scanner is deliberately research-only.  It reads the hithink-finance
DuckDB through the vendor CLI, computes every signal from information available
at the signal close, enters on the next session open, charges a round-trip cost,
and reserves the last observations as an untouched out-of-sample holdout.

No candidate is published unless the local database, sample size, execution
coverage, and predeclared out-of-sample gates all pass.  A passing historical
test is evidence about this fixed rule on this sample, never a return promise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = ROOT / "node_modules" / ".bin" / "hithink-finance"
DEFAULT_OUTPUT = ROOT / "data" / "all-market-oos-scan.json"
PRIVATE_NODE_BIN = ROOT / "data" / "runtime" / "node" / "bin"
CN_TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = "all-market-oos-scan.v2"
ENGINE_VERSION = "2026-08-31.3"
MIN_SUPPORTED_NODE = (22, 12, 0)


class ScanError(RuntimeError):
    """Expected fail-closed scanner error carrying a stable reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScanConfig:
    expected_through: str
    top_k: int = 20
    candidate_count: int = 20
    holding_sessions: int = 20
    warmup_sessions: int = 120
    minimum_price: float = 3.0
    minimum_amount_20d: float = 50_000_000.0
    round_trip_cost_bps: float = 20.0
    minimum_train_periods: int = 60
    oos_periods: int = 24
    purge_periods: int = 1
    minimum_latest_universe: int = 500
    minimum_reference_universe: int = 5_000
    minimum_reference_coverage: float = 0.99
    minimum_historical_reference_coverage: float = 0.995
    minimum_exchange_reference_coverage: float = 0.95
    minimum_latest_vs_recent_p95: float = 0.99
    minimum_execution_coverage: float = 1.0
    minimum_path_coverage: float = 1.0
    maximum_oos_drawdown: float = 0.35
    daily_bar_complete_after_cn: str = "18:00"
    maximum_symbol_directory_age_minutes: int = 1_440

    @property
    def cost_rate(self) -> float:
        return self.round_trip_cost_bps / 10_000.0

    @property
    def periods_per_year(self) -> float:
        return 252.0 / float(self.holding_sessions + 1)


CliRunner = Callable[[Sequence[str]], Mapping[str, Any]]
FingerprintReader = Callable[[str], Mapping[str, Any]]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _parse_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ScanError("INVALID_EXPECTED_DATE", "expected-through must be YYYY-MM-DD") from exc


def _safe_error(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", text
    )
    return text[:800]


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def make_subprocess_runner(cli: Path, timeout_seconds: int) -> CliRunner:
    if not cli.is_file():
        raise ScanError("CLI_NOT_FOUND", f"hithink-finance CLI not found: {cli}")
    private_node = PRIVATE_NODE_BIN / "node"
    if not private_node.is_file() or not os.access(private_node, os.X_OK):
        raise ScanError(
            "PRIVATE_NODE_NOT_FOUND",
            f"verified private Node runtime not found: {private_node}",
        )
    child_environment = dict(os.environ)
    child_environment["PATH"] = (
        str(PRIVATE_NODE_BIN)
        + os.pathsep
        + child_environment.get("PATH", "")
    )

    def run(arguments: Sequence[str]) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                [str(cli), *arguments, "--format", "json"],
                cwd=str(ROOT),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=child_environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScanError("CLI_TIMEOUT", f"CLI timed out after {timeout_seconds}s") from exc
        except OSError as exc:
            raise ScanError("CLI_EXEC_FAILED", _safe_error(exc)) from exc
        if completed.returncode != 0:
            message = _safe_error(completed.stderr or completed.stdout)
            raise ScanError("CLI_NONZERO_EXIT", message or f"CLI exit {completed.returncode}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ScanError("CLI_INVALID_JSON", "CLI did not return valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            error = payload.get("error") if isinstance(payload, dict) else "invalid response"
            raise ScanError("CLI_BUSINESS_ERROR", _safe_error(error))
        return payload

    return run


def _node_tuple(version: Any) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(version or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def audit_cli(runner: CliRunner) -> dict[str, Any]:
    """Validate the current CLI contract before any database query."""

    version = runner(["version"])
    version_data = version.get("data") if isinstance(version.get("data"), Mapping) else {}
    node_version = str(version_data.get("node") or "")
    parsed_node = _node_tuple(node_version)
    capabilities_payload = runner(["capabilities"])
    capabilities = capabilities_payload.get("data")
    if not isinstance(capabilities, list):
        raise ScanError("CAPABILITIES_INVALID", "capabilities data is not an array")
    required = {
        "data.status",
        "data.validate",
        "db.query",
        "market.calendar",
        "symbol.list",
    }
    available = {
        str(row.get("id"))
        for row in capabilities
        if isinstance(row, Mapping) and row.get("id")
    }
    missing = sorted(required - available)
    if missing:
        raise ScanError("CAPABILITY_MISSING", f"missing capabilities: {', '.join(missing)}")
    for capability in sorted(required):
        schema = runner(["schema", capability])
        data = schema.get("data") if isinstance(schema.get("data"), Mapping) else {}
        if data.get("id") != capability:
            raise ScanError("SCHEMA_MISMATCH", f"schema mismatch for {capability}")
    return {
        "cli_version": version_data.get("version"),
        "node_version": node_version,
        "minimum_node_version": ".".join(str(part) for part in MIN_SUPPORTED_NODE),
        "runtime_supported": bool(parsed_node and parsed_node >= MIN_SUPPORTED_NODE),
        "required_capabilities": sorted(required),
    }


def _database_fingerprint(path_value: str) -> dict[str, Any]:
    """Return a cheap mutation fence for the DuckDB file and its WAL.

    The research rows themselves are fetched by one SELECT, so DuckDB supplies
    the actual consistent snapshot.  This fence additionally proves that the
    separately executed vendor validation covered the same on-disk release.
    """

    path = Path(path_value).expanduser().resolve(strict=True)

    def signature(candidate: Path) -> dict[str, Any] | None:
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            return None
        if not candidate.is_file():
            raise ScanError("DATABASE_PATH_INVALID", f"not a regular file: {candidate}")
        return {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    return {
        "path": str(path),
        "database": signature(path),
        "wal": signature(path.with_name(path.name + ".wal")),
    }


def _assert_same_fingerprint(
    before: Mapping[str, Any], after: Mapping[str, Any], stage: str
) -> None:
    if dict(before) != dict(after):
        raise ScanError(
            "DATABASE_CHANGED_DURING_SCAN",
            f"DuckDB changed while {stage}; retry after data synchronization finishes",
        )


def audit_database(
    runner: CliRunner, fingerprint_reader: FingerprintReader
) -> dict[str, Any]:
    status = runner(["data", "status"])
    status_data = status.get("data") if isinstance(status.get("data"), Mapping) else {}
    path = str(status_data.get("path") or "")
    if not path:
        raise ScanError("DATABASE_PATH_MISSING", "data status did not report a database path")
    before = dict(fingerprint_reader(path))
    validation = runner(["data", "validate"])
    validation_data = (
        validation.get("data") if isinstance(validation.get("data"), Mapping) else {}
    )
    issues = validation_data.get("issues")
    if validation_data.get("ok") is not True or not isinstance(issues, list) or issues:
        raise ScanError("DATABASE_VALIDATION_FAILED", "local DuckDB validation did not pass")
    after = dict(fingerprint_reader(path))
    _assert_same_fingerprint(before, after, "vendor database validation")
    return {
        "path": path,
        "schema_version": status_data.get("version"),
        "validation_ok": True,
        "validation_issues": [],
        "snapshot_fingerprint": after,
    }


def audit_reference_data(
    runner: CliRunner, config: ScanConfig, observed_at: datetime
) -> dict[str, Any]:
    """Fetch authoritative current calendar and A-share directory.

    Any remote error is allowed to propagate.  A local subset must never call
    itself a full-market scan merely because it is internally consistent.
    """

    if observed_at.utcoffset() is None:
        raise ScanError("OBSERVED_TIME_NAIVE", "observed_at must include a timezone")
    calendar_payload = runner(["market", "calendar"])
    calendar_data = (
        calendar_payload.get("data")
        if isinstance(calendar_payload.get("data"), Mapping)
        else {}
    )
    calendar_items = calendar_data.get("item")
    calendar_meta = (
        calendar_payload.get("meta")
        if isinstance(calendar_payload.get("meta"), Mapping)
        else {}
    )
    if not isinstance(calendar_items, list):
        raise ScanError("CALENDAR_INVALID", "market.calendar did not return an item array")
    if (
        calendar_meta.get("source") != "remote"
        or calendar_meta.get("truncated") is True
        or _integer(calendar_meta.get("count")) != len(calendar_items)
    ):
        raise ScanError("CALENDAR_INVALID", "market.calendar provenance is incomplete")
    calendar_dates: list[date] = []
    for row in calendar_items:
        raw = row.get("date") if isinstance(row, Mapping) else None
        try:
            parsed = datetime.strptime(str(raw), "%Y%m%d").date()
        except ValueError as exc:
            raise ScanError("CALENDAR_INVALID", "market.calendar contains an invalid date") from exc
        calendar_dates.append(parsed)
    if (
        calendar_dates != sorted(calendar_dates)
        or len(set(calendar_dates)) != len(calendar_dates)
        or len(calendar_dates) < 100
    ):
        raise ScanError("CALENDAR_INVALID", "market.calendar is duplicated or too short")

    local_observed = observed_at.astimezone(CN_TZ)
    calendar_timestamp = _integer(calendar_data.get("timestamp"))
    if calendar_timestamp is None:
        raise ScanError("CALENDAR_INVALID", "market.calendar timestamp is missing")
    calendar_observed = datetime.fromtimestamp(calendar_timestamp / 1000, tz=timezone.utc)
    if abs((calendar_observed - observed_at.astimezone(timezone.utc)).total_seconds()) > 600:
        raise ScanError("CALENDAR_STALE", "market.calendar timestamp differs by over 10 minutes")
    try:
        completion_cutoff = time.fromisoformat(config.daily_bar_complete_after_cn)
    except ValueError as exc:
        raise ScanError("INVALID_COMPLETION_CUTOFF", "daily completion cutoff must be HH:MM") from exc
    completed = [
        session
        for session in calendar_dates
        if session < local_observed.date()
        or (session == local_observed.date() and local_observed.time() >= completion_cutoff)
    ]
    if not completed:
        raise ScanError("CALENDAR_INVALID", "calendar has no completed session")
    latest_completed = max(completed)
    if config.expected_through != latest_completed.isoformat():
        raise ScanError(
            "EXPECTED_SESSION_MISMATCH",
            "expected-through must equal the latest completed session from market.calendar "
            f"({latest_completed.isoformat()})",
        )

    page_size = 10_000
    offset = 0
    symbol_items: list[Any] = []
    symbol_timestamps: list[int] = []
    while True:
        symbols_payload = runner(
            [
                "symbol",
                "list",
                "--exchange",
                "SH,SZ,BJ",
                "--asset-type",
                "a-share",
                "--limit",
                str(page_size),
                "--offset",
                str(offset),
            ]
        )
        symbols_data = (
            symbols_payload.get("data")
            if isinstance(symbols_payload.get("data"), Mapping)
            else {}
        )
        page = symbols_data.get("item")
        symbols_meta = (
            symbols_payload.get("meta")
            if isinstance(symbols_payload.get("meta"), Mapping)
            else {}
        )
        if (
            not isinstance(page, list)
            or symbols_meta.get("source") != "remote"
            or symbols_meta.get("truncated") is True
            or _integer(symbols_meta.get("count")) != len(page)
        ):
            raise ScanError("REFERENCE_UNIVERSE_INVALID", "symbol.list is missing or truncated")
        symbols_timestamp = _integer(symbols_data.get("timestamp"))
        if symbols_timestamp is None:
            raise ScanError("REFERENCE_UNIVERSE_INVALID", "symbol.list timestamp is missing")
        symbol_timestamps.append(symbols_timestamp)
        symbol_items.extend(page)
        if len(page) < page_size:
            break
        offset += len(page)
        if offset > 100_000:
            raise ScanError("REFERENCE_UNIVERSE_INVALID", "symbol.list pagination did not terminate")

    symbols_timestamp = min(symbol_timestamps)
    symbol_ages = [
        (
            observed_at.astimezone(timezone.utc)
            - datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        ).total_seconds()
        for value in symbol_timestamps
    ]
    symbol_age_seconds = max(symbol_ages)
    if (
        min(symbol_ages) < -600
        or symbol_age_seconds > config.maximum_symbol_directory_age_minutes * 60
    ):
        raise ScanError(
            "REFERENCE_UNIVERSE_STALE",
            "symbol.list timestamp is in the future or exceeds the configured age limit",
        )
    reference_symbols: dict[str, str | None] = {}
    for row in symbol_items:
        if not isinstance(row, Mapping):
            raise ScanError("REFERENCE_UNIVERSE_INVALID", "symbol.list contains a non-object")
        thscode = str(row.get("thscode") or "")
        if (
            not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", thscode)
            or row.get("asset_type") != "a-share"
            or thscode in reference_symbols
        ):
            raise ScanError("REFERENCE_UNIVERSE_INVALID", "symbol.list contains an invalid symbol")
        name = str(row.get("name") or "").strip()
        reference_symbols[thscode] = name or None
    if len(reference_symbols) < config.minimum_reference_universe:
        raise ScanError(
            "REFERENCE_UNIVERSE_TOO_SMALL",
            f"authoritative A-share directory has only {len(reference_symbols)} symbols",
        )
    return {
        "calendar_source": "remote",
        "latest_completed_session": latest_completed.isoformat(),
        "daily_bar_complete_after_cn": config.daily_bar_complete_after_cn,
        "calendar_sessions_received": len(calendar_dates),
        "calendar_dates": [value.isoformat() for value in sorted(calendar_dates)],
        "calendar_timestamp": calendar_timestamp,
        "reference_source": "remote symbol.list",
        "reference_symbol_count": len(reference_symbols),
        "reference_timestamp": symbols_timestamp,
        "reference_age_seconds": round(symbol_age_seconds, 3),
        "reference_symbols": reference_symbols,
    }


def _past_only_feature_ctes(config: ScanConfig) -> str:
    expected = _parse_iso_date(config.expected_through)
    warmup_range = config.warmup_sessions - 1
    return f"""
calendar AS (
  SELECT date,
         row_number() OVER (ORDER BY date) AS session_id
  FROM (
    SELECT DISTINCT CAST(date AS DATE) AS date
    FROM v_daily d
    JOIN v_symbol s USING (thscode)
    WHERE CAST(d.date AS DATE) <= DATE '{expected}'
      AND s.asset_type = 'a-share'
      AND regexp_matches(d.thscode, '^[0-9]{{6}}\\.(SH|SZ|BJ)$')
  ) dates
),
calendar_meta AS (
  SELECT max(session_id) AS max_session_id FROM calendar
),
base AS (
  SELECT r.thscode,
         CAST(r.date AS DATE) AS date,
         c.session_id,
         CAST(r.open AS DOUBLE) AS raw_open,
         CAST(r.high AS DOUBLE) AS raw_high,
         CAST(r.low AS DOUBLE) AS raw_low,
         CAST(r.close AS DOUBLE) AS raw_close,
         CAST(r.volume AS DOUBLE) AS volume,
         CAST(r.amount AS DOUBLE) AS amount,
         CAST(h.open AS DOUBLE) AS adjusted_open,
         CAST(h.close AS DOUBLE) AS adjusted_close
  FROM v_daily r
  JOIN v_daily_hfq h USING (thscode, date)
  JOIN v_symbol symbol_meta
    ON symbol_meta.thscode = r.thscode
   AND symbol_meta.asset_type = 'a-share'
  JOIN calendar c ON CAST(r.date AS DATE) = c.date
  WHERE CAST(r.date AS DATE) <= DATE '{expected}'
    AND regexp_matches(r.thscode, '^[0-9]{{6}}\\.(SH|SZ|BJ)$')
),
lagged AS (
  SELECT b.*,
         p1.adjusted_close AS previous_close,
         p20.adjusted_close AS close_20_sessions_ago,
         p60.adjusted_close AS close_60_sessions_ago,
         count(*) OVER warmup_window AS quote_days_warmup,
         count(*) OVER twenty_day_window AS quote_days_20,
         avg(b.amount) OVER twenty_day_window AS amount_20d
  FROM base b
  LEFT JOIN base p1
    ON p1.thscode = b.thscode AND p1.session_id = b.session_id - 1
  LEFT JOIN base p20
    ON p20.thscode = b.thscode AND p20.session_id = b.session_id - 20
  LEFT JOIN base p60
    ON p60.thscode = b.thscode AND p60.session_id = b.session_id - 60
  WINDOW
    warmup_window AS (
      PARTITION BY b.thscode ORDER BY b.session_id
      RANGE BETWEEN {int(warmup_range)} PRECEDING AND CURRENT ROW
    ),
    twenty_day_window AS (
      PARTITION BY b.thscode ORDER BY b.session_id
      RANGE BETWEEN 19 PRECEDING AND CURRENT ROW
    )
),
returns AS (
  SELECT *,
         CASE WHEN previous_close > 0 AND adjusted_close > 0
              THEN ln(adjusted_close / previous_close) END AS daily_log_return
  FROM lagged
),
features AS (
  SELECT *,
         adjusted_close / nullif(close_20_sessions_ago, 0) - 1 AS momentum_20d,
         adjusted_close / nullif(close_60_sessions_ago, 0) - 1 AS momentum_60d,
         stddev_samp(daily_log_return) OVER (
           PARTITION BY thscode ORDER BY session_id
           RANGE BETWEEN 19 PRECEDING AND CURRENT ROW
         ) * sqrt(252.0) AS volatility_20d_annualized,
         count(daily_log_return) OVER (
           PARTITION BY thscode ORDER BY session_id
           RANGE BETWEEN 19 PRECEDING AND CURRENT ROW
         ) AS return_days_20
  FROM returns
),
eligible AS (
  SELECT *
  FROM features
  WHERE quote_days_warmup = {int(config.warmup_sessions)}
    AND quote_days_20 = 20
    AND return_days_20 = 20
    AND raw_close >= {float(config.minimum_price)}
    AND amount_20d >= {float(config.minimum_amount_20d)}
    AND momentum_20d IS NOT NULL
    AND momentum_60d IS NOT NULL
    AND volatility_20d_annualized > 0
),
rank_components AS (
  SELECT *,
         percent_rank() OVER (PARTITION BY date ORDER BY momentum_20d) AS rank_momentum_20d,
         percent_rank() OVER (PARTITION BY date ORDER BY momentum_60d) AS rank_momentum_60d,
         1 - percent_rank() OVER (PARTITION BY date ORDER BY volatility_20d_annualized) AS rank_low_volatility,
         percent_rank() OVER (PARTITION BY date ORDER BY amount_20d) AS rank_liquidity
  FROM eligible
),
scored AS (
  SELECT *,
         0.25 * rank_momentum_20d
       + 0.45 * rank_momentum_60d
       + 0.20 * rank_low_volatility
       + 0.10 * rank_liquidity AS score
  FROM rank_components
)
""".strip()


def _quality_select(config: ScanConfig) -> str:
    expected = _parse_iso_date(config.expected_through)
    return f"""
WITH a_share_daily AS (
  SELECT d.*, s.exchange
  FROM v_daily d
  JOIN v_symbol s USING (thscode)
  WHERE CAST(d.date AS DATE) <= DATE '{expected}'
    AND s.asset_type = 'a-share'
    AND regexp_matches(d.thscode, '^[0-9]{{6}}\\.(SH|SZ|BJ)$')
),
recent_session_counts AS (
  SELECT CAST(date AS DATE) AS date, count(DISTINCT thscode) AS symbols
  FROM a_share_daily
  GROUP BY date
  ORDER BY date DESC
  LIMIT 60
)
SELECT
  count(*) AS rows,
  count(DISTINCT thscode) AS symbols,
  min(CAST(date AS DATE)) AS min_date,
  max(CAST(date AS DATE)) AS max_date,
  count(*) FILTER (WHERE CAST(date AS DATE) = DATE '{expected}') AS latest_rows,
  count(DISTINCT thscode) FILTER (WHERE CAST(date AS DATE) = DATE '{expected}') AS latest_symbols,
  count(DISTINCT thscode) FILTER (
    WHERE CAST(date AS DATE) = DATE '{expected}' AND exchange = 'SH'
  ) AS latest_sh_symbols,
  count(DISTINCT thscode) FILTER (
    WHERE CAST(date AS DATE) = DATE '{expected}' AND exchange = 'SZ'
  ) AS latest_sz_symbols,
  count(DISTINCT thscode) FILTER (
    WHERE CAST(date AS DATE) = DATE '{expected}' AND exchange = 'BJ'
  ) AS latest_bj_symbols,
  (SELECT quantile_cont(symbols, 0.95) FROM recent_session_counts) AS recent_60_session_p95,
  (SELECT count(*) FROM (
     SELECT thscode, date, count(*) AS n
     FROM a_share_daily
     GROUP BY thscode, date HAVING count(*) > 1
   ) duplicates) AS duplicate_keys,
  count(*) FILTER (
    WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
       OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
       OR high < greatest(open, close, low)
       OR low > least(open, close, high)
  ) AS invalid_ohlc,
  (SELECT count(*) FROM v_symbol WHERE asset_type = 'a-share') AS symbol_metadata_rows,
  (SELECT count(*) FROM v_symbol
    WHERE asset_type = 'a-share' AND name IS NOT NULL AND trim(name) <> '') AS named_symbols
  ,(SELECT count(*)
    FROM a_share_daily r
    LEFT JOIN calc_adjust_factor_daily f USING (thscode, date)
    WHERE f.date IS NULL) AS missing_adjusted_rows
  ,(SELECT count(*) FROM v_symbol
    WHERE asset_type = 'a-share'
      AND NOT regexp_matches(thscode, '^[0-9]{{6}}\\.(SH|SZ|BJ)$')) AS invalid_a_share_symbols
  ,(SELECT count(*) FROM _import_batches
    WHERE status <> 'complete' OR completed_at IS NULL) AS incomplete_import_batches
  ,(SELECT count(*)
    FROM raw_kline_daily r
    LEFT JOIN _import_batches b USING (batch_id)
    WHERE CAST(r.date AS DATE) <= DATE '{expected}'
      AND (r.batch_id IS NULL OR b.batch_id IS NULL OR b.status <> 'complete')) AS unverified_batch_rows
  ,(SELECT string_agg(DISTINCT batch_id, ',' ORDER BY batch_id)
    FROM raw_kline_daily
    WHERE CAST(date AS DATE) <= DATE '{expected}') AS kline_batch_ids
  ,(SELECT value FROM _meta WHERE key = 'last_kline_release_id') AS last_kline_release_id
  ,(SELECT string_agg(DISTINCT replace(source, 'fuyao:', ''), ',' ORDER BY replace(source, 'fuyao:', ''))
    FROM _import_batches b
    WHERE b.status = 'complete'
      AND EXISTS (
        SELECT 1 FROM raw_kline_daily r
        WHERE r.batch_id = b.batch_id AND CAST(r.date AS DATE) <= DATE '{expected}'
      )) AS completed_kline_release_ids
FROM a_share_daily
""".strip()


def build_quality_sql(config: ScanConfig) -> str:
    return _quality_select(config)


def _backtest_ctes(config: ScanConfig) -> str:
    step = config.holding_sessions + 1
    return f"""
signal_dates AS (
  SELECT c.date, c.session_id
  FROM calendar c CROSS JOIN calendar_meta m
  WHERE c.session_id > {int(config.warmup_sessions)}
    AND c.session_id + {int(config.holding_sessions + 1)} <= m.max_session_id
    AND c.session_id % {int(step)} = 0
),
signal_scores AS (
  SELECT s.*,
         row_number() OVER (PARTITION BY s.date ORDER BY s.score DESC, s.thscode) AS selection_rank,
         count(*) OVER (PARTITION BY s.date) AS universe_count
  FROM scored s
  JOIN signal_dates d ON s.date = d.date
),
realized AS (
  SELECT s.*,
         entry.adjusted_open AS entry_open,
         exit.adjusted_open AS exit_open,
         CASE WHEN entry.adjusted_open > 0
                    AND entry.volume > 0
                    AND entry.amount > 0
                    AND entry_previous.adjusted_close > 0
                    AND (entry.raw_high > entry.raw_low
                         OR entry.adjusted_open <= entry_previous.adjusted_close)
              THEN 1 ELSE 0 END AS entry_executable,
         CASE WHEN exit.adjusted_open > 0
                    AND exit.volume > 0
                    AND exit.amount > 0
                    AND exit_previous.adjusted_close > 0
                    AND (exit.raw_high > exit.raw_low
                         OR exit.adjusted_open >= exit_previous.adjusted_close)
              THEN 1 ELSE 0 END AS exit_executable,
         CASE
           WHEN NOT (entry.adjusted_open > 0
                     AND entry.volume > 0
                     AND entry.amount > 0
                     AND entry_previous.adjusted_close > 0
                     AND (entry.raw_high > entry.raw_low
                          OR entry.adjusted_open <= entry_previous.adjusted_close)) THEN 0.0
           WHEN NOT (exit.adjusted_open > 0
                     AND exit.volume > 0
                     AND exit.amount > 0
                     AND exit_previous.adjusted_close > 0
                     AND (exit.raw_high > exit.raw_low
                          OR exit.adjusted_open >= exit_previous.adjusted_close)) THEN -1.0
           ELSE exit.adjusted_open / nullif(entry.adjusted_open, 0) - 1.0
         END AS gross_forward_return,
         CASE WHEN entry.adjusted_open > 0
                    AND entry.volume > 0
                    AND entry.amount > 0
                    AND entry_previous.adjusted_close > 0
                    AND (entry.raw_high > entry.raw_low
                         OR entry.adjusted_open <= entry_previous.adjusted_close)
              THEN 1 ELSE 0 END AS executed,
         CASE WHEN entry.adjusted_open > 0
                    AND entry.volume > 0
                    AND entry.amount > 0
                    AND entry_previous.adjusted_close > 0
                    AND (entry.raw_high > entry.raw_low
                         OR entry.adjusted_open <= entry_previous.adjusted_close)
                    AND exit.adjusted_open > 0
                    AND exit.volume > 0
                    AND exit.amount > 0
                    AND exit_previous.adjusted_close > 0
                    AND (exit.raw_high > exit.raw_low
                         OR exit.adjusted_open >= exit_previous.adjusted_close)
              THEN 1 ELSE 0 END AS fully_priced
  FROM signal_scores s
  LEFT JOIN base entry
    ON entry.thscode = s.thscode
   AND entry.session_id = s.session_id + 1
  LEFT JOIN base entry_previous
    ON entry_previous.thscode = s.thscode
   AND entry_previous.session_id = s.session_id
  LEFT JOIN base exit
    ON exit.thscode = s.thscode
   AND exit.session_id = s.session_id + {int(config.holding_sessions + 1)}
  LEFT JOIN base exit_previous
    ON exit_previous.thscode = s.thscode
   AND exit_previous.session_id = s.session_id + {int(config.holding_sessions)}
),
period_results AS (
  SELECT CAST(date AS VARCHAR) AS signal_date,
         max(universe_count) AS universe_count,
         count(*) FILTER (WHERE selection_rank <= {int(config.top_k)}) AS selected_count,
         avg(executed) FILTER (WHERE selection_rank <= {int(config.top_k)}) AS execution_coverage,
         avg(fully_priced) FILTER (WHERE selection_rank <= {int(config.top_k)}) AS exit_price_coverage,
         avg(executed) AS benchmark_execution_coverage,
         avg(fully_priced) AS benchmark_exit_price_coverage,
         avg(
           CASE
             WHEN executed = 0 THEN 0.0
             WHEN fully_priced = 0 THEN -1.0
             ELSE gross_forward_return - {config.cost_rate}
           END
         ) FILTER (WHERE selection_rank <= {int(config.top_k)}) AS strategy_net_return,
         avg(gross_forward_return) AS benchmark_return
  FROM realized
  GROUP BY date
),
selected_assets AS (
  SELECT * FROM realized WHERE selection_rank <= {int(config.top_k)}
),
path_grid AS (
  SELECT a.date AS signal_date,
         a.thscode,
         a.entry_open,
         a.entry_executable,
         a.exit_executable,
         c.session_id AS path_session_id,
         c.date AS path_date,
         c.session_id - (a.session_id + 1) AS path_step,
         CASE
           WHEN a.entry_executable = 0 THEN NULL
           WHEN c.session_id = a.session_id + {int(config.holding_sessions + 1)}
             THEN CASE WHEN a.exit_executable = 1
                       THEN a.exit_open / nullif(a.entry_open, 0) END
           WHEN mark.adjusted_close > 0
             THEN mark.adjusted_close / nullif(a.entry_open, 0)
         END AS mark_multiple
  FROM selected_assets a
  JOIN calendar c
    ON c.session_id BETWEEN a.session_id + 1
                        AND a.session_id + {int(config.holding_sessions + 1)}
  LEFT JOIN base mark
    ON mark.thscode = a.thscode AND mark.session_id = c.session_id
),
strategy_path AS (
  SELECT CAST(signal_date AS VARCHAR) AS signal_date,
         path_session_id,
         CAST(path_date AS VARCHAR) AS path_date,
         path_step,
         count(*) AS expected_assets,
         count(mark_multiple) AS priced_assets,
         count(mark_multiple)::DOUBLE / nullif(count(*), 0) AS asset_coverage,
         avg(mark_multiple)
           - CASE WHEN path_step = {int(config.holding_sessions)}
                  THEN {config.cost_rate} ELSE 0.0 END AS portfolio_net_value
  FROM path_grid
  GROUP BY signal_date, path_session_id, path_date, path_step
)
""".strip()


def build_period_returns_sql(config: ScanConfig) -> str:
    # One cohort every holding+1 exchange sessions prevents overlap.
    feature_ctes = _past_only_feature_ctes(config)
    return f"WITH {feature_ctes},\n{_backtest_ctes(config)}\nSELECT * FROM period_results ORDER BY signal_date"


def _candidate_ctes(config: ScanConfig) -> str:
    expected = _parse_iso_date(config.expected_through)
    return f"""
latest AS (
  SELECT s.*,
         row_number() OVER (ORDER BY s.score DESC, s.thscode) AS candidate_rank,
         count(*) OVER () AS eligible_count
  FROM scored s
  WHERE s.date = DATE '{expected}'
),
candidate_results AS (
SELECT l.candidate_rank,
       l.eligible_count,
       l.thscode,
       sym.name,
       CAST(l.date AS VARCHAR) AS signal_date,
       l.raw_close AS close,
       l.amount_20d,
       l.momentum_20d,
       l.momentum_60d,
       l.volatility_20d_annualized,
       l.rank_momentum_20d,
       l.rank_momentum_60d,
       l.rank_low_volatility,
       l.rank_liquidity,
       l.score
FROM latest l
LEFT JOIN v_symbol sym USING (thscode)
WHERE l.candidate_rank <= {int(config.candidate_count)}
)
""".strip()


def build_candidates_sql(config: ScanConfig) -> str:
    feature_ctes = _past_only_feature_ctes(config)
    return (
        f"WITH {feature_ctes},\n{_candidate_ctes(config)}\n"
        "SELECT * FROM candidate_results ORDER BY candidate_rank"
    )


def build_scan_sql(config: ScanConfig, reference_calendar_start: str) -> str:
    """Build the only analytical SELECT executed by the scanner.

    Quality, backtest, drawdown path, calendar coverage, and latest leaders are
    tagged inside one DuckDB statement so they share one MVCC snapshot.
    """

    start = _parse_iso_date(reference_calendar_start)
    feature_ctes = _past_only_feature_ctes(config)
    return f"""
WITH {feature_ctes},
{_backtest_ctes(config)},
{_candidate_ctes(config)},
quality_result AS (
  {_quality_select(config)}
),
reference_window_calendar AS (
  SELECT CAST(date AS VARCHAR) AS date
  FROM calendar
  WHERE date >= DATE '{start}'
),
latest_local_symbols AS (
  SELECT DISTINCT thscode
  FROM base
  WHERE date = DATE '{_parse_iso_date(config.expected_through)}'
)
SELECT kind, payload
FROM (
  SELECT 0 AS kind_order, '' AS row_order,
         'quality' AS kind, to_json(q) AS payload
  FROM quality_result q
  UNION ALL
  SELECT 1, signal_date,
         'period_return', to_json(p)
  FROM period_results p
  UNION ALL
  SELECT 2, signal_date || ':' || lpad(CAST(path_step AS VARCHAR), 4, '0'),
         'strategy_path', to_json(s)
  FROM strategy_path s
  UNION ALL
  SELECT 3, date,
         'local_calendar', to_json(c)
  FROM reference_window_calendar c
  UNION ALL
  SELECT 4, thscode,
         'latest_symbol', to_json(x)
  FROM latest_local_symbols x
  UNION ALL
  SELECT 5, lpad(CAST(candidate_rank AS VARCHAR), 4, '0'),
         'candidate', to_json(x)
  FROM candidate_results x
) tagged
ORDER BY kind_order, row_order
""".strip()


def _query_rows(runner: CliRunner, sql: str) -> list[dict[str, Any]]:
    payload = runner(["db", "query", "--sql", sql])
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(row, Mapping) for row in data):
        raise ScanError("QUERY_RESULT_INVALID", "db.query data is not an array of objects")
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    if (
        meta.get("truncated") is not False
        or _integer(meta.get("count")) != len(data)
    ):
        raise ScanError("QUERY_RESULT_TRUNCATED", "db.query result provenance is incomplete")
    return [dict(row) for row in data]


def _query_tagged_rows(
    runner: CliRunner, sql: str
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    rows = _query_rows(runner, sql)
    sections: dict[str, list[dict[str, Any]]] = {
        "quality": [],
        "period_return": [],
        "strategy_path": [],
        "local_calendar": [],
        "latest_symbol": [],
        "candidate": [],
    }
    for row in rows:
        kind = str(row.get("kind") or "")
        payload = row.get("payload")
        if kind not in sections or not isinstance(payload, str):
            raise ScanError("TAGGED_QUERY_INVALID", "unrecognized tagged query row")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ScanError("TAGGED_QUERY_INVALID", "tagged payload is invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ScanError("TAGGED_QUERY_INVALID", "tagged payload is not an object")
        sections[kind].append(decoded)
    if len(sections["quality"]) != 1:
        raise ScanError("TAGGED_QUERY_INVALID", "tagged query needs exactly one quality row")
    return sections, len(rows)


def _compound(returns: Sequence[float]) -> float:
    wealth = 1.0
    for value in returns:
        wealth *= max(0.0, 1.0 + value)
    return wealth - 1.0


def _max_drawdown(returns: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= max(0.0, 1.0 + value)
        peak = max(peak, wealth)
        drawdown = wealth / peak - 1.0 if peak > 0 else -1.0
        worst = min(worst, drawdown)
    return worst


def _max_drawdown_levels(levels: Sequence[float]) -> float:
    peak = 1.0
    worst = 0.0
    for level in levels:
        peak = max(peak, level)
        worst = min(worst, level / peak - 1.0 if peak > 0 else -1.0)
    return worst


def _path_metrics(
    rows: Sequence[Mapping[str, Any]],
    path_rows: Sequence[Mapping[str, Any]],
    config: ScanConfig,
) -> dict[str, Any]:
    by_signal: dict[str, list[Mapping[str, Any]]] = {}
    for row in path_rows:
        by_signal.setdefault(str(row.get("signal_date") or ""), []).append(row)
    wealth_base = 1.0
    wealth_levels: list[float] = []
    minimum_coverage = 1.0
    expected_path_points = config.holding_sessions + 1
    minimum_points = expected_path_points
    complete = True
    for period in rows:
        signal_date = str(period.get("signal_date") or "")
        cohort = sorted(
            by_signal.get(signal_date, []),
            key=lambda row: int(_integer(row.get("path_step")) or -1),
        )
        if len(cohort) != expected_path_points:
            complete = False
            minimum_points = min(minimum_points, len(cohort))
            continue
        steps = [_integer(row.get("path_step")) for row in cohort]
        if steps != list(range(expected_path_points)):
            complete = False
        coverages = [_number(row.get("asset_coverage")) for row in cohort]
        values = [_number(row.get("portfolio_net_value")) for row in cohort]
        if any(value is None for value in coverages + values):
            complete = False
            minimum_coverage = 0.0
            continue
        coverage_values = [float(value) for value in coverages if value is not None]
        path_values = [float(value) for value in values if value is not None]
        minimum_coverage = min(minimum_coverage, min(coverage_values))
        if min(coverage_values) < config.minimum_path_coverage or any(
            value <= 0 for value in path_values
        ):
            complete = False
            continue
        period_return = _number(period.get("strategy_net_return"))
        if period_return is None or not math.isclose(
            path_values[-1] - 1.0, period_return, rel_tol=1e-8, abs_tol=1e-8
        ):
            raise ScanError(
                "RETURN_PATH_MISMATCH",
                f"daily path and period return disagree for {signal_date}",
            )
        wealth_levels.extend(wealth_base * value for value in path_values)
        wealth_base *= path_values[-1]
    return {
        "path_complete": complete,
        "minimum_path_coverage": round(minimum_coverage, 6),
        "minimum_path_points": minimum_points,
        "daily_close_max_drawdown": (
            round(_max_drawdown_levels(wealth_levels), 6) if complete else None
        ),
    }


def _metrics(
    rows: Sequence[Mapping[str, Any]],
    config: ScanConfig,
    path_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        raise ScanError("RETURN_SERIES_EMPTY", "return series is empty")
    path = _path_metrics(rows, path_rows, config)
    minimum_execution_coverage = min(
        float(_number(row.get("execution_coverage")) or 0.0) for row in rows
    )
    minimum_exit_price_coverage = min(
        float(_number(row.get("exit_price_coverage")) or 0.0) for row in rows
    )
    minimum_benchmark_execution_coverage = min(
        float(_number(row.get("benchmark_execution_coverage")) or 0.0)
        for row in rows
    )
    minimum_benchmark_exit_price_coverage = min(
        float(_number(row.get("benchmark_exit_price_coverage")) or 0.0)
        for row in rows
    )
    base = {
        "periods": n,
        "start": str(rows[0].get("signal_date")),
        "end": str(rows[-1].get("signal_date")),
        "minimum_execution_coverage": round(minimum_execution_coverage, 6),
        "minimum_exit_price_coverage": round(minimum_exit_price_coverage, 6),
        "minimum_benchmark_execution_coverage": round(
            minimum_benchmark_execution_coverage, 6
        ),
        "minimum_benchmark_exit_price_coverage": round(
            minimum_benchmark_exit_price_coverage, 6
        ),
        "minimum_selected_count": min(
            int(_integer(row.get("selected_count")) or 0) for row in rows
        ),
        "minimum_universe_count": min(
            int(_integer(row.get("universe_count")) or 0) for row in rows
        ),
        **path,
    }
    performance_valid = (
        minimum_execution_coverage >= config.minimum_execution_coverage
        and minimum_exit_price_coverage >= config.minimum_execution_coverage
        and minimum_benchmark_execution_coverage >= config.minimum_execution_coverage
        and minimum_benchmark_exit_price_coverage >= config.minimum_execution_coverage
        and path["path_complete"]
        and path["minimum_path_coverage"] >= config.minimum_path_coverage
    )
    invalid_performance = {
        "performance_valid": False,
        "cumulative_return": None,
        "annualized_return": None,
        "annualized_volatility": None,
        "sharpe_zero_rf": None,
        "cohort_endpoint_max_drawdown": None,
        "max_drawdown": None,
        "positive_period_rate": None,
        "benchmark_cumulative_return": None,
        "benchmark_annualized_return": None,
        "cumulative_excess_return": None,
        "mean_period_excess_return": None,
    }
    if not performance_valid:
        return {**base, **invalid_performance}

    strategy = [_number(row.get("strategy_net_return")) for row in rows]
    benchmark = [_number(row.get("benchmark_return")) for row in rows]
    if any(value is None for value in strategy + benchmark):
        raise ScanError("RETURN_SERIES_INVALID", "valid return series contains null values")
    strategy_values = [float(value) for value in strategy if value is not None]
    benchmark_values = [float(value) for value in benchmark if value is not None]
    cumulative = _compound(strategy_values)
    benchmark_cumulative = _compound(benchmark_values)
    periods_per_year = config.periods_per_year
    annualized = (1.0 + cumulative) ** (periods_per_year / n) - 1.0 if cumulative > -1 else -1.0
    benchmark_annualized = (
        (1.0 + benchmark_cumulative) ** (periods_per_year / n) - 1.0
        if benchmark_cumulative > -1
        else -1.0
    )
    volatility = stdev(strategy_values) * math.sqrt(periods_per_year) if n > 1 else None
    sharpe = mean(strategy_values) * periods_per_year / volatility if volatility else None
    excess_periods = [left - right for left, right in zip(strategy_values, benchmark_values)]
    return {
        **base,
        "performance_valid": True,
        "cumulative_return": round(cumulative, 6),
        "annualized_return": round(annualized, 6),
        "annualized_volatility": round(volatility, 6) if volatility is not None else None,
        "sharpe_zero_rf": round(sharpe, 6) if sharpe is not None else None,
        "cohort_endpoint_max_drawdown": round(_max_drawdown(strategy_values), 6),
        "max_drawdown": path["daily_close_max_drawdown"],
        "positive_period_rate": round(
            sum(value > 0 for value in strategy_values) / n, 6
        ),
        "benchmark_cumulative_return": round(benchmark_cumulative, 6),
        "benchmark_annualized_return": round(benchmark_annualized, 6),
        "cumulative_excess_return": round(cumulative - benchmark_cumulative, 6),
        "mean_period_excess_return": round(mean(excess_periods), 6),
    }


def chronological_validation(
    rows: Sequence[Mapping[str, Any]],
    config: ScanConfig,
    path_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row.get("signal_date") or ""))
    dates = [str(row.get("signal_date") or "") for row in ordered]
    if any(not value for value in dates) or len(set(dates)) != len(dates):
        raise ScanError("SIGNAL_DATES_INVALID", "signal dates are missing or duplicated")
    required = config.minimum_train_periods + config.purge_periods + config.oos_periods
    if len(ordered) < required:
        raise ScanError(
            "INSUFFICIENT_TIME_SERIES",
            f"need at least {required} non-overlapping periods, found {len(ordered)}",
        )
    oos = ordered[-config.oos_periods :]
    purge_start = len(ordered) - config.oos_periods - config.purge_periods
    insample = ordered[:purge_start]
    purged = ordered[purge_start : len(ordered) - config.oos_periods]
    if len(insample) < config.minimum_train_periods:
        raise ScanError("INSUFFICIENT_TRAIN_SAMPLE", "training sample is too short")
    in_metrics = _metrics(insample, config, path_rows)
    out_metrics = _metrics(oos, config, path_rows)
    oos_drawdown = _number(out_metrics.get("max_drawdown"))
    oos_annualized = _number(out_metrics.get("annualized_return"))
    oos_excess = _number(out_metrics.get("cumulative_excess_return"))
    gates = {
        "minimum_oos_periods": out_metrics["periods"] >= config.oos_periods,
        "in_sample_performance_valid": in_metrics["performance_valid"],
        "oos_performance_valid": out_metrics["performance_valid"],
        "in_sample_selected_count": in_metrics["minimum_selected_count"] >= config.top_k,
        "oos_selected_count": out_metrics["minimum_selected_count"] >= config.top_k,
        "in_sample_execution_coverage": in_metrics["minimum_execution_coverage"]
        >= config.minimum_execution_coverage,
        "in_sample_exit_price_coverage": in_metrics["minimum_exit_price_coverage"]
        >= config.minimum_execution_coverage,
        "oos_execution_coverage": out_metrics["minimum_execution_coverage"]
        >= config.minimum_execution_coverage,
        "oos_exit_price_coverage": out_metrics["minimum_exit_price_coverage"]
        >= config.minimum_execution_coverage,
        "in_sample_benchmark_execution_coverage": in_metrics[
            "minimum_benchmark_execution_coverage"
        ]
        >= config.minimum_execution_coverage,
        "in_sample_benchmark_exit_price_coverage": in_metrics[
            "minimum_benchmark_exit_price_coverage"
        ]
        >= config.minimum_execution_coverage,
        "oos_benchmark_execution_coverage": out_metrics[
            "minimum_benchmark_execution_coverage"
        ]
        >= config.minimum_execution_coverage,
        "oos_benchmark_exit_price_coverage": out_metrics[
            "minimum_benchmark_exit_price_coverage"
        ]
        >= config.minimum_execution_coverage,
        "in_sample_daily_path_complete": in_metrics["path_complete"],
        "oos_daily_path_complete": out_metrics["path_complete"],
        "in_sample_daily_path_coverage": in_metrics["minimum_path_coverage"]
        >= config.minimum_path_coverage,
        "oos_daily_path_coverage": out_metrics["minimum_path_coverage"]
        >= config.minimum_path_coverage,
        "positive_absolute_return": oos_annualized is not None and oos_annualized > 0,
        "positive_excess_return": oos_excess is not None and oos_excess > 0,
        "maximum_drawdown": oos_drawdown is not None
        and oos_drawdown >= -config.maximum_oos_drawdown,
        # The current vendor schema only has today's symbol directory.  It has
        # no point-in-time listing/delisting/ST intervals, so historical
        # survivorship bias cannot be ruled out.  Strict mode must not turn a
        # historical score into published candidates until that table exists.
        "historical_point_in_time_universe_verified": False,
        # This first run was not cryptographically timestamped before the OOS
        # observations existed.  Report the holdout, but never call it an
        # independently untouched preregistration or use it to release names.
        "independent_preregistration_verified": False,
    }
    return {
        "split_policy": "chronological holdout with purge gap; not independently preregistered",
        "parameter_tuning_control": "not independently verifiable for this historical first run",
        "in_sample": in_metrics,
        "purged_periods": [str(row.get("signal_date")) for row in purged],
        "out_of_sample": out_metrics,
        "gates": gates,
        "passed": all(gates.values()),
    }


def normalize_quality(
    rows: Sequence[Mapping[str, Any]],
    config: ScanConfig,
    reference_data: Mapping[str, Any],
    latest_local_symbols: set[str],
) -> dict[str, Any]:
    if len(rows) != 1:
        raise ScanError("QUALITY_RESULT_INVALID", "quality query must return exactly one row")
    raw = rows[0]
    integer_fields = (
        "rows",
        "symbols",
        "latest_rows",
        "latest_symbols",
        "latest_sh_symbols",
        "latest_sz_symbols",
        "latest_bj_symbols",
        "duplicate_keys",
        "invalid_ohlc",
        "symbol_metadata_rows",
        "named_symbols",
        "missing_adjusted_rows",
        "invalid_a_share_symbols",
        "incomplete_import_batches",
        "unverified_batch_rows",
    )
    parsed: dict[str, Any] = {key: _integer(raw.get(key)) for key in integer_fields}
    if any(parsed[key] is None for key in integer_fields):
        raise ScanError("QUALITY_RESULT_INVALID", "quality counts are missing or invalid")
    parsed["min_date"] = str(raw.get("min_date") or "")
    parsed["max_date"] = str(raw.get("max_date") or "")
    parsed["expected_through"] = config.expected_through
    parsed["fresh"] = parsed["max_date"] == config.expected_through
    recent_p95 = _number(raw.get("recent_60_session_p95"))
    if recent_p95 is None or recent_p95 <= 0:
        raise ScanError("QUALITY_RESULT_INVALID", "recent universe p95 is invalid")
    parsed["recent_60_session_p95"] = round(recent_p95, 3)
    parsed["kline_batch_ids"] = str(raw.get("kline_batch_ids") or "")
    parsed["last_kline_release_id"] = str(raw.get("last_kline_release_id") or "")
    parsed["completed_kline_release_ids"] = str(
        raw.get("completed_kline_release_ids") or ""
    )
    parsed["name_coverage"] = round(
        parsed["named_symbols"] / parsed["symbol_metadata_rows"], 6
    ) if parsed["symbol_metadata_rows"] else 0.0
    reference_symbols = reference_data.get("reference_symbols")
    if not isinstance(reference_symbols, Mapping):
        raise ScanError("REFERENCE_UNIVERSE_INVALID", "reference symbol map is missing")
    if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value) for value in latest_local_symbols):
        raise ScanError("QUALITY_RESULT_INVALID", "latest local symbol set is invalid")
    if len(latest_local_symbols) != parsed["latest_symbols"]:
        raise ScanError("QUALITY_RESULT_INVALID", "latest symbol count disagrees with symbol rows")
    reference_set = {str(value) for value in reference_symbols}
    reference_count = len(reference_symbols)
    reference_exchange_counts = {
        exchange: sum(str(code).endswith(f".{exchange}") for code in reference_symbols)
        for exchange in ("SH", "SZ", "BJ")
    }
    parsed["reference_symbol_count"] = reference_count
    parsed["historical_reference_coverage"] = round(
        parsed["symbols"] / reference_count, 6
    )
    intersection = latest_local_symbols & reference_set
    missing_reference = sorted(reference_set - latest_local_symbols)
    unexpected_local = sorted(latest_local_symbols - reference_set)
    parsed["latest_reference_coverage"] = round(len(intersection) / reference_count, 6)
    parsed["missing_reference_symbols"] = len(missing_reference)
    parsed["unexpected_local_symbols"] = len(unexpected_local)
    parsed["missing_reference_sha256"] = hashlib.sha256(
        "\n".join(missing_reference).encode("utf-8")
    ).hexdigest()
    parsed["unexpected_local_sha256"] = hashlib.sha256(
        "\n".join(unexpected_local).encode("utf-8")
    ).hexdigest()
    parsed["latest_vs_recent_p95"] = round(
        parsed["latest_symbols"] / recent_p95, 6
    )
    parsed["exchange_reference_coverage"] = {
        exchange: round(
            sum(code.endswith(f".{exchange}") for code in intersection) / count, 6
        )
        if count
        else 0.0
        for exchange, count in reference_exchange_counts.items()
    }
    completed_releases = {
        value for value in parsed["completed_kline_release_ids"].split(",") if value
    }
    parsed["gates"] = {
        "expected_session_present": parsed["fresh"] and parsed["latest_symbols"] > 0,
        "no_duplicate_keys": parsed["duplicate_keys"] == 0,
        "valid_ohlc": parsed["invalid_ohlc"] == 0,
        "complete_adjusted_history": parsed["missing_adjusted_rows"] == 0,
        "valid_a_share_classification": parsed["invalid_a_share_symbols"] == 0,
        "no_incomplete_imports": parsed["incomplete_import_batches"] == 0,
        "all_rows_from_completed_batches": parsed["unverified_batch_rows"] == 0,
        "release_metadata_consistent": bool(parsed["last_kline_release_id"])
        and parsed["last_kline_release_id"] in completed_releases,
        "minimum_latest_universe": parsed["latest_symbols"] >= config.minimum_latest_universe,
        "historical_reference_coverage": parsed["historical_reference_coverage"]
        >= config.minimum_historical_reference_coverage,
        "latest_reference_coverage": parsed["latest_reference_coverage"]
        >= config.minimum_reference_coverage,
        "exchange_reference_coverage": min(
            parsed["exchange_reference_coverage"].values()
        )
        >= config.minimum_exchange_reference_coverage,
        "latest_vs_recent_p95": parsed["latest_vs_recent_p95"]
        >= config.minimum_latest_vs_recent_p95,
    }
    parsed["passed"] = all(parsed["gates"].values())
    return parsed


def normalize_candidates(
    rows: Sequence[Mapping[str, Any]], reference_data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    reference_symbols = reference_data.get("reference_symbols")
    if not isinstance(reference_symbols, Mapping):
        raise ScanError("REFERENCE_UNIVERSE_INVALID", "reference symbol map is missing")
    output: list[dict[str, Any]] = []
    for raw in rows:
        rank = _integer(raw.get("candidate_rank"))
        thscode = str(raw.get("thscode") or "")
        if rank is None or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", thscode):
            raise ScanError("CANDIDATE_RESULT_INVALID", "candidate rank or thscode is invalid")
        if thscode not in reference_symbols:
            raise ScanError(
                "CANDIDATE_REFERENCE_MISSING",
                f"candidate {thscode} is absent from the authoritative current directory",
            )
        required_numbers = {
            key: _number(raw.get(key))
            for key in (
                "close",
                "amount_20d",
                "momentum_20d",
                "momentum_60d",
                "volatility_20d_annualized",
                "score",
            )
        }
        if any(value is None for value in required_numbers.values()):
            raise ScanError("CANDIDATE_RESULT_INVALID", "candidate factors are missing or invalid")
        if required_numbers["close"] <= 0 or required_numbers["amount_20d"] < 0:
            raise ScanError("CANDIDATE_RESULT_INVALID", "candidate price or liquidity is invalid")
        output.append(
            {
                "rank": rank,
                "thscode": thscode,
                "name": reference_symbols.get(thscode) or raw.get("name") or None,
                "signal_date": str(raw.get("signal_date") or ""),
                "close": round(float(required_numbers["close"]), 4),
                "amount_20d": round(float(required_numbers["amount_20d"]), 2),
                "momentum_20d": round(float(required_numbers["momentum_20d"]), 6),
                "momentum_60d": round(float(required_numbers["momentum_60d"]), 6),
                "volatility_20d_annualized": round(
                    float(required_numbers["volatility_20d_annualized"]), 6
                ),
                "score": round(float(required_numbers["score"]), 6),
                "research_only": True,
            }
        )
    ranks = [row["rank"] for row in output]
    symbols = [row["thscode"] for row in output]
    if ranks != list(range(1, len(output) + 1)) or len(set(symbols)) != len(symbols):
        raise ScanError("CANDIDATE_RESULT_INVALID", "candidate ranks or symbols are duplicated")
    if any(not 0 <= row["score"] <= 1 for row in output):
        raise ScanError("CANDIDATE_RESULT_INVALID", "candidate score is outside [0, 1]")
    return output


def _base_report(config: ScanConfig, observed_at: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "engine_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "generated_at": observed_at.astimezone(CN_TZ).isoformat(timespec="seconds"),
        "ok": False,
        "status": "DATA_UNVERIFIED",
        "research_only": True,
        "disclaimer": "历史样本外结果不构成收益保证、买卖建议或自动交易授权。",
        "config": asdict(config),
        "methodology": {
            "data": "hithink-finance local DuckDB v_daily + v_daily_hfq",
            "universe": "远端当前A股目录校验覆盖；历史信号要求连续120个全市场交易日有报价",
            "signal_timing": "所有特征只使用信号日收盘及以前数据",
            "execution": f"信号后第1个交易日开盘进入，第{config.holding_sessions + 1}个交易日开盘退出；开盘等于日内高/低的买入/卖出保守视为不可执行",
            "schedule": f"每 {config.holding_sessions + 1} 个全市场交易日一个互不重叠组合",
            "score": "20日动量25% + 60日动量45% + 低20日波动20% + 20日流动性10%",
            "benchmark": "同期合格 A 股等权收益，未扣成本；策略与基准均要求完整成交覆盖",
            "transaction_cost": f"策略每个成功进入的完整持有周期扣 {config.round_trip_cost_bps:.1f} bps",
            "split": f"至少{config.minimum_train_periods}期样本内 + {config.purge_periods}期隔离 + 最后{config.oos_periods}期历史留出；未独立预注册",
            "parameter_tuning": "历史首次运行无法独立证明未查看留出样本，因此对应门禁固定失败",
            "missing_exit_policy": "缺失或保守判定不可执行会使成交/路径覆盖门禁失败，禁止释放候选",
            "drawdown": "按每个持有期逐交易日收盘及退出日开盘路径计算，不仅使用组合端点",
        },
        "limitations": [
            "当前远端目录可补名称，但本地库没有历史 ST、上市、退市有效区间。",
            "无法验证历史点时点股票宇宙，幸存者偏差门禁固定失败，不会依据该回测发布候选。",
            "日线只能保守识别一价/开盘极值，不能替代逐笔成交、官方涨跌停价、滑点和容量数据。",
            "候选只是固定横截面规则的研究输出，不含目标权重、交易金额或委托股数。",
        ],
        "required_manual_checks": [
            "证券名称、ST/退市风险和最新公告",
            "次日停牌、涨跌停与真实可成交量",
            "行业集中度、基本面与组合风险预算",
        ],
        "candidates": [],
    }


def run_scan(
    runner: CliRunner,
    config: ScanConfig,
    *,
    observed_at: datetime | None = None,
    fingerprint_reader: FingerprintReader = _database_fingerprint,
) -> dict[str, Any]:
    observed = observed_at or datetime.now(CN_TZ)
    report = _base_report(config, observed)
    cli_audit = audit_cli(runner)
    reference = audit_reference_data(runner, config, observed)
    database = audit_database(runner, fingerprint_reader)
    reference_dates = [date.fromisoformat(value) for value in reference["calendar_dates"]]
    expected_date = date.fromisoformat(config.expected_through)
    comparable_reference_dates = {
        value.isoformat() for value in reference_dates if value <= expected_date
    }
    if not comparable_reference_dates:
        raise ScanError("CALENDAR_INVALID", "no comparable authoritative calendar window")
    scan_sql = build_scan_sql(config, min(comparable_reference_dates))
    sections, tagged_row_count = _query_tagged_rows(runner, scan_sql)
    final_fingerprint = dict(fingerprint_reader(database["path"]))
    _assert_same_fingerprint(
        database["snapshot_fingerprint"], final_fingerprint, "the unified analytical query"
    )
    local_calendar = [str(row.get("date") or "") for row in sections["local_calendar"]]
    if any(not value for value in local_calendar) or len(local_calendar) != len(set(local_calendar)):
        raise ScanError("LOCAL_CALENDAR_INVALID", "local calendar rows are missing or duplicated")
    latest_local_symbols = {str(row.get("thscode") or "") for row in sections["latest_symbol"]}
    quality = normalize_quality(
        sections["quality"], config, reference, latest_local_symbols
    )
    quality["authoritative_calendar_start"] = min(comparable_reference_dates)
    quality["authoritative_calendar_sessions"] = len(comparable_reference_dates)
    quality["local_calendar_sessions"] = len(local_calendar)
    quality["gates"]["authoritative_calendar_exact_match"] = (
        set(local_calendar) == comparable_reference_dates
    )
    quality["passed"] = all(quality["gates"].values())
    report["runtime"] = cli_audit
    report["database"] = database
    report["reference_data"] = {
        "calendar_source": reference["calendar_source"],
        "calendar_timestamp": reference["calendar_timestamp"],
        "latest_completed_session": reference["latest_completed_session"],
        "daily_bar_complete_after_cn": reference["daily_bar_complete_after_cn"],
        "reference_source": reference["reference_source"],
        "reference_timestamp": reference["reference_timestamp"],
        "reference_age_seconds": reference["reference_age_seconds"],
        "reference_symbol_count": reference["reference_symbol_count"],
        "reference_symbol_set_sha256": hashlib.sha256(
            "\n".join(sorted(reference["reference_symbols"])).encode("utf-8")
        ).hexdigest(),
    }
    report["data_quality"] = quality
    report["query_manifest"] = {
        "unified_snapshot_query": {
            "sha256": hashlib.sha256(scan_sql.encode("utf-8")).hexdigest(),
            "row_count": tagged_row_count,
            "section_row_counts": {
                key: len(value) for key, value in sections.items()
            },
            "sql": scan_sql,
        }
    }
    if not quality["passed"]:
        report["errors"] = [
            key for key, passed in quality["gates"].items() if not passed
        ]
        return report
    returns = sections["period_return"]
    validation = chronological_validation(returns, config, sections["strategy_path"])
    report["validation"] = validation
    raw_candidates = sections["candidate"]
    normalized_candidates = normalize_candidates(raw_candidates, reference)
    eligible_count = _integer(raw_candidates[0].get("eligible_count")) if raw_candidates else 0
    report["latest_scan"] = {
        "signal_date": config.expected_through,
        "eligible_count": eligible_count,
        "scored_leaders": len(normalized_candidates),
        "candidate_payload_released": False,
        "current_market_coverage_verified": quality["passed"],
        "historical_point_in_time_universe_verified": False,
        "full_market_scan": False,
    }
    if not normalized_candidates or (eligible_count or 0) < config.minimum_latest_universe:
        report["status"] = "DATA_UNVERIFIED"
        report["errors"] = ["latest_eligible_universe_too_small"]
        return report
    if not cli_audit["runtime_supported"]:
        report["status"] = "RUNTIME_UNSUPPORTED"
        report["errors"] = [
            f"Node {cli_audit['node_version']} is below the CLI minimum {cli_audit['minimum_node_version']}",
            *(
                [
                    "strategy gates also failed: "
                    + ", ".join(
                        key for key, passed in validation["gates"].items() if not passed
                    )
                ]
                if not validation["passed"]
                else []
            ),
        ]
        return report
    if not validation["passed"]:
        report["status"] = "STRATEGY_UNVALIDATED"
        report["errors"] = [
            key for key, passed in validation["gates"].items() if not passed
        ]
        return report
    report["ok"] = True
    report["status"] = "READY_FOR_RESEARCH"
    report["candidates"] = normalized_candidates
    report["latest_scan"]["candidate_payload_released"] = True
    report["errors"] = []
    return report


def failed_report(config: ScanConfig, exc: Exception) -> dict[str, Any]:
    report = _base_report(config, datetime.now(CN_TZ))
    report["errors"] = [
        {
            "code": exc.code if isinstance(exc, ScanError) else "UNEXPECTED_ERROR",
            "message": _safe_error(exc),
        }
    ]
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only all-market scan with strict chronological OOS validation"
    )
    parser.add_argument(
        "--expected-through",
        required=True,
        help="latest completed exchange session expected in DuckDB (YYYY-MM-DD)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--candidate-count", type=int, default=20)
    parser.add_argument("--round-trip-cost-bps", type=float, default=20.0)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    return parser


def _config_from_args(args: argparse.Namespace) -> ScanConfig:
    expected = _parse_iso_date(args.expected_through)
    if not 5 <= args.top_k <= 100:
        raise ScanError("INVALID_TOP_K", "top-k must be between 5 and 100")
    if not 1 <= args.candidate_count <= 100:
        raise ScanError("INVALID_CANDIDATE_COUNT", "candidate-count must be between 1 and 100")
    if not 0 <= args.round_trip_cost_bps <= 500:
        raise ScanError("INVALID_COST", "round-trip-cost-bps must be between 0 and 500")
    if not 10 <= args.timeout_seconds <= 1800:
        raise ScanError("INVALID_TIMEOUT", "timeout-seconds must be between 10 and 1800")
    return ScanConfig(
        expected_through=expected,
        top_k=args.top_k,
        candidate_count=args.candidate_count,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        runner = make_subprocess_runner(args.cli.resolve(), args.timeout_seconds)
        report = run_scan(runner, config)
    except Exception as exc:
        try:
            config
        except UnboundLocalError:
            config = ScanConfig(expected_through=str(args.expected_through))
        report = failed_report(config, exc)
    report["output_path"] = str(args.output.resolve())
    _atomic_write_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "status": report.get("status"),
                "output": str(args.output.resolve()),
                "candidate_count": len(report.get("candidates") or []),
                "errors": report.get("errors") or [],
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
