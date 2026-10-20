#!/usr/bin/env python3
"""Tamper-evident, append-only forward validation for watchlist research states.

The module deliberately does not select securities or emit trading actions.  A
completed-session market snapshot and the opportunity radar are preregistered
on the same China calendar day.  Outcomes are written once, and only on the
exact 5/20/60-session due date, when every required stock and CSI 300 close is
present in one validated daily snapshot.

The JSONL ledger is protected by a SHA-256 hash chain.  Every append opens the
ledger with O_APPEND, takes an exclusive fcntl lock, verifies the complete
chain while holding that lock, appends canonical JSON, and fsyncs before
unlocking.  Corrupt, truncated, reordered, or non-canonical ledgers are never
repaired automatically and cannot be appended to.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


CN_TZ = timezone(timedelta(hours=8))
ENGINE_VERSION = "2026-08-31.1"
LEDGER_SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
EVENT_PREREGISTRATION = "RESEARCH_STATE_PREREGISTRATION"
EVENT_OUTCOME = "HORIZON_OUTCOME"
ALLOWED_RESEARCH_STATES = frozenset(
    {
        "INVALIDATED",
        "RISK_REDUCTION",
        "PULLBACK_ZONE",
        "BREAKOUT_PENDING",
        "CONFIRMED",
        "WATCH",
    }
)
MAX_LEDGER_FUTURE_SKEW_SECONDS = 300
MAX_LEDGER_BYTES = 128 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT_ROOT / "config" / "forward-validation-policy.json"
DEFAULT_LEDGER = PROJECT_ROOT / "data" / "forward-validation.jsonl"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_THSCODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")


class ForwardValidationError(RuntimeError):
    """Base class with a stable machine-readable error code."""

    code = "FORWARD_VALIDATION_ERROR"


class LedgerIntegrityError(ForwardValidationError):
    code = "LEDGER_INTEGRITY_ERROR"


class InputValidationError(ForwardValidationError):
    code = "INPUT_VALIDATION_ERROR"


class DuplicateRegistrationError(InputValidationError):
    code = "DUPLICATE_REGISTRATION"


class BackfillRejectedError(InputValidationError):
    code = "BACKFILL_REJECTED"


class PolicyMismatchError(InputValidationError):
    code = "POLICY_BINDING_MISMATCH"


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InputValidationError("value is not canonical-JSON serializable") from exc


def document_sha256(value: Any) -> str:
    """Hash a JSON document by canonical semantic content."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InputValidationError("%s must be a finite number" % field) from exc
    if not math.isfinite(result):
        raise InputValidationError("%s must be a finite number" % field)
    return result


def _positive_number(value: Any, field: str) -> float:
    result = _number(value, field)
    if result <= 0:
        raise InputValidationError("%s must be positive" % field)
    return result


def _parse_date(value: Any, field: str) -> date:
    raw = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise InputValidationError("%s must be YYYY-MM-DD" % field)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InputValidationError("%s is not a valid date" % field) from exc


def _parse_datetime(value: Any, field: str) -> datetime:
    raw = str(value or "")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InputValidationError("%s must be an ISO-8601 timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputValidationError("%s must include a UTC offset" % field)
    return parsed.astimezone(CN_TZ)


def _now_cn(now: Optional[datetime]) -> datetime:
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None or current.utcoffset() is None:
        raise InputValidationError("now must include a UTC offset")
    return current.astimezone(CN_TZ)


def _parse_cutoff(value: Any, field: str) -> time:
    try:
        parsed = time.fromisoformat(str(value))
    except ValueError as exc:
        raise InputValidationError("%s must be HH:MM:SS" % field) from exc
    if parsed.tzinfo is not None:
        raise InputValidationError("%s must be a China-local wall-clock time" % field)
    return parsed


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InputValidationError("%s must be an object" % field)
    return value


def _as_list(value: Any, field: str) -> List[Any]:
    if not isinstance(value, list):
        raise InputValidationError("%s must be an array" % field)
    return value


def _validate_thscode(value: Any, field: str) -> str:
    result = str(value or "").upper()
    if not _THSCODE_RE.fullmatch(result):
        raise InputValidationError("%s must be a six-digit SH/SZ/BJ thscode" % field)
    return result


def _symbol_from_thscode(thscode: str) -> str:
    code, market = thscode.split(".", 1)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market] + code


def load_json_document(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputValidationError("cannot read valid JSON from %s" % path) from exc
    if not isinstance(value, dict):
        raise InputValidationError("JSON document %s must be an object" % path)
    # Reject NaN/Infinity accepted by Python's permissive decoder.
    _canonical_json(value)
    return value


def load_policy(path: Path = DEFAULT_POLICY) -> Dict[str, Any]:
    policy = load_json_document(path)
    validate_policy(policy)
    return policy


def validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("schema_version") != 1:
        raise InputValidationError("unsupported forward-validation policy schema")
    if policy.get("engine_version") != ENGINE_VERSION:
        raise PolicyMismatchError("policy engine_version does not match this engine")
    if policy.get("scope") != "configured_watchlist_research_states_only":
        raise InputValidationError("policy scope must stay watchlist-research-only")
    if not str(policy.get("ledger_epoch") or ""):
        raise InputValidationError("policy ledger_epoch is required")
    benchmark = _as_mapping(policy.get("benchmark"), "policy.benchmark")
    if _validate_thscode(benchmark.get("thscode"), "policy.benchmark.thscode") != "000300.SH":
        raise InputValidationError("the benchmark must be CSI 300 (000300.SH)")
    if not str(benchmark.get("snapshot_name") or ""):
        raise InputValidationError("policy.benchmark.snapshot_name is required")
    horizons = _as_list(policy.get("horizons_sessions"), "policy.horizons_sessions")
    if horizons != [5, 20, 60]:
        raise InputValidationError("horizons_sessions must be exactly [5, 20, 60]")
    registration = _as_mapping(policy.get("registration"), "policy.registration")
    outcome = _as_mapping(policy.get("outcome"), "policy.outcome")
    for section_name, section in (("registration", registration), ("outcome", outcome)):
        if section.get("same_cn_calendar_day_only") is not True:
            raise InputValidationError("policy.%s must prohibit backfill" % section_name)
        _parse_cutoff(
            section.get("completed_session_cutoff_cn"),
            "policy.%s.completed_session_cutoff_cn" % section_name,
        )
        tolerance = section.get("future_tolerance_seconds")
        if not isinstance(tolerance, int) or not 0 <= tolerance <= 300:
            raise InputValidationError(
                "policy.%s.future_tolerance_seconds must be an integer in [0, 300]"
                % section_name
            )
        if section.get("require_market_validation_passed") is not True:
            raise InputValidationError("policy.%s must require validated market data" % section_name)
    if registration.get("require_complete_strategy_coverage") is not True:
        raise InputValidationError("registration must cover the entire configured strategy")
    if str(registration.get("required_equity_adjustment_token") or "").lower() != "qfq":
        raise InputValidationError("registration equity evidence must be forward-adjusted")
    allowed = _as_list(
        registration.get("allowed_research_states"),
        "policy.registration.allowed_research_states",
    )
    if not allowed or "DATA_UNVERIFIED" in allowed or len(set(allowed)) != len(allowed):
        raise InputValidationError("allowed research states are invalid")
    if not set(allowed).issubset(ALLOWED_RESEARCH_STATES):
        raise InputValidationError("policy contains an unsupported research state")
    if registration.get("require_item_quote_timestamp") is not True:
        raise InputValidationError("registration must bind every item quote timestamp")
    if registration.get("require_watchlist_current") is not True:
        raise InputValidationError("registration must reject expired watchlists")
    _parse_cutoff(registration.get("quote_not_before_cn"), "policy.registration.quote_not_before_cn")
    if outcome.get("benchmark_calendar_is_session_authority") is not True:
        raise InputValidationError("CSI 300 dates must be the session authority")
    if outcome.get("require_every_stock_session") is not True:
        raise InputValidationError("outcomes must require every stock session")
    if outcome.get("settle_only_on_exact_due_session") is not True:
        raise InputValidationError("outcome backfill must remain disabled")
    if str(outcome.get("required_equity_adjustment_token") or "").lower() != "qfq":
        raise InputValidationError("outcome equity evidence must be forward-adjusted")


def _record_hash(record_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(record_without_hash).encode("utf-8")).hexdigest()


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_LEDGER_BYTES:
            raise LedgerIntegrityError(
                "forward-validation ledger exceeds the hard size limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_records(raw: bytes) -> List[Dict[str, Any]]:
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise LedgerIntegrityError("ledger has a truncated final line")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerIntegrityError("ledger is not valid UTF-8") from exc
    records = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise LedgerIntegrityError("ledger contains a blank line at %d" % line_number)
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError("invalid JSON at ledger line %d" % line_number) from exc
        if not isinstance(value, dict):
            raise LedgerIntegrityError("ledger line %d is not an object" % line_number)
        try:
            canonical_line = _canonical_json(value)
        except InputValidationError as exc:
            raise LedgerIntegrityError("non-finite value at ledger line %d" % line_number) from exc
        if line != canonical_line:
            raise LedgerIntegrityError("ledger line %d is not canonical JSON" % line_number)
        records.append(value)
    return records


def _validate_record_chain(records: Sequence[Mapping[str, Any]]) -> str:
    previous_hash = GENESIS_HASH
    registrations = {}
    outcome_keys = set()
    outcome_ids = set()
    last_recorded = None  # type: Optional[datetime]
    last_registration_date = None  # type: Optional[date]
    epoch_binding = None
    exact_keys = {
        "schema_version",
        "sequence",
        "event_type",
        "recorded_at",
        "previous_hash",
        "payload",
        "record_hash",
    }
    for index, record in enumerate(records, 1):
        if set(record) != exact_keys:
            raise LedgerIntegrityError("ledger record %d has unexpected fields" % index)
        if record.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise LedgerIntegrityError("ledger record %d has unsupported schema" % index)
        if record.get("sequence") != index:
            raise LedgerIntegrityError("ledger sequence is not contiguous at record %d" % index)
        if record.get("previous_hash") != previous_hash:
            raise LedgerIntegrityError("hash-chain predecessor mismatch at record %d" % index)
        actual_hash = str(record.get("record_hash") or "")
        if not _HASH_RE.fullmatch(actual_hash):
            raise LedgerIntegrityError("invalid record hash at record %d" % index)
        unsigned = dict(record)
        unsigned.pop("record_hash")
        if _record_hash(unsigned) != actual_hash:
            raise LedgerIntegrityError("record hash mismatch at record %d" % index)
        try:
            recorded_at = _parse_datetime(record.get("recorded_at"), "recorded_at")
        except InputValidationError as exc:
            raise LedgerIntegrityError("invalid recorded_at at record %d" % index) from exc
        if last_recorded is not None and recorded_at < last_recorded:
            raise LedgerIntegrityError("recorded_at moves backwards at record %d" % index)
        last_recorded = recorded_at
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise LedgerIntegrityError("payload is not an object at record %d" % index)
        event_type = record.get("event_type")
        if event_type == EVENT_PREREGISTRATION:
            registration_id = str(payload.get("registration_id") or "")
            if not registration_id or registration_id in registrations:
                raise LedgerIntegrityError("duplicate/empty registration id at record %d" % index)
            try:
                session_date = _parse_date(payload.get("session_date"), "session_date")
            except InputValidationError as exc:
                raise LedgerIntegrityError("invalid registration date at record %d" % index) from exc
            if session_date != recorded_at.date():
                raise LedgerIntegrityError("registration is a backfill/future record at %d" % index)
            if last_registration_date is not None and session_date <= last_registration_date:
                raise LedgerIntegrityError("registration dates are duplicated or non-forward at %d" % index)
            last_registration_date = session_date
            horizons = payload.get("horizons_sessions")
            if horizons != [5, 20, 60]:
                raise LedgerIntegrityError("registration horizons changed at record %d" % index)
            binding = payload.get("binding")
            states = payload.get("research_states")
            if not isinstance(binding, Mapping) or not isinstance(states, list) or not states:
                raise LedgerIntegrityError("registration binding/states invalid at record %d" % index)
            if (
                payload.get("research_only") is not True
                or payload.get("trading_instruction") is not False
                or payload.get("full_market_selector") is not False
                or payload.get("scope") != "configured_watchlist_research_states_only"
            ):
                raise LedgerIntegrityError("registration scope changed at record %d" % index)
            current_epoch = (
                binding.get("ledger_epoch"),
                binding.get("engine_version"),
                binding.get("policy_sha256"),
                binding.get("strategy_sha256"),
            )
            if any(not value for value in current_epoch):
                raise LedgerIntegrityError("registration binding incomplete at record %d" % index)
            if epoch_binding is None:
                epoch_binding = current_epoch
            elif current_epoch != epoch_binding:
                raise LedgerIntegrityError("ledger policy/strategy epoch changed at record %d" % index)
            symbols = []
            for state in states:
                if not isinstance(state, Mapping):
                    raise LedgerIntegrityError("invalid research state at record %d" % index)
                try:
                    symbol = _validate_thscode(state.get("thscode"), "research_state.thscode")
                    _positive_number(state.get("reference_close"), "research_state.reference_close")
                except InputValidationError as exc:
                    raise LedgerIntegrityError("invalid research state at record %d" % index) from exc
                if not str(state.get("research_state") or ""):
                    raise LedgerIntegrityError("empty research state at record %d" % index)
                if state.get("research_state") not in ALLOWED_RESEARCH_STATES:
                    raise LedgerIntegrityError("unsupported research state at record %d" % index)
                symbols.append(symbol)
            if len(symbols) != len(set(symbols)):
                raise LedgerIntegrityError("duplicate symbols in registration %s" % registration_id)
            registrations[registration_id] = payload
        elif event_type == EVENT_OUTCOME:
            registration_id = str(payload.get("registration_id") or "")
            registration = registrations.get(registration_id)
            if registration is None:
                raise LedgerIntegrityError("outcome precedes/omits registration at record %d" % index)
            try:
                symbol = _validate_thscode(payload.get("thscode"), "outcome.thscode")
                horizon = int(payload.get("horizon_sessions"))
                target_date = _parse_date(payload.get("target_session"), "target_session")
            except (InputValidationError, TypeError, ValueError) as exc:
                raise LedgerIntegrityError("invalid outcome identity at record %d" % index) from exc
            key = (registration_id, symbol, horizon)
            if key in outcome_keys:
                raise LedgerIntegrityError("duplicate horizon outcome at record %d" % index)
            outcome_keys.add(key)
            outcome_id = str(payload.get("outcome_id") or "")
            if not outcome_id or outcome_id in outcome_ids:
                raise LedgerIntegrityError("duplicate/empty outcome id at record %d" % index)
            outcome_ids.add(outcome_id)
            if horizon not in registration.get("horizons_sessions", []):
                raise LedgerIntegrityError("outcome horizon was not preregistered")
            registered_symbols = {
                item.get("thscode") for item in registration.get("research_states", [])
            }
            if symbol not in registered_symbols:
                raise LedgerIntegrityError("outcome symbol was not preregistered")
            if target_date != recorded_at.date():
                raise LedgerIntegrityError("outcome was not recorded on its exact due session")
            registration_date = _parse_date(
                registration.get("session_date"), "registration.session_date"
            )
            if target_date <= registration_date:
                raise LedgerIntegrityError("outcome target does not follow registration")
            registration_binding = registration.get("binding") or {}
            binding = payload.get("binding")
            if not isinstance(binding, Mapping):
                raise LedgerIntegrityError("outcome binding is missing")
            expected_binding = {
                "registration_engine_version": registration_binding.get("engine_version"),
                "registration_policy_sha256": registration_binding.get("policy_sha256"),
                "registration_strategy_sha256": registration_binding.get("strategy_sha256"),
                "registration_input_bundle_sha256": registration_binding.get("input_bundle_sha256"),
            }
            for key_name, expected in expected_binding.items():
                if binding.get(key_name) != expected:
                    raise LedgerIntegrityError("outcome binding mismatch: %s" % key_name)
            registered_state = next(
                item for item in registration.get("research_states", []) if item.get("thscode") == symbol
            )
            if payload.get("research_state") != registered_state.get("research_state"):
                raise LedgerIntegrityError("outcome research state differs from preregistration")
            if payload.get("status") != "OBSERVED":
                raise LedgerIntegrityError("outcome status is not an observation")
            if payload.get("research_only") is not True or payload.get("trading_instruction") is not False:
                raise LedgerIntegrityError("outcome was relabeled as a trading instruction")
            for field in (
                "stock_return_pct",
                "benchmark_return_pct",
                "excess_return_pct_points",
                "relative_return_pct",
            ):
                try:
                    _number(payload.get(field), "outcome.%s" % field)
                except InputValidationError as exc:
                    raise LedgerIntegrityError("invalid outcome metric at record %d" % index) from exc
            try:
                stock_start = _positive_number(
                    payload.get("stock_start_close_from_same_evidence"), "outcome.stock_start_close"
                )
                stock_end = _positive_number(payload.get("stock_target_close"), "outcome.stock_target_close")
                benchmark_start = _positive_number(
                    payload.get("benchmark_start_close"), "outcome.benchmark_start_close"
                )
                benchmark_end = _positive_number(
                    payload.get("benchmark_target_close"), "outcome.benchmark_target_close"
                )
            except InputValidationError as exc:
                raise LedgerIntegrityError("invalid outcome close at record %d" % index) from exc
            stock_return = (stock_end / stock_start - 1.0) * 100.0
            benchmark_return = (benchmark_end / benchmark_start - 1.0) * 100.0
            expected_metrics = {
                "stock_return_pct": stock_return,
                "benchmark_return_pct": benchmark_return,
                "excess_return_pct_points": stock_return - benchmark_return,
                "relative_return_pct": (
                    (stock_end / stock_start) / (benchmark_end / benchmark_start) - 1.0
                )
                * 100.0,
            }
            for metric, expected in expected_metrics.items():
                if not math.isclose(
                    float(payload.get(metric)), expected, rel_tol=0.0, abs_tol=1e-7
                ):
                    raise LedgerIntegrityError("outcome arithmetic mismatch: %s" % metric)
            evidence = payload.get("evidence")
            if not isinstance(evidence, Mapping):
                raise LedgerIntegrityError("outcome evidence binding is missing")
            if evidence.get("required_session_count") != horizon + 1:
                raise LedgerIntegrityError("outcome session count does not match horizon")
            if evidence.get("data_date") != target_date.isoformat():
                raise LedgerIntegrityError("outcome evidence date differs from due session")
            if evidence.get("outcome_evidence_sha256") != binding.get("outcome_evidence_sha256"):
                raise LedgerIntegrityError("outcome evidence hash is inconsistent")
            if not _HASH_RE.fullmatch(str(evidence.get("outcome_evidence_sha256") or "")):
                raise LedgerIntegrityError("outcome evidence hash is invalid")
        else:
            raise LedgerIntegrityError("unsupported event type at record %d" % index)
        previous_hash = actual_hash
    return previous_hash


class ForwardValidationLedger:
    """Verified JSONL ledger.  Construction performs the mandatory startup audit."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.verify()

    def _flags(self) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        return flags

    def _open_for_append(self) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(str(self.path), self._flags(), 0o600)
        except OSError as exc:
            raise LedgerIntegrityError("cannot safely open ledger: %s" % self.path) from exc

    def verify(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {
                "ok": True,
                "status": "VERIFIED_EMPTY",
                "record_count": 0,
                "head_hash": GENESIS_HASH,
                "ledger": str(self.path),
            }
        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            records = _decode_records(_read_all(fd))
            head_hash = _validate_record_chain(records)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return {
            "ok": True,
            "status": "VERIFIED",
            "record_count": len(records),
            "head_hash": head_hash,
            "ledger": str(self.path),
        }

    def records(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            records = _decode_records(_read_all(fd))
            _validate_record_chain(records)
            return records
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def append_many(
        self,
        events: Sequence[Tuple[str, Mapping[str, Any]]],
        recorded_at: datetime,
    ) -> List[Dict[str, Any]]:
        if not events:
            return []
        observed = _now_cn(recorded_at)
        if observed > datetime.now(CN_TZ) + timedelta(seconds=MAX_LEDGER_FUTURE_SKEW_SECONDS):
            raise InputValidationError("ledger recorded_at cannot be in the future")
        fd = self._open_for_append()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            existing = _decode_records(_read_all(fd))
            head_hash = _validate_record_chain(existing)
            sequence = len(existing)
            additions = []
            for event_type, raw_payload in events:
                sequence += 1
                unsigned = {
                    "schema_version": LEDGER_SCHEMA_VERSION,
                    "sequence": sequence,
                    "event_type": event_type,
                    "recorded_at": observed.isoformat(timespec="seconds"),
                    "previous_hash": head_hash,
                    "payload": dict(raw_payload),
                }
                record = dict(unsigned)
                record["record_hash"] = _record_hash(unsigned)
                additions.append(record)
                head_hash = record["record_hash"]
            _validate_record_chain([*existing, *additions])
            encoded = "".join(_canonical_json(item) + "\n" for item in additions).encode("utf-8")
            try:
                existing_size = os.fstat(fd).st_size
            except OSError as exc:
                raise LedgerIntegrityError("cannot inspect ledger size") from exc
            if existing_size + len(encoded) > MAX_LEDGER_BYTES:
                raise LedgerIntegrityError(
                    "forward-validation ledger hard size limit reached; start a reviewed new epoch"
                )
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("zero-byte ledger write")
                view = view[written:]
            os.fsync(fd)
            return additions
        except OSError as exc:
            raise LedgerIntegrityError("ledger append/fsync failed") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def append(self, event_type: str, payload: Mapping[str, Any], recorded_at: datetime) -> Dict[str, Any]:
        return self.append_many([(event_type, payload)], recorded_at)[0]

    def summary(self) -> Dict[str, Any]:
        records = self.records()
        registrations = [r for r in records if r["event_type"] == EVENT_PREREGISTRATION]
        outcomes = [r for r in records if r["event_type"] == EVENT_OUTCOME]
        expected = sum(
            len(r["payload"]["research_states"]) * len(r["payload"]["horizons_sessions"])
            for r in registrations
        )
        return {
            "ok": True,
            "status": "VERIFIED",
            "ledger": str(self.path),
            "record_count": len(records),
            "head_hash": records[-1]["record_hash"] if records else GENESIS_HASH,
            "registration_count": len(registrations),
            "outcome_count": len(outcomes),
            "unresolved_outcome_count": expected - len(outcomes),
            "latest_registration_session": (
                registrations[-1]["payload"]["session_date"] if registrations else None
            ),
            "research_only": True,
            "scope": "configured_watchlist_research_states_only",
            "full_market_selector": False,
        }


def _validate_completed_snapshot(
    snapshot: Mapping[str, Any],
    policy: Mapping[str, Any],
    section_name: str,
    observed_at: datetime,
) -> Tuple[date, datetime]:
    section = _as_mapping(policy.get(section_name), "policy.%s" % section_name)
    session_date = _parse_date(snapshot.get("data_date"), "market_snapshot.data_date")
    generated_at = _parse_datetime(snapshot.get("generated_at"), "market_snapshot.generated_at")
    tolerance = timedelta(seconds=int(section.get("future_tolerance_seconds", 0)))
    if session_date > observed_at.date() or generated_at > observed_at + tolerance:
        raise InputValidationError("market snapshot contains a future date/timestamp")
    if section.get("same_cn_calendar_day_only") is True and session_date != observed_at.date():
        raise BackfillRejectedError(
            "session %s cannot be recorded on %s" % (session_date, observed_at.date())
        )
    if generated_at.date() != session_date:
        raise BackfillRejectedError("snapshot generation date does not equal its session date")
    cutoff = _parse_cutoff(
        section.get("completed_session_cutoff_cn"),
        "policy.%s.completed_session_cutoff_cn" % section_name,
    )
    if generated_at.timetz().replace(tzinfo=None) < cutoff or observed_at.timetz().replace(tzinfo=None) < cutoff:
        raise InputValidationError("daily snapshot is not a completed-session observation")
    validation = _as_mapping(snapshot.get("validation"), "market_snapshot.validation")
    if section.get("require_market_validation_passed") is True and validation.get("passed") is not True:
        raise InputValidationError("market snapshot validation did not pass")
    return session_date, generated_at


def _series_from_row(row: Mapping[str, Any], field: str) -> List[Tuple[date, float]]:
    raw = row.get("recent_closes")
    if not isinstance(raw, list) or not raw:
        raise InputValidationError("%s.recent_closes is empty" % field)
    values = {}
    for index, item in enumerate(raw):
        value = _as_mapping(item, "%s.recent_closes[%d]" % (field, index))
        session = _parse_date(value.get("date"), "%s.recent_closes.date" % field)
        close = _positive_number(value.get("close"), "%s.recent_closes.close" % field)
        if session in values:
            raise InputValidationError("%s has duplicate close date %s" % (field, session))
        values[session] = close
    result = sorted(values.items())
    declared_date = _parse_date(row.get("date"), "%s.date" % field)
    declared_close = _positive_number(row.get("close"), "%s.close" % field)
    if result[-1][0] != declared_date or not math.isclose(
        result[-1][1], declared_close, rel_tol=0.0, abs_tol=1e-8
    ):
        raise InputValidationError("%s latest close/date does not match recent_closes" % field)
    return result


def _benchmark_row(snapshot: Mapping[str, Any], policy: Mapping[str, Any]) -> Mapping[str, Any]:
    benchmark = _as_mapping(policy.get("benchmark"), "policy.benchmark")
    indices = _as_mapping(snapshot.get("indices"), "market_snapshot.indices")
    name = str(benchmark.get("snapshot_name"))
    row = _as_mapping(indices.get(name), "market_snapshot.indices.%s" % name)
    expected_symbol = _symbol_from_thscode(str(benchmark.get("thscode")))
    if str(row.get("symbol") or "").lower() != expected_symbol:
        raise InputValidationError("benchmark symbol does not match policy")
    return row


def _stock_rows(snapshot: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    stocks = _as_mapping(snapshot.get("stocks"), "market_snapshot.stocks")
    output = {}
    for name, raw in stocks.items():
        row = _as_mapping(raw, "market_snapshot.stocks.%s" % name)
        symbol = str(row.get("symbol") or "").lower()
        if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", symbol):
            raise InputValidationError("invalid stock symbol in market snapshot: %r" % symbol)
        thscode = symbol[-6:] + "." + symbol[:2].upper()
        if thscode in output:
            raise InputValidationError("duplicate stock in market snapshot: %s" % thscode)
        output[thscode] = row
    return output


def _strategy_items(strategy: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    rows = _as_list(strategy.get("items"), "strategy.items")
    if not rows:
        raise InputValidationError("strategy.items must not be empty")
    output = {}
    for index, raw in enumerate(rows):
        row = _as_mapping(raw, "strategy.items[%d]" % index)
        symbol = _validate_thscode(row.get("thscode"), "strategy.items[%d].thscode" % index)
        if symbol in output:
            raise InputValidationError("duplicate strategy symbol: %s" % symbol)
        output[symbol] = row
    return output


def build_registration_payload(
    market_snapshot: Mapping[str, Any],
    opportunity_radar: Mapping[str, Any],
    strategy: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Validate and freeze one full configured-watchlist research observation."""

    validate_policy(policy)
    observed_at = _now_cn(now)
    session_date, _ = _validate_completed_snapshot(
        market_snapshot, policy, "registration", observed_at
    )
    registration_policy = _as_mapping(policy.get("registration"), "policy.registration")
    required_adjustment = str(
        registration_policy.get("required_equity_adjustment_token") or ""
    ).lower()
    if required_adjustment not in str(market_snapshot.get("price_adjustment") or "").lower():
        raise InputValidationError("registration snapshot is not explicitly forward-adjusted")
    if registration_policy.get("require_radar_ok") is True and opportunity_radar.get("ok") is not True:
        raise InputValidationError("opportunity radar is not fully verified")
    radar_generated = _parse_datetime(
        opportunity_radar.get("generated_at"), "opportunity_radar.generated_at"
    )
    tolerance = timedelta(seconds=int(registration_policy.get("future_tolerance_seconds", 0)))
    if radar_generated > observed_at + tolerance or radar_generated.date() != session_date:
        raise InputValidationError("opportunity radar timestamp is future or not from this session")
    completed_cutoff = _parse_cutoff(
        registration_policy.get("completed_session_cutoff_cn"),
        "policy.registration.completed_session_cutoff_cn",
    )
    if radar_generated.timetz().replace(tzinfo=None) < completed_cutoff:
        raise InputValidationError("opportunity radar was generated before session completion")
    if opportunity_radar.get("watchlist_as_of") != strategy.get("as_of"):
        raise InputValidationError("radar watchlist_as_of does not match strategy")
    if opportunity_radar.get("review_by") != strategy.get("review_by"):
        raise InputValidationError("radar review_by does not match strategy")
    strategy_as_of = _parse_date(strategy.get("as_of"), "strategy.as_of")
    strategy_review_by = _parse_date(strategy.get("review_by"), "strategy.review_by")
    if strategy_as_of > session_date:
        raise InputValidationError("strategy as_of is in the future")
    if registration_policy.get("require_watchlist_current") is True and (
        strategy_review_by < session_date or opportunity_radar.get("watchlist_stale") is True
    ):
        raise InputValidationError("configured watchlist review window has expired")

    strategy_rows = _strategy_items(strategy)
    radar_raw = _as_list(opportunity_radar.get("items"), "opportunity_radar.items")
    radar_rows = {}
    for index, raw in enumerate(radar_raw):
        row = _as_mapping(raw, "opportunity_radar.items[%d]" % index)
        symbol = _validate_thscode(row.get("thscode"), "opportunity_radar.items.thscode")
        if symbol in radar_rows:
            raise InputValidationError("duplicate radar symbol: %s" % symbol)
        radar_rows[symbol] = row
    if set(radar_rows) != set(strategy_rows):
        raise InputValidationError("radar must cover the entire configured strategy exactly once")

    allowed_states = set(registration_policy.get("allowed_research_states") or [])
    stock_rows = _stock_rows(market_snapshot)
    benchmark_series = _series_from_row(
        _benchmark_row(market_snapshot, policy), "market_snapshot.benchmark"
    )
    benchmark_map = dict(benchmark_series)
    if benchmark_series[-1][0] != session_date or session_date not in benchmark_map:
        raise InputValidationError("benchmark does not end on the completed session")

    research_states = []
    for symbol in sorted(strategy_rows):
        radar_row = radar_rows[symbol]
        state = str(radar_row.get("state") or "")
        if state not in allowed_states:
            raise InputValidationError("radar state %r is not preregistrable" % state)
        if _parse_date(radar_row.get("completed_session"), "radar.completed_session") != session_date:
            raise InputValidationError("radar state is not bound to the completed session")
        if registration_policy.get("require_item_quote_timestamp") is True:
            quote_at = _parse_datetime(radar_row.get("quote_time"), "radar.quote_time")
            quote_cutoff = _parse_cutoff(
                registration_policy.get("quote_not_before_cn"),
                "policy.registration.quote_not_before_cn",
            )
            if (
                quote_at.date() != session_date
                or quote_at > observed_at + tolerance
                or quote_at.timetz().replace(tzinfo=None) < quote_cutoff
            ):
                raise InputValidationError("radar item quote is not a completed-session quote")
        stock_row = stock_rows.get(symbol)
        if stock_row is None:
            raise InputValidationError("market snapshot is missing strategy symbol %s" % symbol)
        technical = _as_mapping(stock_row.get("technical"), "stock.technical")
        stock_series = _series_from_row(technical, "market_snapshot.stock.%s" % symbol)
        stock_map = dict(stock_series)
        if stock_series[-1][0] != session_date or session_date not in stock_map:
            raise InputValidationError("stock %s does not end on the completed session" % symbol)
        score = None
        components = radar_row.get("score_components")
        if isinstance(components, Mapping) and components.get("total") is not None:
            score = _number(components.get("total"), "radar.score_components.total")
        blockers = radar_row.get("blocked_by") or []
        reasons = radar_row.get("reason_codes") or []
        if not isinstance(blockers, list) or not isinstance(reasons, list):
            raise InputValidationError("radar blockers/reason codes must be arrays")
        research_states.append(
            {
                "thscode": symbol,
                "name": str(radar_row.get("name") or strategy_rows[symbol].get("name") or ""),
                "theme": str(radar_row.get("theme") or strategy_rows[symbol].get("theme") or ""),
                        "research_state": state,
                "state_label": str(radar_row.get("state_label") or state),
                        "reference_close": round(stock_map[session_date], 8),
                        "upstream_quote_time": radar_row.get("quote_time"),
                "score": None if score is None else round(score, 8),
                "blocked_by": [str(value) for value in blockers],
                "reason_codes": [str(value) for value in reasons],
                "research_only": True,
                "trading_instruction": False,
            }
        )

    policy_hash = document_sha256(policy)
    strategy_hash = document_sha256(strategy)
    snapshot_hash = document_sha256(market_snapshot)
    radar_hash = document_sha256(opportunity_radar)
    input_bundle_hash = document_sha256(
        {
            "market_snapshot_sha256": snapshot_hash,
            "opportunity_radar_sha256": radar_hash,
            "strategy_sha256": strategy_hash,
        }
    )
    identity = {
        "ledger_epoch": policy.get("ledger_epoch"),
        "session_date": session_date.isoformat(),
        "policy_sha256": policy_hash,
        "strategy_sha256": strategy_hash,
        "input_bundle_sha256": input_bundle_hash,
    }
    registration_id = "fvr-" + document_sha256(identity)[:32]
    return {
        "registration_id": registration_id,
        "record_kind": "configured_watchlist_research_state_preregistration",
        "session_date": session_date.isoformat(),
        "research_only": True,
        "trading_instruction": False,
        "full_market_selector": False,
        "scope": policy.get("scope"),
        "scope_disclosure": policy.get("scope_disclosure"),
        "horizons_sessions": list(policy.get("horizons_sessions") or []),
        "benchmark": {
            "thscode": policy["benchmark"]["thscode"],
            "name": policy["benchmark"]["snapshot_name"],
            "reference_close": round(benchmark_map[session_date], 8),
        },
        "upstream": {
            "radar_id": opportunity_radar.get("radar_id"),
            "radar_engine_version": opportunity_radar.get("engine_version"),
            "market_data_date": market_snapshot.get("data_date"),
            "market_generated_at": market_snapshot.get("generated_at"),
        },
        "binding": {
            "ledger_epoch": policy.get("ledger_epoch"),
            "engine_version": ENGINE_VERSION,
            "engine_source_sha256": _source_sha256(),
            "policy_version": policy.get("policy_version"),
            "policy_sha256": policy_hash,
            "strategy_sha256": strategy_hash,
            "market_snapshot_sha256": snapshot_hash,
            "opportunity_radar_sha256": radar_hash,
            "input_bundle_sha256": input_bundle_hash,
        },
        "research_states": research_states,
    }


def register_daily(
    ledger: ForwardValidationLedger,
    market_snapshot: Mapping[str, Any],
    opportunity_radar: Mapping[str, Any],
    strategy: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Preregister today's complete watchlist state; duplicates/backfills fail."""

    # Mandatory full-chain audit before processing any would-be append.
    ledger.verify()
    observed_at = _now_cn(now)
    payload = build_registration_payload(
        market_snapshot, opportunity_radar, strategy, policy, now=observed_at
    )
    registrations = [
        record for record in ledger.records() if record["event_type"] == EVENT_PREREGISTRATION
    ]
    session = _parse_date(payload["session_date"], "registration.session_date")
    if registrations:
        latest = _parse_date(
            registrations[-1]["payload"]["session_date"], "latest_registration.session_date"
        )
        if session == latest:
            raise DuplicateRegistrationError("session %s is already registered" % session)
        if session < latest:
            raise BackfillRejectedError("cannot register before latest session %s" % latest)
        first_binding = registrations[0]["payload"]["binding"]
        for field in ("ledger_epoch", "engine_version", "policy_sha256", "strategy_sha256"):
            if payload["binding"].get(field) != first_binding.get(field):
                raise PolicyMismatchError("ledger epoch binding changed: %s" % field)
    record = ledger.append(EVENT_PREREGISTRATION, payload, observed_at)
    return {
        "ok": True,
        "status": "PREREGISTERED",
        "ledger": str(ledger.path),
        "sequence": record["sequence"],
        "record_hash": record["record_hash"],
        "registration_id": payload["registration_id"],
        "session_date": payload["session_date"],
        "research_state_count": len(payload["research_states"]),
        "horizons_sessions": payload["horizons_sessions"],
        "research_only": True,
        "full_market_selector": False,
    }


def _pending(
    registration_id: str,
    symbol: str,
    horizon: int,
    reason: str,
    target_session: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "registration_id": registration_id,
        "thscode": symbol,
        "horizon_sessions": horizon,
        "target_session": target_session,
        "status": "PENDING",
        "reason_code": reason,
    }


def settle_due_outcomes(
    ledger: ForwardValidationLedger,
    market_evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record exact-due-session outcomes; incomplete evidence remains pending."""

    validate_policy(policy)
    chain = ledger.verify()
    observed_at = _now_cn(now)
    evidence_date, _ = _validate_completed_snapshot(
        market_evidence, policy, "outcome", observed_at
    )
    required_adjustment = str(policy["outcome"]["required_equity_adjustment_token"]).lower()
    if required_adjustment not in str(market_evidence.get("price_adjustment") or "").lower():
        raise InputValidationError("outcome evidence is not explicitly forward-adjusted")

    records = ledger.records()
    registrations = [
        record["payload"] for record in records if record["event_type"] == EVENT_PREREGISTRATION
    ]
    if not registrations:
        return {
            "ok": True,
            "status": "NO_REGISTRATIONS",
            "ledger": str(ledger.path),
            "recorded_count": 0,
            "pending_count": 0,
            "pending": [],
            "research_only": True,
        }
    policy_hash = document_sha256(policy)
    for registration in registrations:
        binding = registration.get("binding") or {}
        if binding.get("policy_sha256") != policy_hash:
            raise PolicyMismatchError("current policy does not match ledger epoch")
        if binding.get("engine_version") != ENGINE_VERSION:
            raise PolicyMismatchError("registration engine version does not match")

    benchmark_series = _series_from_row(
        _benchmark_row(market_evidence, policy), "market_evidence.benchmark"
    )
    if benchmark_series[-1][0] != evidence_date:
        raise InputValidationError("benchmark evidence does not end on data_date")
    benchmark_dates = [item[0] for item in benchmark_series]
    benchmark_map = dict(benchmark_series)
    benchmark_index = {value: index for index, value in enumerate(benchmark_dates)}
    stock_rows = _stock_rows(market_evidence)
    stock_series_cache = {}  # type: Dict[str, List[Tuple[date, float]]]
    evidence_hash = document_sha256(market_evidence)
    existing_keys = {
        (
            record["payload"]["registration_id"],
            record["payload"]["thscode"],
            int(record["payload"]["horizon_sessions"]),
        )
        for record in records
        if record["event_type"] == EVENT_OUTCOME
    }

    pending = []
    already_recorded = []
    outcome_payloads = []
    tolerance = _number(
        policy["outcome"].get("benchmark_reference_close_tolerance"),
        "policy.outcome.benchmark_reference_close_tolerance",
    )
    for registration in registrations:
        registration_id = str(registration["registration_id"])
        registration_date = _parse_date(registration["session_date"], "registration.session_date")
        start_index = benchmark_index.get(registration_date)
        for state in registration["research_states"]:
            symbol = str(state["thscode"])
            for horizon in registration["horizons_sessions"]:
                key = (registration_id, symbol, int(horizon))
                if key in existing_keys:
                    already_recorded.append(
                        {
                            "registration_id": registration_id,
                            "thscode": symbol,
                            "horizon_sessions": int(horizon),
                        }
                    )
                    continue
                if start_index is None:
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "REGISTRATION_SESSION_OUTSIDE_EVIDENCE_WINDOW",
                        )
                    )
                    continue
                target_index = start_index + int(horizon)
                if target_index >= len(benchmark_dates):
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "HORIZON_NOT_MATURE",
                        )
                    )
                    continue
                target_date = benchmark_dates[target_index]
                if target_date < evidence_date:
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "OUTCOME_WINDOW_MISSED_NO_BACKFILL",
                            target_date.isoformat(),
                        )
                    )
                    continue
                if target_date > evidence_date:
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "HORIZON_NOT_MATURE",
                            target_date.isoformat(),
                        )
                    )
                    continue
                registered_benchmark = _positive_number(
                    registration["benchmark"]["reference_close"],
                    "registration.benchmark.reference_close",
                )
                if not math.isclose(
                    benchmark_map[registration_date],
                    registered_benchmark,
                    rel_tol=0.0,
                    abs_tol=tolerance,
                ):
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "BENCHMARK_BASE_CLOSE_MISMATCH",
                            target_date.isoformat(),
                        )
                    )
                    continue
                if symbol not in stock_series_cache:
                    row = stock_rows.get(symbol)
                    if row is None:
                        stock_series_cache[symbol] = []
                    else:
                        technical = _as_mapping(row.get("technical"), "stock.technical")
                        stock_series_cache[symbol] = _series_from_row(
                            technical, "market_evidence.stock.%s" % symbol
                        )
                stock_series = stock_series_cache[symbol]
                if stock_series and stock_series[-1][0] > evidence_date:
                    raise InputValidationError(
                        "stock evidence %s contains a future close" % symbol
                    )
                stock_map = dict(stock_series)
                required_dates = benchmark_dates[start_index : target_index + 1]
                missing_dates = [value for value in required_dates if value not in stock_map]
                if missing_dates:
                    pending.append(
                        _pending(
                            registration_id,
                            symbol,
                            int(horizon),
                            "INCOMPLETE_STOCK_SESSION_EVIDENCE",
                            target_date.isoformat(),
                        )
                    )
                    continue

                stock_start = stock_map[registration_date]
                stock_end = stock_map[target_date]
                benchmark_start = benchmark_map[registration_date]
                benchmark_end = benchmark_map[target_date]
                stock_return = stock_end / stock_start - 1.0
                benchmark_return = benchmark_end / benchmark_start - 1.0
                relative_return = (1.0 + stock_return) / (1.0 + benchmark_return) - 1.0
                identity = {
                    "registration_id": registration_id,
                    "thscode": symbol,
                    "horizon_sessions": int(horizon),
                }
                outcome_payloads.append(
                    {
                        "outcome_id": "fvo-" + document_sha256(identity)[:32],
                        "record_kind": "observed_forward_research_outcome",
                        "registration_id": registration_id,
                        "thscode": symbol,
                        "research_state": state["research_state"],
                        "horizon_sessions": int(horizon),
                        "start_session": registration_date.isoformat(),
                        "target_session": target_date.isoformat(),
                        "status": "OBSERVED",
                        "research_only": True,
                        "trading_instruction": False,
                        "stock_start_close_from_same_evidence": round(stock_start, 8),
                        "stock_target_close": round(stock_end, 8),
                        "benchmark_start_close": round(benchmark_start, 8),
                        "benchmark_target_close": round(benchmark_end, 8),
                        "stock_return_pct": round(stock_return * 100.0, 8),
                        "benchmark_return_pct": round(benchmark_return * 100.0, 8),
                        "excess_return_pct_points": round(
                            (stock_return - benchmark_return) * 100.0, 8
                        ),
                        "relative_return_pct": round(relative_return * 100.0, 8),
                        "evidence": {
                            "market_snapshot_sha256": evidence_hash,
                            "data_date": evidence_date.isoformat(),
                            "price_adjustment": market_evidence.get("price_adjustment"),
                            "required_session_count": len(required_dates),
                            "session_dates_sha256": document_sha256(
                                [value.isoformat() for value in required_dates]
                            ),
                            "all_stock_sessions_present": True,
                            "benchmark_calendar_authority": policy["benchmark"]["thscode"],
                            "outcome_evidence_sha256": evidence_hash,
                        },
                        "binding": {
                            "registration_engine_version": registration["binding"]["engine_version"],
                            "registration_policy_sha256": registration["binding"]["policy_sha256"],
                            "registration_strategy_sha256": registration["binding"]["strategy_sha256"],
                            "registration_input_bundle_sha256": registration["binding"]["input_bundle_sha256"],
                            "outcome_engine_version": ENGINE_VERSION,
                            "outcome_engine_source_sha256": _source_sha256(),
                            "outcome_evidence_sha256": evidence_hash,
                        },
                    }
                )

    additions = ledger.append_many(
        [(EVENT_OUTCOME, payload) for payload in outcome_payloads], observed_at
    )
    if additions:
        status = "OUTCOMES_RECORDED"
    elif pending:
        status = "EVIDENCE_PENDING"
    elif already_recorded:
        status = "ALREADY_RECORDED"
    else:
        status = "NO_DUE_OUTCOMES"
    return {
        "ok": True,
        "status": status,
        "ledger": str(ledger.path),
        "verified_head_before": chain["head_hash"],
        "evidence_date": evidence_date.isoformat(),
        "evidence_sha256": evidence_hash,
        "recorded_count": len(additions),
        "recorded_outcomes": [
            {
                "sequence": record["sequence"],
                "record_hash": record["record_hash"],
                "outcome_id": record["payload"]["outcome_id"],
                "registration_id": record["payload"]["registration_id"],
                "thscode": record["payload"]["thscode"],
                "horizon_sessions": record["payload"]["horizon_sessions"],
            }
            for record in additions
        ],
        "already_recorded_count": len(already_recorded),
        "pending_count": len(pending),
        "pending": pending,
        "research_only": True,
        "full_market_selector": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append-only forward paper validation for configured-watchlist research states"
    )
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify", help="verify the complete JSONL hash chain")
    subparsers.add_parser("summary", help="verify and summarize ledger coverage")
    register = subparsers.add_parser("register", help="preregister today's completed-session research states")
    register.add_argument("--market-snapshot", type=Path, required=True)
    register.add_argument("--opportunity-radar", type=Path, required=True)
    register.add_argument("--strategy", type=Path, required=True)
    settle = subparsers.add_parser("settle", help="record outcomes due on today's completed session")
    settle.add_argument("--market-evidence", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ledger = ForwardValidationLedger(args.ledger)
        if args.command == "verify":
            result = ledger.verify()
        elif args.command == "summary":
            result = ledger.summary()
        elif args.command == "register":
            policy = load_policy(args.policy)
            result = register_daily(
                ledger,
                load_json_document(args.market_snapshot),
                load_json_document(args.opportunity_radar),
                load_json_document(args.strategy),
                policy,
            )
        elif args.command == "settle":
            policy = load_policy(args.policy)
            result = settle_due_outcomes(
                ledger,
                load_json_document(args.market_evidence),
                policy,
            )
        else:  # pragma: no cover - argparse guarantees this is unreachable.
            raise InputValidationError("unsupported command")
        exit_code = 0
    except ForwardValidationError as exc:
        result = {
            "ok": False,
            "status": exc.code,
            "error": str(exc),
            "ledger": str(Path(args.ledger).resolve()),
        }
        exit_code = 2
    except Exception as exc:  # Fail closed without a traceback or secret-bearing repr.
        result = {
            "ok": False,
            "status": "UNEXPECTED_ERROR",
            "error": "%s: %s" % (type(exc).__name__, str(exc)),
            "ledger": str(Path(args.ledger).resolve()),
        }
        exit_code = 2
    print(_canonical_json(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
