#!/usr/bin/env python3
"""Low-frequency, authenticated A-share quote validation via HiThink CLI.

The API key is deliberately never loaded by this module.  The official CLI
reads it from the user's system keyring.  Public-feed quotes remain separate;
callers decide whether to use a validated official quote as an explicit
fallback and must preserve source provenance.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


CN_TZ = timezone(timedelta(hours=8))
MAX_STDOUT_BYTES = 2 * 1024 * 1024
RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60.0
DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0


def _safe_error_message(value: Any) -> str:
    message = str(value or "")[:800]
    message = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]", message)
    message = re.sub(r"(?i)\b(?:sk|key|token)-[A-Za-z0-9_-]{8,}", "[REDACTED]", message)
    return message


def _iso_now() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _timestamp_ms_to_iso(value: Any) -> str:
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, CN_TZ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class HithinkQuoteError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class HithinkQuoteProvider:
    """Single-flight cache around ``hithink-finance market snapshot``."""

    def __init__(
        self,
        root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], datetime] = lambda: datetime.now(CN_TZ),
        timeout_seconds: float = 15.0,
    ) -> None:
        self.root = Path(root)
        self.cli_path = self.root / "node_modules" / ".bin" / "hithink-finance"
        self.project_node_path = self.root / "data" / "runtime" / "node" / "bin" / "node"
        self.cli_entry_path = (
            self.root
            / "node_modules"
            / "@hithink-tech"
            / "hithink-finance-cli"
            / "dist"
            / "cli"
            / "main.js"
        )
        self._runner = runner
        self._monotonic = monotonic
        self._wall_now = wall_now
        self.timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "quotes": {},
            "requested_codes": [],
            "unsupported_codes": [],
            "source_timestamp": "",
            "last_attempt_at": "",
            "last_success_at": "",
            "success_monotonic": 0.0,
            "next_retry_monotonic": 0.0,
            "last_error": "",
            "last_error_code": "",
            "refreshing": False,
        }

    def _command_prefix(self) -> list[str]:
        """Prefer the checksum-pinned project Node over an ambient runtime."""

        if self.project_node_path.is_file() and self.cli_entry_path.is_file():
            return [str(self.project_node_path), str(self.cli_entry_path)]
        return [str(self.cli_path)]

    @staticmethod
    def ttl_seconds(phase: str) -> float:
        if phase in {"open", "auction"}:
            return 15.0
        if phase in {"pre_open", "lunch"}:
            return 60.0
        return 300.0

    @staticmethod
    def normalize_codes(codes: list[str] | tuple[str, ...]) -> list[str]:
        return sorted(
            {
                str(code).strip().upper()
                for code in codes
                if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", str(code).strip().upper())
            }
        )

    def _run_once(self, codes: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
        if not self.cli_path.is_file():
            raise HithinkQuoteError("hithink-finance CLI 未安装", code="CLI_NOT_FOUND")
        command = [
            *self._command_prefix(),
            "market",
            "snapshot",
            "--thscodes",
            ",".join(codes),
            "--limit",
            str(max(1, len(codes))),
            "--format",
            "json",
        ]
        try:
            result = self._runner(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HithinkQuoteError("同花顺授权行情调用超时", code="CLI_TIMEOUT", retryable=True) from exc
        except OSError as exc:
            raise HithinkQuoteError(
                f"同花顺授权行情进程启动失败：{type(exc).__name__}",
                code="CLI_START_FAILED",
                retryable=True,
            ) from exc

        stdout = result.stdout or ""
        if len(stdout.encode("utf-8", "replace")) > MAX_STDOUT_BYTES:
            raise HithinkQuoteError("同花顺授权行情响应异常过大", code="CLI_OUTPUT_TOO_LARGE")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HithinkQuoteError(
                "同花顺授权行情未返回有效 JSON",
                code="CLI_INVALID_JSON",
                retryable=True,
            ) from exc

        if result.returncode != 0 or envelope.get("ok") is not True:
            error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
            code = str(error.get("code") or f"CLI_EXIT_{result.returncode}")
            message = str(error.get("message") or "同花顺授权行情调用失败")
            retryable = bool(error.get("retryable"))
            raise HithinkQuoteError(message, code=code, retryable=retryable)

        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        items = data.get("item") if isinstance(data.get("item"), list) else []
        quotes: dict[str, dict[str, Any]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            thscode = str(raw.get("thscode") or "").upper()
            price = _positive_float(raw.get("last_price"))
            if thscode not in codes or price is None:
                continue
            quotes[thscode] = {
                "thscode": thscode,
                "price": price,
                "prev_close": _optional_float(raw.get("prev_price")),
                "open": _optional_float(raw.get("open_price")),
                "high": _optional_float(raw.get("high_price")),
                "low": _optional_float(raw.get("low_price")),
                "change": _optional_float(raw.get("price_change")),
                "change_pct": _optional_float(raw.get("price_change_ratio_pct")),
                "volume": _optional_float(raw.get("volume")),
                "turnover": _optional_float(raw.get("turnover")),
            }
        if not quotes:
            raise HithinkQuoteError(
                "同花顺授权行情成功响应中没有有效报价",
                code="CLI_EMPTY_RESULT",
                retryable=True,
            )
        return quotes, _timestamp_ms_to_iso(data.get("timestamp"))

    @staticmethod
    def _unknown_code(exc: HithinkQuoteError) -> str:
        if exc.code != "FUYAO_1002":
            return ""
        match = re.search(r"Unknown thscode:\s*(\d{6}\.(?:SH|SZ|BJ))", str(exc), re.I)
        return match.group(1).upper() if match else ""

    def _fetch_with_unknown_code_isolation(
        self, codes: list[str]
    ) -> tuple[dict[str, dict[str, Any]], str, list[str]]:
        remaining = list(codes)
        unsupported: list[str] = []
        # A malformed/unsupported symbol rejects the whole remote batch.  Isolate
        # explicit FUYAO_1002 symbols with a bounded retry instead of discarding
        # every valid quote.  The cap prevents an accidental request loop.
        for _ in range(min(3, len(codes)) + 1):
            try:
                quotes, source_timestamp = self._run_once(remaining)
                return quotes, source_timestamp, unsupported
            except HithinkQuoteError as exc:
                unknown = self._unknown_code(exc)
                if not unknown or unknown not in remaining:
                    raise
                unsupported.append(unknown)
                remaining.remove(unknown)
                if not remaining:
                    raise HithinkQuoteError(
                        "请求标的均不在同花顺 A 股行情目录中",
                        code="ALL_CODES_UNSUPPORTED",
                    ) from exc
        raise HithinkQuoteError("同花顺标的隔离重试次数耗尽", code="ISOLATION_LIMIT")

    def refresh(self, codes: list[str] | tuple[str, ...]) -> None:
        normalized = self.normalize_codes(codes)
        if not normalized:
            with self._lock:
                self._state.update(
                    requested_codes=[],
                    quotes={},
                    unsupported_codes=[],
                    last_error="没有可供同花顺校验的 A 股代码",
                    last_error_code="NO_CODES",
                    refreshing=False,
                )
            return
        attempt_at = _iso_now()
        with self._lock:
            self._state["last_attempt_at"] = attempt_at
        try:
            quotes, source_timestamp, unsupported = self._fetch_with_unknown_code_isolation(normalized)
        except HithinkQuoteError as exc:
            cooldown = (
                RATE_LIMIT_COOLDOWN_SECONDS
                if "429" in exc.code or "RATE" in exc.code.upper()
                else DEFAULT_FAILURE_COOLDOWN_SECONDS
            )
            with self._lock:
                self._state.update(
                    requested_codes=normalized,
                    last_error=_safe_error_message(exc),
                    last_error_code=exc.code,
                    next_retry_monotonic=self._monotonic() + cooldown,
                    refreshing=False,
                )
            return
        except Exception as exc:  # provider failure must never kill public quotes
            with self._lock:
                self._state.update(
                    requested_codes=normalized,
                    last_error=_safe_error_message(f"{type(exc).__name__}: {exc}"),
                    last_error_code="UNEXPECTED_PROVIDER_ERROR",
                    next_retry_monotonic=self._monotonic() + DEFAULT_FAILURE_COOLDOWN_SECONDS,
                    refreshing=False,
                )
            return

        success_at = _iso_now()
        with self._lock:
            self._state.update(
                quotes=quotes,
                requested_codes=normalized,
                unsupported_codes=unsupported,
                source_timestamp=source_timestamp,
                last_success_at=success_at,
                success_monotonic=self._monotonic(),
                next_retry_monotonic=0.0,
                last_error="",
                last_error_code="",
                refreshing=False,
            )

    def _worker(self, codes: list[str]) -> None:
        self.refresh(codes)

    def ensure_refresh(
        self,
        codes: list[str] | tuple[str, ...],
        phase: str,
        *,
        force: bool = False,
    ) -> bool:
        normalized = self.normalize_codes(codes)
        now = self._monotonic()
        with self._lock:
            age = now - float(self._state.get("success_monotonic") or 0.0)
            same_codes = normalized == list(self._state.get("requested_codes") or [])
            retry_at = float(self._state.get("next_retry_monotonic") or 0.0)
            should_refresh = (
                force
                or not same_codes
                or not self._state.get("quotes")
                or age >= self.ttl_seconds(phase)
            )
            if self._state.get("refreshing") or not should_refresh or now < retry_at:
                return False
            self._state["refreshing"] = True
        try:
            threading.Thread(
                target=self._worker,
                args=(normalized,),
                name="hithink-quote-validator",
                daemon=True,
            ).start()
            return True
        except Exception as exc:
            with self._lock:
                self._state.update(
                    refreshing=False,
                    last_error=_safe_error_message(
                        f"校验线程启动失败：{type(exc).__name__}: {exc}"
                    ),
                    last_error_code="THREAD_START_FAILED",
                    next_retry_monotonic=now + DEFAULT_FAILURE_COOLDOWN_SECONDS,
                )
            return False

    def snapshot(
        self,
        codes: list[str] | tuple[str, ...],
        phase: str,
    ) -> dict[str, Any]:
        normalized = self.normalize_codes(codes)
        now = self._monotonic()
        with self._lock:
            state = copy.deepcopy(self._state)
        success_monotonic = float(state.pop("success_monotonic", 0.0) or 0.0)
        retry_at = float(state.pop("next_retry_monotonic", 0.0) or 0.0)
        age = None if not success_monotonic else max(0.0, now - success_monotonic)
        ttl = self.ttl_seconds(phase)
        source_timestamp = str(state.get("source_timestamp") or "")
        source_age: float | None = None
        source_clock_skew = False
        if source_timestamp:
            try:
                source_dt = datetime.fromisoformat(source_timestamp)
                if source_dt.tzinfo is None:
                    source_dt = source_dt.replace(tzinfo=CN_TZ)
                else:
                    source_dt = source_dt.astimezone(CN_TZ)
                source_age = (self._wall_now().astimezone(CN_TZ) - source_dt).total_seconds()
                source_clock_skew = source_age < -60.0
                source_age = max(0.0, source_age)
            except (TypeError, ValueError):
                source_age = None
        coverage = len(set(normalized) & set((state.get("quotes") or {}).keys()))
        unsupported = sorted(set(state.get("unsupported_codes") or []) & set(normalized))
        source_stale = bool(
            source_age is None
            or source_clock_skew
            or source_age > max(ttl * 2.0, 90.0)
        )
        stale = age is None or age > max(ttl * 2.0, 30.0) or source_stale
        if not self.cli_path.is_file():
            status = "not_configured"
        elif state.get("refreshing") and age is None:
            status = "pending"
        elif stale:
            status = "unavailable" if age is None else "stale"
        elif coverage < len(normalized) or unsupported or state.get("last_error"):
            status = "degraded"
        else:
            status = "healthy"
        return {
            "provider": "同花顺金融数据服务",
            "role": "授权行情交叉校验与腾讯失效时的股票价格备援",
            "status": status,
            "configured": self.cli_path.is_file(),
            "runtime": (
                "project-node"
                if self.project_node_path.is_file() and self.cli_entry_path.is_file()
                else "ambient-node"
            ),
            "refreshing": bool(state.get("refreshing")),
            "requested_count": len(normalized),
            "coverage_count": coverage,
            "unsupported_codes": unsupported,
            "quotes": state.get("quotes") or {},
            "source_timestamp": source_timestamp,
            "source_age_seconds": None if source_age is None else round(source_age, 1),
            "source_clock_skew": source_clock_skew,
            "source_stale": source_stale,
            "last_attempt_at": state.get("last_attempt_at") or "",
            "last_success_at": state.get("last_success_at") or "",
            "age_seconds": None if age is None else round(age, 1),
            "ttl_seconds": ttl,
            "stale": stale,
            "retry_after_seconds": round(max(0.0, retry_at - now), 1),
            "last_error": state.get("last_error") or "",
            "last_error_code": state.get("last_error_code") or "",
        }
