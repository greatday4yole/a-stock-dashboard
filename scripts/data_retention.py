#!/usr/bin/env python3
"""Bound dashboard-generated data with a strict, auditable allowlist.

The default CLI mode is a dry run.  No rule accepts a filesystem path or glob
from policy: targets and filename grammars are fixed in this module.  This is
intentional so a malformed policy cannot turn retention into a general-purpose
recursive deletion tool.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.position_advice_store import compact_position_advice_store
except ModuleNotFoundError:  # Direct execution: python scripts/data_retention.py
    from position_advice_store import compact_position_advice_store  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "data-retention-policy.json"
DEFAULT_STATE_PATH = ROOT / "data" / "data-retention-state.json"
CN_TZ = timezone(timedelta(hours=8))

POLICY_SCHEMA_VERSION = 1
REPORT_SCHEMA = "dashboard-data-retention-report.v1"
INTRADAY_RE = re.compile(r"^intraday-pnl-(\d{4}-\d{2}-\d{2})\.jsonl$")
ANALYSIS_DIR_RE = re.compile(r"^portfolio-analysis-(\d{4}-\d{2}-\d{2})$")
PREMARKET_RE = re.compile(r"^premarket_index_snapshot_(\d{8})\.json$")
TRASH_RE = re.compile(
    r"^\.retention-trash-[0-9a-f]{32}-(portfolio-analysis-\d{4}-\d{2}-\d{2})$"
)
TEMP_RE = re.compile(
    r"^\.(?:"
    r"dashboard-news-cache\.json|dashboard-market-news-cache\.json|"
    r"market-snapshot\.json|macro-snapshot\.json|risk-snapshot\.json|"
    r"daily-pnl-history\.json|"
    r"position-advice-state\.json|position-advice-audit\.jsonl|"
    r"data-retention-state\.json"
    r")\.[A-Za-z0-9._-]{1,160}\.(?:tmp|retention\.tmp)$"
)

_TOP_LEVEL_RULES = {
    "cleanup_interval_seconds",
    "intraday_pnl",
    "portfolio_analysis",
    "premarket_snapshots",
    "orphan_temporary_files",
    "position_advice_audit",
}


class RetentionPolicyError(ValueError):
    """The numeric-only retention policy is invalid."""


class RetentionSafetyError(RuntimeError):
    """A target failed the allowlist, type, or no-symlink safety check."""


class RetentionBusyError(RuntimeError):
    """Another cleanup process already owns the project retention lock."""


def _integer(
    value: Any, field: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetentionPolicyError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise RetentionPolicyError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def load_retention_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load a strict numeric policy; paths and patterns are never configurable."""

    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise RetentionPolicyError("retention policy root must be an object")
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise RetentionPolicyError("unsupported retention policy schema_version")
    policy = raw.get("policy")
    if not isinstance(policy, Mapping) or set(policy) != _TOP_LEVEL_RULES:
        raise RetentionPolicyError("retention policy fields mismatch")

    def exact_section(name: str, fields: set[str]) -> Mapping[str, Any]:
        section = policy.get(name)
        if not isinstance(section, Mapping) or set(section) != fields:
            raise RetentionPolicyError(f"policy.{name} fields mismatch")
        return section

    intraday = exact_section("intraday_pnl", {"retention_days"})
    analyses = exact_section(
        "portfolio_analysis", {"retention_days", "keep_latest"}
    )
    premarket = exact_section(
        "premarket_snapshots", {"retention_days", "keep_latest"}
    )
    temporary = exact_section("orphan_temporary_files", {"retention_hours"})
    advice = exact_section(
        "position_advice_audit", {"max_bytes", "keep_recent_transitions"}
    )
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "cleanup_interval_seconds": _integer(
            policy.get("cleanup_interval_seconds"),
            "policy.cleanup_interval_seconds",
            minimum=300,
            maximum=86_400,
        ),
        "intraday_pnl": {
            "retention_days": _integer(
                intraday.get("retention_days"),
                "policy.intraday_pnl.retention_days",
                minimum=7,
                maximum=366,
            )
        },
        "portfolio_analysis": {
            "retention_days": _integer(
                analyses.get("retention_days"),
                "policy.portfolio_analysis.retention_days",
                minimum=90,
                maximum=3650,
            ),
            "keep_latest": _integer(
                analyses.get("keep_latest"),
                "policy.portfolio_analysis.keep_latest",
                minimum=1,
                maximum=20,
            ),
        },
        "premarket_snapshots": {
            "retention_days": _integer(
                premarket.get("retention_days"),
                "policy.premarket_snapshots.retention_days",
                minimum=7,
                maximum=366,
            ),
            "keep_latest": _integer(
                premarket.get("keep_latest"),
                "policy.premarket_snapshots.keep_latest",
                minimum=1,
                maximum=30,
            ),
        },
        "orphan_temporary_files": {
            "retention_hours": _integer(
                temporary.get("retention_hours"),
                "policy.orphan_temporary_files.retention_hours",
                minimum=1,
                maximum=168,
            )
        },
        "position_advice_audit": {
            "max_bytes": _integer(
                advice.get("max_bytes"),
                "policy.position_advice_audit.max_bytes",
                minimum=1024 * 1024,
                maximum=1024 * 1024 * 1024,
            ),
            "keep_recent_transitions": _integer(
                advice.get("keep_recent_transitions"),
                "policy.position_advice_audit.keep_recent_transitions",
                minimum=0,
                maximum=1000,
            ),
        },
    }


def _new_rule() -> dict[str, Any]:
    return {
        "scanned": 0,
        "candidates": 0,
        "candidate_bytes": 0,
        "removed_files": 0,
        "removed_directories": 0,
        "bytes_reclaimed": 0,
        "skipped_unsafe": 0,
        "warnings": [],
        "errors": [],
    }


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:500]}"


def _parse_date(raw: str, pattern: str) -> date | None:
    try:
        return datetime.strptime(raw, pattern).date()
    except ValueError:
        return None


def _ensure_data_root(project_root: Path, *, create: bool) -> Path:
    root = Path(project_root)
    if not root.is_absolute():
        raise RetentionSafetyError("project root must be absolute")
    root = root.resolve(strict=True)
    data_root = root / "data"
    if not data_root.exists():
        if not create:
            return data_root
        data_root.mkdir(mode=0o700)
    if data_root.is_symlink() or not data_root.is_dir():
        raise RetentionSafetyError("project data root must be a real directory")
    if data_root.resolve(strict=True).parent != root:
        raise RetentionSafetyError("project data root escaped the project")
    return data_root


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=parent_fd)


def _entry_stat(parent_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _same_entry(current: os.stat_result, expected: os.stat_result) -> bool:
    return (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) == (
        expected.st_dev,
        expected.st_ino,
        stat.S_IFMT(expected.st_mode),
    )


def _unlink_expected(parent: Path, name: str, expected: os.stat_result) -> int:
    descriptor = _open_directory(parent)
    try:
        current = _entry_stat(descriptor, name)
        if not _same_entry(current, expected) or not stat.S_ISREG(current.st_mode):
            raise RetentionSafetyError(f"allowlisted file changed before deletion: {name}")
        os.unlink(name, dir_fd=descriptor)
        return int(current.st_size)
    finally:
        os.close(descriptor)


def _inspect_tree_fd(descriptor: int) -> tuple[int, int]:
    total = 0
    files = 0
    for name in os.listdir(descriptor):
        if name in {".", ".."}:
            raise RetentionSafetyError("invalid directory entry")
        item = _entry_stat(descriptor, name)
        if stat.S_ISREG(item.st_mode):
            total += int(item.st_size)
            files += 1
        elif stat.S_ISDIR(item.st_mode):
            child = _open_child_directory(descriptor, name)
            try:
                child_total, child_files = _inspect_tree_fd(child)
            finally:
                os.close(child)
            total += child_total
            files += child_files
        else:
            raise RetentionSafetyError(
                f"directory contains a symbolic link or special file: {name}"
            )
    return total, files


def _inspect_tree(path: Path) -> tuple[int, int]:
    descriptor = _open_directory(path)
    try:
        return _inspect_tree_fd(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree_contents(descriptor: int) -> tuple[int, int, int]:
    total = 0
    files = 0
    directories = 0
    for name in os.listdir(descriptor):
        item = _entry_stat(descriptor, name)
        if stat.S_ISDIR(item.st_mode):
            child = _open_child_directory(descriptor, name)
            try:
                child_total, child_files, child_directories = _remove_tree_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
            total += child_total
            files += child_files
            directories += child_directories + 1
        elif stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode):
            # A symlink introduced after the preflight inspection is unlinked as
            # an object.  It is never followed and therefore cannot affect its target.
            os.unlink(name, dir_fd=descriptor)
            total += int(item.st_size) if stat.S_ISREG(item.st_mode) else 0
            files += 1
        else:
            raise RetentionSafetyError(f"refusing to remove special file: {name}")
    return total, files, directories


def _atomic_remove_directory(
    data_root: Path, name: str, expected: os.stat_result
) -> tuple[int, int, int]:
    root_fd = _open_directory(data_root)
    tombstone = f".retention-trash-{uuid.uuid4().hex}-{name}"
    try:
        current = _entry_stat(root_fd, name)
        if not _same_entry(current, expected) or not stat.S_ISDIR(current.st_mode):
            raise RetentionSafetyError(f"allowlisted directory changed before deletion: {name}")
        os.rename(name, tombstone, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        trash_fd = _open_child_directory(root_fd, tombstone)
        try:
            total, files, child_directories = _remove_tree_contents(trash_fd)
        finally:
            os.close(trash_fd)
        os.rmdir(tombstone, dir_fd=root_fd)
        return total, files, child_directories + 1
    finally:
        os.close(root_fd)


def _prune_dated_files(
    *,
    parent: Path,
    matcher: re.Pattern[str],
    date_pattern: str,
    retention_days: int,
    keep_latest: int,
    today: date,
    dry_run: bool,
) -> dict[str, Any]:
    result = _new_rule()
    if not parent.exists():
        return result
    if parent.is_symlink() or not parent.is_dir():
        result["errors"].append("allowlisted parent is not a real directory")
        return result
    entries: list[tuple[date, str, os.stat_result]] = []
    with os.scandir(parent) as iterator:
        for entry in iterator:
            match = matcher.fullmatch(entry.name)
            if not match:
                continue
            result["scanned"] += 1
            if entry.is_symlink():
                result["skipped_unsafe"] += 1
                result["warnings"].append(f"skipped symbolic link: {entry.name}")
                continue
            item = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                result["skipped_unsafe"] += 1
                result["warnings"].append(f"skipped non-regular file: {entry.name}")
                continue
            parsed = _parse_date(match.group(1), date_pattern)
            if parsed is None or parsed > today:
                result["warnings"].append(f"skipped invalid/future dated file: {entry.name}")
                continue
            entries.append((parsed, entry.name, item))
    protected = {
        name
        for _, name, _ in sorted(entries, reverse=True)[: max(0, keep_latest)]
    }
    cutoff = today - timedelta(days=retention_days)
    for artifact_date, name, item in entries:
        if name in protected or artifact_date >= cutoff:
            continue
        result["candidates"] += 1
        result["candidate_bytes"] += int(item.st_size)
        if dry_run:
            continue
        try:
            reclaimed = _unlink_expected(parent, name, item)
        except Exception as exc:
            result["errors"].append(_safe_error(exc))
        else:
            result["removed_files"] += 1
            result["bytes_reclaimed"] += reclaimed
    return result


def _prune_analysis_directories(
    data_root: Path,
    *,
    retention_days: int,
    keep_latest: int,
    today: date,
    dry_run: bool,
) -> dict[str, Any]:
    result = _new_rule()
    entries: list[tuple[date, str, os.stat_result]] = []
    with os.scandir(data_root) as iterator:
        for entry in iterator:
            match = ANALYSIS_DIR_RE.fullmatch(entry.name)
            if not match:
                continue
            result["scanned"] += 1
            if entry.is_symlink():
                result["skipped_unsafe"] += 1
                result["warnings"].append(f"skipped symbolic link: {entry.name}")
                continue
            item = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(item.st_mode):
                result["skipped_unsafe"] += 1
                result["warnings"].append(f"skipped non-directory: {entry.name}")
                continue
            artifact_date = _parse_date(match.group(1), "%Y-%m-%d")
            if artifact_date is None or artifact_date > today:
                result["warnings"].append(f"skipped invalid/future directory: {entry.name}")
                continue
            entries.append((artifact_date, entry.name, item))
    protected = {
        name for _, name, _ in sorted(entries, reverse=True)[:keep_latest]
    }
    cutoff = today - timedelta(days=retention_days)
    for artifact_date, name, item in entries:
        if name in protected or artifact_date >= cutoff:
            continue
        try:
            size, _ = _inspect_tree(data_root / name)
        except Exception as exc:
            result["skipped_unsafe"] += 1
            result["warnings"].append(_safe_error(exc))
            continue
        result["candidates"] += 1
        result["candidate_bytes"] += size
        if dry_run:
            continue
        try:
            reclaimed, removed_files, removed_directories = _atomic_remove_directory(
                data_root, name, item
            )
        except Exception as exc:
            result["errors"].append(_safe_error(exc))
        else:
            result["removed_files"] += removed_files
            result["removed_directories"] += removed_directories
            result["bytes_reclaimed"] += reclaimed
    return result


def _clean_orphan_temporary_files(
    data_root: Path,
    *,
    retention_hours: int,
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    result = _new_rule()
    cutoff_timestamp = (now - timedelta(hours=retention_hours)).timestamp()
    with os.scandir(data_root) as iterator:
        for entry in iterator:
            if not TEMP_RE.fullmatch(entry.name):
                continue
            result["scanned"] += 1
            if entry.is_symlink():
                result["skipped_unsafe"] += 1
                result["warnings"].append(f"skipped symbolic link: {entry.name}")
                continue
            item = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode) or item.st_mtime >= cutoff_timestamp:
                continue
            result["candidates"] += 1
            result["candidate_bytes"] += int(item.st_size)
            if dry_run:
                continue
            try:
                reclaimed = _unlink_expected(data_root, entry.name, item)
            except Exception as exc:
                result["errors"].append(_safe_error(exc))
            else:
                result["removed_files"] += 1
                result["bytes_reclaimed"] += reclaimed
    return result


def _clean_retention_tombstones(data_root: Path, *, dry_run: bool) -> dict[str, Any]:
    """Report interrupted deletions without automatically removing their contents.

    A filename alone cannot prove that a tombstone was created by this process.
    Deleting every directory that matches the tombstone grammar would therefore
    let a coincidentally named user directory be removed on the next cleanup.
    Preserve these directories for explicit manual review instead.
    """

    result = _new_rule()
    with os.scandir(data_root) as iterator:
        for entry in iterator:
            if not TRASH_RE.fullmatch(entry.name):
                continue
            result["scanned"] += 1
            result["skipped_unsafe"] += 1
            result["warnings"].append(
                f"preserved unverified retention tombstone for manual review: {entry.name}"
            )
    return result


def _compact_advice(
    data_root: Path,
    *,
    max_bytes: int,
    keep_recent_transitions: int,
    dry_run: bool,
    now: datetime,
) -> dict[str, Any]:
    result = _new_rule()
    audit_path = data_root / "position-advice-audit.jsonl"
    state_path = data_root / "position-advice-state.json"
    if not audit_path.exists():
        result["details"] = {"compacted": False, "reason": "ledger_absent"}
        return result
    result["scanned"] = 1
    if audit_path.is_symlink() or state_path.is_symlink():
        result["skipped_unsafe"] = 1
        result["errors"].append("advice state/audit must not be symbolic links")
        return result
    try:
        compacted = compact_position_advice_store(
            state_path,
            audit_path,
            max_bytes=max_bytes,
            keep_recent_transitions=keep_recent_transitions,
            dry_run=dry_run,
            now=now,
        )
    except Exception as exc:
        result["errors"].append(_safe_error(exc))
        return result
    if compacted.get("compacted"):
        result["candidates"] = 1
        result["candidate_bytes"] = int(compacted.get("bytes_reclaimed") or 0)
        if not dry_run:
            result["removed_files"] = 1  # one ledger atomically replaced
            result["bytes_reclaimed"] = int(compacted.get("bytes_reclaimed") or 0)
    result["details"] = {
        key: compacted.get(key)
        for key in (
            "compacted",
            "before_bytes",
            "after_bytes",
            "bytes_reclaimed",
            "before_records",
            "after_records",
            "max_bytes",
            "keep_recent_transitions",
            "checkpoint",
        )
    }
    return result


def _atomic_write_report(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise RetentionSafetyError("retention state must not be a symbolic link")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = _open_directory(path.parent)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def _acquire_lock(data_root: Path) -> int:
    # Lock the already-opened, allowlisted data directory itself.  This keeps a
    # dry run genuinely non-mutating while still serializing all project-local
    # retention workers on the same directory inode.
    descriptor = _open_directory(data_root)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise RetentionBusyError("another data-retention run is active") from exc
    return descriptor


def run_data_retention(
    *,
    project_root: Path = ROOT,
    policy: Mapping[str, Any] | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Execute every allowlisted rule and return per-rule and aggregate stats."""

    observed = now or datetime.now(CN_TZ)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("now must include a UTC offset")
    observed = observed.astimezone(CN_TZ)
    normalized_policy = dict(policy) if policy is not None else load_retention_policy()
    data_root = _ensure_data_root(Path(project_root), create=not dry_run)
    started_at = observed.isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "ok": True,
        "dry_run": bool(dry_run),
        "started_at": started_at,
        "completed_at": "",
        "policy_schema_version": normalized_policy.get("schema_version"),
        "rules": {},
        "totals": {},
    }
    if not data_root.exists():
        report["completed_at"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
        report["totals"] = {
            "scanned": 0,
            "candidates": 0,
            "candidate_bytes": 0,
            "removed_files": 0,
            "removed_directories": 0,
            "bytes_reclaimed": 0,
            "skipped_unsafe": 0,
            "errors": 0,
        }
        return report

    lock_fd = _acquire_lock(data_root)
    try:
        report["rules"]["retention_tombstones"] = _clean_retention_tombstones(
            data_root, dry_run=dry_run
        )
        report["rules"]["intraday_pnl"] = _prune_dated_files(
            parent=data_root / "intraday-pnl",
            matcher=INTRADAY_RE,
            date_pattern="%Y-%m-%d",
            retention_days=int(normalized_policy["intraday_pnl"]["retention_days"]),
            keep_latest=1,
            today=observed.date(),
            dry_run=dry_run,
        )
        report["rules"]["portfolio_analysis"] = _prune_analysis_directories(
            data_root,
            retention_days=int(
                normalized_policy["portfolio_analysis"]["retention_days"]
            ),
            keep_latest=int(normalized_policy["portfolio_analysis"]["keep_latest"]),
            today=observed.date(),
            dry_run=dry_run,
        )
        report["rules"]["premarket_snapshots"] = _prune_dated_files(
            parent=data_root,
            matcher=PREMARKET_RE,
            date_pattern="%Y%m%d",
            retention_days=int(
                normalized_policy["premarket_snapshots"]["retention_days"]
            ),
            keep_latest=int(normalized_policy["premarket_snapshots"]["keep_latest"]),
            today=observed.date(),
            dry_run=dry_run,
        )
        report["rules"]["orphan_temporary_files"] = _clean_orphan_temporary_files(
            data_root,
            retention_hours=int(
                normalized_policy["orphan_temporary_files"]["retention_hours"]
            ),
            now=observed,
            dry_run=dry_run,
        )
        report["rules"]["position_advice_audit"] = _compact_advice(
            data_root,
            max_bytes=int(normalized_policy["position_advice_audit"]["max_bytes"]),
            keep_recent_transitions=int(
                normalized_policy["position_advice_audit"]["keep_recent_transitions"]
            ),
            dry_run=dry_run,
            now=observed,
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    numeric_fields = (
        "scanned",
        "candidates",
        "candidate_bytes",
        "removed_files",
        "removed_directories",
        "bytes_reclaimed",
        "skipped_unsafe",
    )
    report["totals"] = {
        field: sum(int(rule.get(field) or 0) for rule in report["rules"].values())
        for field in numeric_fields
    }
    error_count = sum(len(rule.get("errors") or []) for rule in report["rules"].values())
    report["totals"]["errors"] = error_count
    report["ok"] = error_count == 0
    report["completed_at"] = datetime.now(CN_TZ).isoformat(timespec="seconds")
    if not dry_run:
        destination = Path(state_path) if state_path is not None else data_root / DEFAULT_STATE_PATH.name
        if destination.parent.resolve(strict=True) != data_root.resolve(strict=True):
            raise RetentionSafetyError("retention state path must stay directly under data root")
        _atomic_write_report(destination, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform allowlisted cleanup (default is a non-mutating dry run)",
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        policy = load_retention_policy(args.policy)
        report = run_data_retention(policy=policy, dry_run=not args.apply)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": _safe_error(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
