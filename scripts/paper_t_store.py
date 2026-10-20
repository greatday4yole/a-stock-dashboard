#!/usr/bin/env python3
"""模拟盘成交台账存储（Paper-T execution ledger）。

仅记录"模拟执行"的组合反T成交事件：按交易日一个追加式 JSONL 文件
``data/paper-trading/paper-t-YYYY-MM-DD.jsonl``，与盘中盈亏账本同构
（canonical JSON、sequence、previous_record_hash 哈希链、record_hash 校验），
但独立目录、独立 schema，不与真实账户或盘中账本混写。

事件类型（event 字段）：
- ``session_open``  首次模拟执行时落一条会话元数据（模式/阈值/组合市值）
- ``fill``          一笔模拟成交（leg=sell/buy，一股一手都来自行情最新价）
- ``session_close`` 买回腿完成后落一条汇总（卖出/买回金额、费用、已实现盈亏）
- ``void``          撤销当日模拟会话（需给原因；统计口径中剔除）

估算数据，非投资建议。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

SCHEMA_VERSION = "paper-t-ledger.v1"
DEFAULT_DATA_DIR = Path("data/paper-trading")
GENESIS_HASH = "0" * 64
_HASH_RE = re.compile(r"[0-9a-f]{64}")
MAX_LEDGER_BYTES = 4 * 1024 * 1024

_KNOWN_EVENTS = ("session_open", "fill", "void", "session_close")
_KNOWN_LEGS = ("sell", "buy")
_FILL_REQUIRED = ("symbol", "shares", "price")


class PaperTIntegrityError(Exception):
    """台账完整性校验失败（链断裂/字段非法/被篡改）。"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_document(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _record_hash(record: Mapping[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_hash", None)
    return _sha256_document(unsigned)


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _finite_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _decode_ledger(raw: bytes, expected_date: date) -> List[Dict[str, Any]]:
    if not raw:
        return []
    truncated_tail = not raw.endswith(b"\n")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaperTIntegrityError("paper-t ledger is not UTF-8") from exc
    lines = text.splitlines()
    if truncated_tail and lines:
        # A crash between "write" and "flush" can leave the final line
        # half-written.  That line was never durably committed, so it is
        # discarded on read and the ledger is truncated back to the last
        # complete record.  Freezing the whole day over a torn tail would
        # otherwise lose every remaining session of that trading day.
        lines = lines[:-1]
    records: List[Dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    previous_at: Optional[datetime] = None
    for line_number, line in enumerate(lines, 1):
        if not line:
            raise PaperTIntegrityError(f"ledger contains an empty line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperTIntegrityError(f"invalid JSON at ledger line {line_number}") from exc
        if not isinstance(value, dict):
            raise PaperTIntegrityError(f"ledger line {line_number} must be an object")
        if _canonical_json(value) != line:
            raise PaperTIntegrityError(f"ledger line {line_number} is not canonical JSON")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise PaperTIntegrityError(f"unsupported schema at ledger line {line_number}")
        if value.get("trade_date") != expected_date.isoformat():
            raise PaperTIntegrityError(f"trade-date mismatch at ledger line {line_number}")
        if value.get("sequence") != line_number:
            raise PaperTIntegrityError(f"sequence mismatch at ledger line {line_number}")
        if value.get("previous_record_hash") != previous_hash:
            raise PaperTIntegrityError(f"hash-chain mismatch at ledger line {line_number}")
        actual_hash = str(value.get("record_hash") or "")
        if not _HASH_RE.fullmatch(actual_hash) or actual_hash != _record_hash(value):
            raise PaperTIntegrityError(f"record hash mismatch at ledger line {line_number}")
        event = str(value.get("event") or "")
        if event not in _KNOWN_EVENTS:
            raise PaperTIntegrityError(f"unknown event at ledger line {line_number}")
        stamp = _parse_ts(value.get("ts"))
        if stamp is None:
            raise PaperTIntegrityError(f"invalid ts at ledger line {line_number}")
        if previous_at is not None and stamp < previous_at:
            raise PaperTIntegrityError(f"ledger ts moves backwards at line {line_number}")
        previous_at = stamp
        if event == "fill":
            for field in _FILL_REQUIRED:
                if value.get(field) in (None, ""):
                    raise PaperTIntegrityError(f"fill missing {field} at line {line_number}")
            if str(value.get("leg") or "") not in _KNOWN_LEGS:
                raise PaperTIntegrityError(f"invalid leg at ledger line {line_number}")
            try:
                shares = int(value.get("shares"))
            except (TypeError, ValueError):
                raise PaperTIntegrityError(f"invalid shares at ledger line {line_number}")
            price = _finite_float(value.get("price"))
            if shares <= 0 or not price or price <= 0:
                raise PaperTIntegrityError(f"invalid fill amounts at ledger line {line_number}")
            notional = _finite_float(value.get("notional"))
            if notional is None or abs(notional - round(shares * price, 2)) > 0.011:
                raise PaperTIntegrityError(f"notional mismatch at ledger line {line_number}")
        previous_hash = actual_hash
        records.append(value)
    return records


class PaperTStore:
    """Concurrency-safe append-only daily JSONL store for simulated T fills.

    The per-path lock is shared across every store instance in this process,
    so two stores pointing at the same ledger cannot interleave an append.
    """

    _locks_guard = threading.Lock()
    _shared_locks: Dict[str, threading.RLock] = {}

    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    @classmethod
    def _lock_for(cls, path: Path) -> threading.RLock:
        key = str(path.absolute())
        with cls._locks_guard:
            lock = cls._shared_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._shared_locks[key] = lock
            return lock

    def path_for(self, trade_date: date) -> Path:
        return self.data_dir / f"paper-t-{trade_date.isoformat()}.jsonl"

    def _truncate_torn_tail(self, path: Path) -> None:
        """Drop a half-written final line so the next append stays parseable.

        The torn line was never durably committed; keeping it would make the
        next append concatenate onto it and destroy one more good record.
        """

        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return
        if not raw or raw.endswith(b"\n"):
            return
        keep = raw.rfind(b"\n")
        with open(path, "r+b") as handle:
            handle.truncate(keep + 1 if keep >= 0 else 0)
            handle.flush()
            os.fsync(handle.fileno())

    def read_day(self, trade_date: date) -> List[Dict[str, Any]]:
        path = self.path_for(trade_date)
        with self._lock_for(path):
            if not path.exists():
                return []
            raw = path.read_bytes()
        if len(raw) > MAX_LEDGER_BYTES:
            raise PaperTIntegrityError("paper-t ledger is unexpectedly large")
        return _decode_ledger(raw, trade_date)

    def append(self, trade_date: date, event: Mapping[str, Any], ts: datetime) -> Dict[str, Any]:
        """Append one event atomically under the per-day lock; returns the record."""

        if str(event.get("event") or "") not in _KNOWN_EVENTS:
            raise ValueError(f"unknown paper-t event: {event.get('event')!r}")
        stamp = ts if ts.tzinfo else ts.astimezone()
        path = self.path_for(trade_date)
        with self._lock_for(path):
            self._truncate_torn_tail(path)
            existing = self.read_day(trade_date)
            if existing:
                last_ts = _parse_ts(existing[-1].get("ts"))
                if last_ts is not None and stamp < last_ts:
                    raise ValueError("paper-t ledger ts must be non-decreasing")
            sequence = len(existing) + 1
            record: Dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "trade_date": trade_date.isoformat(),
                "sequence": sequence,
                "event": str(event["event"]),
                "ts": stamp.isoformat(timespec="seconds"),
                **{key: value for key, value in event.items() if key != "event"},
                "previous_record_hash": (
                    str(existing[-1]["record_hash"]) if existing else GENESIS_HASH
                ),
            }
            record["record_hash"] = _record_hash(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(_canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def list_dates(self) -> List[str]:
        if not self.data_dir.exists():
            return []
        return sorted(
            path.stem.replace("paper-t-", "")
            for path in self.data_dir.glob("paper-t-*.jsonl")
        )
