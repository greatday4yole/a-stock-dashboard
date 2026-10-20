#!/usr/bin/env python3
"""Build a fail-closed 20-session ADV payload for the rebalance engine.

Stock turnover is read from the installed hithink-finance DuckDB via the
vendor CLI.  The vendor database currently contains A shares only, therefore
ETFs may be supplied by already persisted, unmodified ``fund.history`` JSON
response envelopes.  A supplemental envelope is never accepted for a stock.

The builder deliberately returns an unusable payload when any holding lacks a
complete, positive-turnover observation on any of the same twenty market
sessions.  It does not submit orders, fetch remote data, or invent liquidity.
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
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTFOLIO = ROOT / "config" / "portfolio.json"
DEFAULT_POLICY = ROOT / "config" / "risk-policy.json"
DEFAULT_OUTPUT = ROOT / "data" / "liquidity-snapshot.json"
DEFAULT_NODE = ROOT / "data" / "runtime" / "node" / "bin" / "node"
DEFAULT_CLI_ENTRY = (
    ROOT
    / "node_modules"
    / "@hithink-tech"
    / "hithink-finance-cli"
    / "dist"
    / "cli"
    / "main.js"
)
DEFAULT_SUPPLEMENTAL_HISTORY_DIR = ROOT / "data" / "portfolio-analysis-2026-08-31"
CN_TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
PAYLOAD_TYPE = "portfolio-liquidity-adv.v1"
LOOKBACK_SESSIONS = 20
SUPPORTED_DATABASE_SCHEMA = 1
MIN_SUPPORTED_NODE = (22, 12, 0)
MAX_SUPPLEMENTAL_BYTES = 20 * 1024 * 1024
THSCODE_RE = re.compile(r"\d{6}\.(SH|SZ|BJ)")


class LiquidityBuildError(RuntimeError):
    """Expected failure carrying a stable, non-secret reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


CliRunner = Callable[[Sequence[str]], Mapping[str, Any]]
FingerprintReader = Callable[[str], Mapping[str, Any]]


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _safe_text(value: Any, limit: int = 800) -> str:
    text = str(value or "")
    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", text
    )
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
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


def _iso_date(value: Any, label: str) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError as exc:
        raise LiquidityBuildError("INVALID_AS_OF", f"{label} must be YYYY-MM-DD") from exc


def _aware_cn(value: datetime | None) -> datetime:
    current = value or _now()
    if current.utcoffset() is None:
        raise LiquidityBuildError("NAIVE_GENERATED_AT", "generated_at must include a timezone")
    return current.astimezone(CN_TZ)


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise LiquidityBuildError("JSON_ROOT_INVALID", f"JSON root must be an object: {path}")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any], compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def make_cli_runner(
    node: Path = DEFAULT_NODE,
    cli_entry: Path = DEFAULT_CLI_ENTRY,
    timeout_seconds: int = 60,
) -> CliRunner:
    """Return a JSON-only runner pinned to the project's private Node runtime."""

    if not node.is_file() or not os.access(node, os.X_OK):
        raise LiquidityBuildError(
            "PRIVATE_NODE_NOT_FOUND", f"private Node runtime is unavailable: {node}"
        )
    if not cli_entry.is_file():
        raise LiquidityBuildError("CLI_NOT_FOUND", f"hithink-finance CLI is unavailable: {cli_entry}")

    def run(arguments: Sequence[str]) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                [str(node), str(cli_entry), *arguments, "--format", "json"],
                cwd=str(ROOT),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=dict(os.environ),
            )
        except subprocess.TimeoutExpired as exc:
            raise LiquidityBuildError(
                "CLI_TIMEOUT", f"hithink-finance timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise LiquidityBuildError("CLI_EXEC_FAILED", _safe_text(exc)) from exc
        if completed.returncode != 0:
            detail = _safe_text(completed.stderr or completed.stdout)
            raise LiquidityBuildError(
                "CLI_NONZERO_EXIT", detail or f"hithink-finance exit {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LiquidityBuildError("CLI_INVALID_JSON", "hithink-finance returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = payload.get("error") if isinstance(payload, dict) else "invalid response"
            raise LiquidityBuildError("CLI_BUSINESS_ERROR", _safe_text(detail))
        return payload

    return run


def _node_tuple(value: Any) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _database_fingerprint(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve(strict=True)

    def signature(candidate: Path) -> dict[str, Any] | None:
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            return None
        if not candidate.is_file():
            raise LiquidityBuildError("DATABASE_PATH_INVALID", f"not a regular file: {candidate}")
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


def _same_fingerprint(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if dict(before) != dict(after):
        raise LiquidityBuildError(
            "DATABASE_CHANGED_DURING_BUILD",
            "DuckDB changed while ADV evidence was being read; retry after synchronization",
        )


def _expect_data(payload: Mapping[str, Any], command: str) -> Any:
    if payload.get("ok") is not True:
        raise LiquidityBuildError("CLI_BUSINESS_ERROR", f"{command} did not report ok=true")
    return payload.get("data")


def audit_local_database(
    runner: CliRunner, fingerprint_reader: FingerprintReader = _database_fingerprint
) -> dict[str, Any]:
    """Verify CLI capability/schema and freeze a validated local DB identity."""

    version_data = _expect_data(runner(["version"]), "version")
    if not isinstance(version_data, Mapping):
        raise LiquidityBuildError("VERSION_INVALID", "version data is not an object")
    parsed_node = _node_tuple(version_data.get("node"))
    if parsed_node is None or parsed_node < MIN_SUPPORTED_NODE:
        raise LiquidityBuildError("NODE_UNSUPPORTED", "private Node runtime is unsupported")

    capabilities = _expect_data(runner(["capabilities"]), "capabilities")
    if not isinstance(capabilities, list):
        raise LiquidityBuildError("CAPABILITIES_INVALID", "capabilities data is not an array")
    required = {"data.status", "data.validate", "db.query"}
    available = {
        str(row.get("id"))
        for row in capabilities
        if isinstance(row, Mapping) and row.get("id")
    }
    missing = sorted(required - available)
    if missing:
        raise LiquidityBuildError(
            "CAPABILITY_MISSING", "missing capabilities: " + ", ".join(missing)
        )
    for capability in sorted(required):
        schema = _expect_data(runner(["schema", capability]), f"schema {capability}")
        if not isinstance(schema, Mapping) or schema.get("id") != capability:
            raise LiquidityBuildError("SCHEMA_MISMATCH", f"schema mismatch for {capability}")

    status = _expect_data(runner(["data", "status"]), "data.status")
    if not isinstance(status, Mapping):
        raise LiquidityBuildError("DATABASE_STATUS_INVALID", "data.status is not an object")
    path = str(status.get("path") or "")
    if not path:
        raise LiquidityBuildError("DATABASE_PATH_MISSING", "data.status omitted the DuckDB path")
    if _integer(status.get("version")) != SUPPORTED_DATABASE_SCHEMA:
        raise LiquidityBuildError(
            "DATABASE_SCHEMA_UNSUPPORTED",
            f"expected DuckDB schema {SUPPORTED_DATABASE_SCHEMA}",
        )

    before = dict(fingerprint_reader(path))
    validation = _expect_data(
        runner(["--db", path, "data", "validate"]), "data.validate"
    )
    if not isinstance(validation, Mapping):
        raise LiquidityBuildError("DATABASE_VALIDATION_INVALID", "validation data is not an object")
    issues = validation.get("issues")
    if validation.get("ok") is not True or not isinstance(issues, list) or issues:
        raise LiquidityBuildError(
            "DATABASE_VALIDATION_FAILED", "local DuckDB validation did not pass"
        )
    after = dict(fingerprint_reader(path))
    _same_fingerprint(before, after)
    return {
        "path": path,
        "schema_version": SUPPORTED_DATABASE_SCHEMA,
        "cli_version": version_data.get("version"),
        "node_version": version_data.get("node"),
        "fingerprint": after,
    }


def _normalise_portfolio(portfolio: Mapping[str, Any]) -> dict[str, Any]:
    portfolio_id = str(portfolio.get("portfolio_id") or "").strip()
    if not portfolio_id:
        raise LiquidityBuildError("PORTFOLIO_ID_MISSING", "portfolio_id is required")
    portfolio_as_of = _iso_date(portfolio.get("as_of"), "portfolio.as_of")
    accounts = portfolio.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise LiquidityBuildError("PORTFOLIO_ACCOUNTS_INVALID", "portfolio accounts are required")

    symbols: dict[str, dict[str, str]] = {}
    position_count = 0
    for account in accounts:
        if not isinstance(account, Mapping):
            raise LiquidityBuildError("PORTFOLIO_ACCOUNT_INVALID", "portfolio account must be an object")
        positions = account.get("positions")
        if not isinstance(positions, list):
            raise LiquidityBuildError("PORTFOLIO_POSITIONS_INVALID", "account positions must be a list")
        for position in positions:
            position_count += 1
            if not isinstance(position, Mapping):
                raise LiquidityBuildError("PORTFOLIO_POSITION_INVALID", "portfolio position must be an object")
            thscode = str(position.get("thscode") or "").strip().upper()
            if not THSCODE_RE.fullmatch(thscode):
                raise LiquidityBuildError("THSCODE_INVALID", f"invalid portfolio thscode: {thscode!r}")
            instrument_type = str(position.get("instrument_type") or "").strip().lower()
            if instrument_type not in {"stock", "etf"}:
                raise LiquidityBuildError(
                    "INSTRUMENT_TYPE_UNSUPPORTED",
                    f"{thscode} must explicitly declare instrument_type stock or etf",
                )
            existing = symbols.get(thscode)
            if existing and existing["instrument_type"] != instrument_type:
                raise LiquidityBuildError(
                    "INSTRUMENT_TYPE_CONFLICT", f"{thscode} has conflicting instrument types"
                )
            symbols[thscode] = {
                "thscode": thscode,
                "instrument_type": instrument_type,
                "name": str(position.get("name") or thscode),
            }
    if not symbols:
        raise LiquidityBuildError("PORTFOLIO_EMPTY", "portfolio contains no positions")
    return {
        "portfolio_id": portfolio_id,
        "portfolio_as_of": portfolio_as_of,
        "position_count": position_count,
        "symbols": [symbols[key] for key in sorted(symbols)],
    }


def _normalise_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    policy_version = str(policy.get("policy_version") or "").strip()
    if not policy_version:
        raise LiquidityBuildError("POLICY_VERSION_MISSING", "policy_version is required")
    execution = policy.get("execution")
    if not isinstance(execution, Mapping):
        raise LiquidityBuildError("EXECUTION_POLICY_MISSING", "execution policy is required")
    participation = _number(execution.get("maximum_adv_participation_pct_per_day"))
    horizon = _number(execution.get("liquidity_horizon_days"))
    if participation is None or not 0 < participation <= 100:
        raise LiquidityBuildError(
            "ADV_PARTICIPATION_INVALID", "maximum ADV participation must be in (0, 100]"
        )
    if horizon is None or horizon <= 0:
        raise LiquidityBuildError("LIQUIDITY_HORIZON_INVALID", "liquidity horizon must be positive")
    return {
        "policy_version": policy_version,
        "maximum_adv_participation_pct_per_day": participation,
        "liquidity_horizon_days": horizon,
    }


def build_adv_sql(symbols: Sequence[str], as_of: str) -> str:
    """Return one read-only query containing the common sessions and stock rows."""

    requested = sorted(set(symbols))
    if not requested or any(not THSCODE_RE.fullmatch(symbol) for symbol in requested):
        raise LiquidityBuildError("QUERY_SYMBOLS_INVALID", "query symbols must be valid thscodes")
    cutoff = _iso_date(as_of, "as_of")
    values = ",\n        ".join(f"('{symbol}')" for symbol in requested)
    # All symbol/date pairs are materialized.  Missing bars therefore remain
    # visible rather than silently shortening the mean's denominator.
    return f"""
WITH requested(thscode) AS (
    VALUES
        {values}
),
session_window AS (
    SELECT date
    FROM (SELECT DISTINCT date FROM v_daily WHERE date <= DATE '{cutoff}')
    ORDER BY date DESC
    LIMIT {LOOKBACK_SESSIONS}
),
source_bars AS (
    SELECT d.thscode, d.date, COUNT(*) AS source_row_count, MIN(d.amount) AS amount
    FROM v_daily AS d
    INNER JOIN requested AS r ON r.thscode = d.thscode
    INNER JOIN session_window AS s ON s.date = d.date
    GROUP BY d.thscode, d.date
),
evidence AS (
    SELECT
        'meta' AS row_kind,
        NULL::VARCHAR AS thscode,
        NULL::VARCHAR AS trade_date,
        NULL::DOUBLE AS amount,
        NULL::BIGINT AS source_row_count,
        CAST((SELECT MAX(date) FROM v_daily) AS VARCHAR) AS database_max_date,
        CAST((SELECT MAX(date) FROM session_window) AS VARCHAR) AS resolved_as_of
    UNION ALL
    SELECT
        'session', NULL, CAST(s.date AS VARCHAR), NULL, NULL, NULL, NULL
    FROM session_window AS s
    UNION ALL
    SELECT
        'bar', r.thscode, CAST(s.date AS VARCHAR), b.amount,
        COALESCE(b.source_row_count, 0), NULL, NULL
    FROM requested AS r
    CROSS JOIN session_window AS s
    LEFT JOIN source_bars AS b
      ON b.thscode = r.thscode AND b.date = s.date
)
SELECT row_kind, thscode, trade_date, amount, source_row_count,
       database_max_date, resolved_as_of
FROM evidence
ORDER BY CASE row_kind WHEN 'meta' THEN 0 WHEN 'session' THEN 1 ELSE 2 END,
         trade_date, thscode
""".strip()


def _query_database(
    runner: CliRunner,
    database: Mapping[str, Any],
    symbols: Sequence[str],
    as_of: str,
    fingerprint_reader: FingerprintReader,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    sql = build_adv_sql(symbols, as_of)
    before = dict(fingerprint_reader(str(database["path"])))
    _same_fingerprint(database["fingerprint"], before)
    response = runner(["--db", str(database["path"]), "db", "query", "--sql", sql])
    after = dict(fingerprint_reader(str(database["path"])))
    _same_fingerprint(before, after)
    rows = _expect_data(response, "db.query")
    meta = response.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, Mapping):
        raise LiquidityBuildError("QUERY_RESPONSE_INVALID", "db.query response shape is invalid")
    if meta.get("truncated") is not False or _integer(meta.get("count")) != len(rows):
        raise LiquidityBuildError("QUERY_RESPONSE_INCOMPLETE", "db.query result is truncated or uncounted")

    meta_rows = [row for row in rows if isinstance(row, Mapping) and row.get("row_kind") == "meta"]
    if len(meta_rows) != 1:
        raise LiquidityBuildError("QUERY_META_INVALID", "ADV query must return exactly one meta row")
    meta_row = dict(meta_rows[0])
    sessions_raw = [
        str(row.get("trade_date") or "")
        for row in rows
        if isinstance(row, Mapping) and row.get("row_kind") == "session"
    ]
    try:
        sessions = sorted({_iso_date(value, "query session") for value in sessions_raw})
    except LiquidityBuildError as exc:
        raise LiquidityBuildError("QUERY_SESSION_INVALID", str(exc)) from exc
    if len(sessions_raw) != len(sessions) or len(sessions) != LOOKBACK_SESSIONS:
        raise LiquidityBuildError(
            "SESSION_WINDOW_INCOMPLETE",
            f"expected {LOOKBACK_SESSIONS} distinct completed sessions, got {len(sessions)}",
        )
    if sessions[-1] != as_of or str(meta_row.get("resolved_as_of") or "") != as_of:
        raise LiquidityBuildError(
            "DATABASE_NOT_CURRENT_AS_OF",
            f"DuckDB does not contain the bound session {as_of}",
        )

    bar_rows = [row for row in rows if isinstance(row, Mapping) and row.get("row_kind") == "bar"]
    expected = len(set(symbols)) * LOOKBACK_SESSIONS
    if len(rows) != 1 + LOOKBACK_SESSIONS + expected:
        raise LiquidityBuildError(
            "QUERY_ROWS_UNEXPECTED", "ADV query returned rows outside the declared evidence grid"
        )
    if len(bar_rows) != expected:
        raise LiquidityBuildError(
            "QUERY_GRID_INCOMPLETE", f"expected {expected} symbol/session rows, got {len(bar_rows)}"
        )
    by_symbol: dict[str, dict[str, Any]] = {symbol: {} for symbol in sorted(set(symbols))}
    for row in bar_rows:
        symbol = str(row.get("thscode") or "")
        trade_date = str(row.get("trade_date") or "")
        if symbol not in by_symbol or trade_date not in sessions or trade_date in by_symbol[symbol]:
            raise LiquidityBuildError("QUERY_GRID_INVALID", "ADV query grid contains an unknown or duplicate key")
        by_symbol[symbol][trade_date] = {
            "amount": row.get("amount"),
            "source_row_count": row.get("source_row_count"),
        }
    for symbol, values_by_date in by_symbol.items():
        if set(values_by_date) != set(sessions):
            raise LiquidityBuildError("QUERY_GRID_INVALID", f"query grid is incomplete for {symbol}")
    query_evidence = {
        "query_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "database_max_date": str(meta_row.get("database_max_date") or ""),
        "resolved_as_of": as_of,
        "row_count": len(rows),
    }
    return sessions, by_symbol, query_evidence


def _adv_record(
    symbol: Mapping[str, str],
    sessions: Sequence[str],
    amounts: Sequence[float],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    average = sum(amounts) / len(amounts)
    return {
        "thscode": symbol["thscode"],
        "name": symbol["name"],
        "instrument_type": symbol["instrument_type"],
        "adv_cny": round(average, 2),
        "average_daily_turnover_cny": round(average, 2),
        "adv20_cny": round(average, 2),
        "window_session_count": len(sessions),
        "observed_session_count": len(amounts),
        "window_start": sessions[0],
        "window_end": sessions[-1],
        "latest_turnover_cny": round(amounts[-1], 2),
        "minimum_turnover_cny": round(min(amounts), 2),
        "maximum_turnover_cny": round(max(amounts), 2),
        "calculation": "arithmetic_mean_of_exact_common_20_sessions",
        "source": dict(source),
    }


def _stock_record(
    symbol: Mapping[str, str],
    sessions: Sequence[str],
    grid: Mapping[str, Mapping[str, Any]],
    database: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    amounts: list[float] = []
    for session in sessions:
        row = grid.get(session)
        count = _integer(row.get("source_row_count")) if isinstance(row, Mapping) else None
        amount = _number(row.get("amount")) if isinstance(row, Mapping) else None
        if count != 1:
            errors.append(
                {
                    "code": "STOCK_BAR_COUNT_INVALID",
                    "thscode": symbol["thscode"],
                    "date": session,
                    "message": "expected exactly one DuckDB v_daily row",
                }
            )
        elif amount is None or amount <= 0:
            errors.append(
                {
                    "code": "STOCK_TURNOVER_INVALID",
                    "thscode": symbol["thscode"],
                    "date": session,
                    "message": "DuckDB amount must be finite and positive",
                }
            )
        else:
            amounts.append(amount)
    if errors or len(amounts) != LOOKBACK_SESSIONS:
        return None, errors
    source = {
        "kind": "hithink_duckdb",
        "table": "v_daily",
        "field": "amount",
        "unit": "CNY",
        "database_schema_version": database["schema_version"],
        "database_snapshot_sha256": _canonical_hash(database["fingerprint"]),
    }
    return _adv_record(symbol, sessions, amounts, source), []


def _supplemental_filename(thscode: str) -> str:
    ticker, exchange = thscode.split(".")
    return f"history-{ticker}-{exchange}.json"


def _timestamp_date(value: Any) -> str | None:
    milliseconds = _integer(value)
    if milliseconds is None or milliseconds < 0:
        return None
    try:
        # Vendor daily-bar timestamps denote 00:00 in Asia/Shanghai (16:00 UTC
        # on the preceding civil day).  Converting in UTC would shift every
        # Friday bar to Thursday and silently destroy the common-session grid.
        observed = datetime.fromtimestamp(milliseconds / 1000, tz=CN_TZ)
        if observed.time() != datetime.min.time():
            return None
        return observed.date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _etf_record(
    symbol: Mapping[str, str],
    sessions: Sequence[str],
    history_dir: Path | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    thscode = symbol["thscode"]
    if history_dir is None:
        return None, [{
            "code": "ETF_SUPPLEMENT_DISABLED",
            "thscode": thscode,
            "message": "ETF fund.history supplemental evidence is disabled",
        }], None
    path = history_dir / _supplemental_filename(thscode)
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_SUPPLEMENTAL_BYTES:
            raise OSError("not a bounded regular file")
        before_signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
        raw = path.read_bytes()
        after_stat = path.stat()
        after_signature = (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_size,
            after_stat.st_mtime_ns,
        )
        if before_signature != after_signature or len(raw) != after_stat.st_size:
            raise OSError("file changed while it was read")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [{
            "code": "ETF_SUPPLEMENT_UNREADABLE",
            "thscode": thscode,
            "message": _safe_text(f"invalid supplemental file {path.name}: {exc}"),
        }], None

    errors: list[dict[str, Any]] = []
    data = payload.get("data") if isinstance(payload, Mapping) else None
    meta = payload.get("meta") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        errors.append({"code": "ETF_ENVELOPE_NOT_OK", "thscode": thscode, "message": "fund.history envelope must report ok=true"})
    if not isinstance(payload, Mapping) or payload.get("command") != "fund.history":
        errors.append({"code": "ETF_COMMAND_MISMATCH", "thscode": thscode, "message": "supplement must be an exact fund.history response"})
    if not isinstance(data, Mapping):
        errors.append({"code": "ETF_DATA_INVALID", "thscode": thscode, "message": "fund.history data must be an object"})
        data = {}
    if not isinstance(meta, Mapping):
        errors.append({"code": "ETF_META_INVALID", "thscode": thscode, "message": "fund.history meta must be an object"})
        meta = {}
    if data.get("thscode") != thscode:
        errors.append({"code": "ETF_SYMBOL_MISMATCH", "thscode": thscode, "message": "fund.history thscode does not match the holding"})
    if data.get("interval") != "1d":
        errors.append({"code": "ETF_INTERVAL_MISMATCH", "thscode": thscode, "message": "fund.history interval must be 1d"})
    if meta.get("source") != "remote":
        errors.append({"code": "ETF_PROVENANCE_INVALID", "thscode": thscode, "message": "fund.history meta.source must be remote"})
    if str(meta.get("schema_version") or "") != "1" or not str(meta.get("request_id") or "").strip():
        errors.append({"code": "ETF_PROVENANCE_INCOMPLETE", "thscode": thscode, "message": "fund.history provenance requires schema_version=1 and request_id"})
    if meta.get("truncated") is not False:
        errors.append({"code": "ETF_TRUNCATED", "thscode": thscode, "message": "fund.history response must explicitly be untruncated"})
    items = data.get("item")
    if not isinstance(items, list):
        errors.append({"code": "ETF_ITEMS_INVALID", "thscode": thscode, "message": "fund.history item must be an array"})
        items = []
    if _integer(meta.get("count")) != len(items):
        errors.append({"code": "ETF_COUNT_MISMATCH", "thscode": thscode, "message": "fund.history meta.count must equal item length"})

    amounts_by_date: dict[str, float] = {}
    observed_dates: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            errors.append({"code": "ETF_BAR_INVALID", "thscode": thscode, "message": "fund.history bar must be an object"})
            continue
        session = _timestamp_date(item.get("date_ms"))
        amount = _number(item.get("turnover"))
        if session is None or amount is None or amount <= 0:
            errors.append({"code": "ETF_BAR_INVALID", "thscode": thscode, "message": "ETF date_ms/turnover must be valid and positive"})
            continue
        if session in amounts_by_date:
            errors.append({"code": "ETF_DUPLICATE_SESSION", "thscode": thscode, "date": session, "message": "fund.history contains a duplicate session"})
            continue
        amounts_by_date[session] = amount
        observed_dates.append(session)
    source_dates = sorted(amounts_by_date)
    if observed_dates != source_dates:
        errors.append({"code": "ETF_SESSION_ORDER_INVALID", "thscode": thscode, "message": "fund.history sessions must be strictly increasing"})
    timestamp_date = _timestamp_date(data.get("timestamp"))
    if not source_dates or timestamp_date != source_dates[-1]:
        errors.append({"code": "ETF_TIMESTAMP_MISMATCH", "thscode": thscode, "message": "fund.history timestamp must equal the latest source bar date"})
    missing_sessions = sorted(set(sessions) - set(amounts_by_date))
    if missing_sessions:
        errors.append({
            "code": "ETF_WINDOW_INCOMPLETE",
            "thscode": thscode,
            "message": f"ETF is missing {len(missing_sessions)} common sessions",
            "missing_sessions": missing_sessions,
        })
    if errors:
        return None, errors, {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    amounts = [amounts_by_date[session] for session in sessions]
    excluded_future = sum(session > sessions[-1] for session in source_dates)
    source = {
        "kind": "hithink_fund_history_envelope",
        "command": "fund.history",
        "field": "turnover",
        "unit": "CNY",
        "meta_source": "remote",
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_min_date": source_dates[0],
        "source_max_date": source_dates[-1],
        "future_rows_excluded": excluded_future,
    }
    evidence = {"path": source["path"], "sha256": source["sha256"]}
    return _adv_record(symbol, sessions, amounts, source), [], evidence


def _failure_payload(
    base: Mapping[str, Any], error: LiquidityBuildError | Exception
) -> dict[str, Any]:
    code = error.code if isinstance(error, LiquidityBuildError) else "UNEXPECTED_BUILD_ERROR"
    return {
        **base,
        "ok": False,
        "status": "failed",
        "stale": True,
        "input_errors": [{"code": code, "message": _safe_text(error)}],
        "positions": [],
        "coverage": {
            "complete": False,
            "required_symbol_count": 0,
            "valid_symbol_count": 0,
            "coverage_ratio": 0.0,
        },
    }


def build_liquidity_payload(
    portfolio: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    as_of: str | None = None,
    generated_at: datetime | None = None,
    runner: CliRunner | None = None,
    fingerprint_reader: FingerprintReader = _database_fingerprint,
    supplemental_history_dir: Path | None = DEFAULT_SUPPLEMENTAL_HISTORY_DIR,
) -> dict[str, Any]:
    """Build a rebalance-compatible payload; all expected failures fail closed."""

    try:
        observed = _aware_cn(generated_at)
    except Exception as exc:
        observed = _now()
        base = {
            "schema_version": SCHEMA_VERSION,
            "payload_type": PAYLOAD_TYPE,
            "generated_at": observed.isoformat(timespec="seconds"),
        }
        return _failure_payload(base, exc)

    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": PAYLOAD_TYPE,
        "generated_at": observed.isoformat(timespec="seconds"),
        "lookback_sessions": LOOKBACK_SESSIONS,
    }
    try:
        portfolio_info = _normalise_portfolio(portfolio)
        policy_info = _normalise_policy(policy)
        cutoff = _iso_date(as_of or portfolio_info["portfolio_as_of"], "as_of")
        if cutoff != portfolio_info["portfolio_as_of"]:
            raise LiquidityBuildError(
                "AS_OF_BINDING_MISMATCH", "liquidity as_of must equal portfolio.as_of"
            )
        base.update(
            {
                "portfolio_id": portfolio_info["portfolio_id"],
                "portfolio_as_of": portfolio_info["portfolio_as_of"],
                "policy_version": policy_info["policy_version"],
                "data_as_of": cutoff,
            }
        )
        active_runner = runner or make_cli_runner()
        database = audit_local_database(active_runner, fingerprint_reader)
        symbols = [row["thscode"] for row in portfolio_info["symbols"]]
        sessions, grid, query_evidence = _query_database(
            active_runner, database, symbols, cutoff, fingerprint_reader
        )

        input_errors: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        supplemental_evidence: list[dict[str, Any]] = []
        for symbol in portfolio_info["symbols"]:
            if symbol["instrument_type"] == "stock":
                record, errors = _stock_record(
                    symbol, sessions, grid[symbol["thscode"]], database
                )
                evidence = None
            else:
                record, errors, evidence = _etf_record(
                    symbol, sessions, supplemental_history_dir
                )
            input_errors.extend(errors)
            if record is not None:
                positions.append(record)
            if evidence is not None:
                supplemental_evidence.append(evidence)

        required = len(portfolio_info["symbols"])
        valid = len(positions)
        valid_symbols = {row["thscode"] for row in positions}
        missing_symbols = sorted(set(symbols) - valid_symbols)
        coverage = {
            "complete": not input_errors and valid == required,
            "portfolio_position_count": portfolio_info["position_count"],
            "required_symbol_count": required,
            "valid_symbol_count": valid,
            "coverage_ratio": round(valid / required, 6),
            "missing_or_invalid_symbols": missing_symbols,
            "required_session_count": LOOKBACK_SESSIONS,
            "common_session_count": len(sessions),
        }
        if not coverage["complete"] and not input_errors:
            input_errors.append(
                {
                    "code": "COVERAGE_INCOMPLETE",
                    "message": "not every portfolio symbol has valid ADV evidence",
                }
            )

        evidence = {
            "database": {
                "path": database["path"],
                "schema_version": database["schema_version"],
                "cli_version": database["cli_version"],
                "node_version": database["node_version"],
                "snapshot_fingerprint_sha256": _canonical_hash(database["fingerprint"]),
            },
            "query": query_evidence,
            "supplemental_files": sorted(
                supplemental_evidence, key=lambda row: str(row.get("path") or "")
            ),
        }
        evidence["evidence_sha256"] = _canonical_hash(evidence)
        binding = {
            "portfolio_id": portfolio_info["portfolio_id"],
            "portfolio_as_of": portfolio_info["portfolio_as_of"],
            "policy_version": policy_info["policy_version"],
            "data_as_of": cutoff,
            "generated_at": base["generated_at"],
            "lookback_sessions": LOOKBACK_SESSIONS,
            "window_start": sessions[0],
            "window_end": sessions[-1],
            "session_calendar_sha256": _canonical_hash(list(sessions)),
            "source_evidence_sha256": evidence["evidence_sha256"],
            "portfolio_sha256": _canonical_hash(portfolio),
            "policy_sha256": _canonical_hash(policy),
        }
        binding["binding_sha256"] = _canonical_hash(binding)
        ok = not input_errors and coverage["complete"]
        return {
            **base,
            "ok": ok,
            "status": "fresh" if ok else "failed",
            "stale": not ok,
            "input_binding": binding,
            "data_window": {
                "basis": "latest_20_common_completed_market_sessions_at_or_before_bound_as_of",
                "cutoff_inclusive": True,
                "session_count": len(sessions),
                "start": sessions[0],
                "end": sessions[-1],
                "sessions": list(sessions),
                "future_data_used": False,
            },
            "coverage": coverage,
            "input_errors": input_errors,
            "positions": positions,
            "evidence": evidence,
            "policy_context": policy_info,
        }
    except Exception as exc:
        return _failure_payload(base, exc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a fail-closed 20-session portfolio ADV payload"
    )
    parser.add_argument("--portfolio-file", type=Path, default=DEFAULT_PORTFOLIO)
    parser.add_argument("--policy-file", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--as-of", help="Must equal portfolio.as_of (YYYY-MM-DD)")
    parser.add_argument(
        "--supplemental-history-dir",
        type=Path,
        default=DEFAULT_SUPPLEMENTAL_HISTORY_DIR,
        help="Directory containing persisted fund.history envelopes for ETFs",
    )
    parser.add_argument(
        "--no-supplemental-etf",
        action="store_true",
        help="Disable ETF supplemental envelopes; ETF holdings then fail closed",
    )
    parser.add_argument("--output", type=Path, help="Atomically write JSON instead of stdout")
    parser.add_argument("--compact", action="store_true")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        portfolio = _read_json(args.portfolio_file)
        policy = _read_json(args.policy_file)
        payload = build_liquidity_payload(
            portfolio,
            policy,
            as_of=args.as_of,
            supplemental_history_dir=(
                None if args.no_supplemental_etf else args.supplemental_history_dir
            ),
        )
    except Exception as exc:
        payload = _failure_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "payload_type": PAYLOAD_TYPE,
                "generated_at": _now().isoformat(timespec="seconds"),
            },
            exc,
        )
    if args.output:
        _atomic_write(args.output, payload, args.compact)
    else:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=None if args.compact else 2,
                separators=(",", ":") if args.compact else None,
                allow_nan=False,
            )
        )
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(_main())
