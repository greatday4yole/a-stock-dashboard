#!/usr/bin/env python3
"""Supervise dashboard_server.py under launchd with graceful termination.

launchd terminates jobs with SIGTERM while dashboard_server.py intentionally
uses KeyboardInterrupt for its cleanup path.  This small stdlib-only process
translates TERM to INT, applies bounded escalation, and captures child output in
size-limited rotating logs.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO


LOCKED_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_KILL_AFTER_SECONDS = 5.0
SAFE_ENVIRONMENT_KEYS = (
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


class SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler that reapplies owner-only mode after every rollover."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except PermissionError:
            stream.close()
            raise
        return stream


def child_command(
    python_executable: Path, working_directory: Path, host: str, port: int
) -> list[str]:
    if host != LOCKED_HOST:
        raise ValueError(f"host is locked to {LOCKED_HOST}")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return [
        str(python_executable),
        str(working_directory / "scripts" / "dashboard_server.py"),
        "--host",
        LOCKED_HOST,
        "--port",
        str(port),
    ]


def safe_child_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build a small allowlisted environment that cannot forward API keys."""

    source = os.environ if source is None else source
    environment = {
        key: source[key]
        for key in SAFE_ENVIRONMENT_KEYS
        if key in source and source[key]
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment["PYTHONUNBUFFERED"] = "1"
    environment.setdefault("LC_CTYPE", "UTF-8")
    node_ca_bundle = environment.get("NODE_EXTRA_CA_CERTS")
    if node_ca_bundle:
        ca_path = Path(node_ca_bundle)
        if not ca_path.is_absolute() or not ca_path.is_file() or not os.access(ca_path, os.R_OK):
            environment.pop("NODE_EXTRA_CA_CERTS", None)
    return environment


def make_logger(log_directory: Path, max_log_bytes: int, backup_count: int) -> logging.Logger:
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        log_directory.chmod(0o700)
    except PermissionError:
        pass

    log_path = log_directory / "dashboard.log"
    log_path.touch(mode=0o600, exist_ok=True)
    try:
        log_path.chmod(0o600)
    except PermissionError:
        pass

    logger = logging.getLogger(f"ldl-dashboard-supervisor-{os.getpid()}-{id(log_directory)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = SecureRotatingFileHandler(
        log_path,
        maxBytes=max_log_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def validate_runtime(python_executable: Path, working_directory: Path) -> None:
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise ValueError(f"working directory must be an existing absolute path: {working_directory}")
    if not python_executable.is_absolute():
        raise ValueError(f"python executable must be an absolute path: {python_executable}")
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        raise ValueError(f"python executable is not executable: {python_executable}")

    required = (
        working_directory / "scripts" / "dashboard_server.py",
        working_directory / "dashboard" / "index.html",
        working_directory / "config" / "portfolio.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"required dashboard files are missing: {', '.join(missing)}")

    dependency = subprocess.run(
        [str(python_executable), "-c", "import requests"],
        cwd=working_directory,
        env=safe_child_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if dependency.returncode != 0:
        detail = (dependency.stderr or dependency.stdout).strip()
        raise ValueError(f"Python environment cannot import requests: {detail or 'unknown error'}")


def _signal_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _log_output_chunk(logger: logging.Logger, buffer: bytearray, chunk: bytes) -> None:
    buffer.extend(chunk)
    while b"\n" in buffer:
        raw_line, _, remainder = buffer.partition(b"\n")
        buffer[:] = remainder
        logger.info("dashboard: %s", raw_line.decode("utf-8", errors="replace"))
    if len(buffer) > 64 * 1024:
        logger.info("dashboard: %s", bytes(buffer).decode("utf-8", errors="replace"))
        buffer.clear()


def supervise(
    command: list[str],
    *,
    working_directory: Path,
    logger: logging.Logger,
    grace_seconds: float,
    kill_after_seconds: float,
    environment: dict[str, str] | None = None,
) -> int:
    process = subprocess.Popen(
        command,
        cwd=working_directory,
        env=safe_child_environment() if environment is None else environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        bufsize=0,
    )
    assert process.stdout is not None
    output: BinaryIO = process.stdout
    selector = selectors.DefaultSelector()
    selector.register(output, selectors.EVENT_READ)
    buffered = bytearray()
    requested_signal: int | None = None
    shutdown_stage = "running"
    deadline: float | None = None

    def request_shutdown(signum: int, _frame: object) -> None:
        nonlocal requested_signal
        requested_signal = signum

    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    for sig in previous_handlers:
        signal.signal(sig, request_shutdown)

    logger.info("starting dashboard pid=%s command=%r", process.pid, command)
    try:
        while True:
            now = time.monotonic()
            if requested_signal is not None and shutdown_stage == "running":
                logger.info("shutdown requested by signal %s; forwarding SIGINT", requested_signal)
                _signal_process_group(process, signal.SIGINT)
                shutdown_stage = "interrupt"
                deadline = now + grace_seconds
            elif (
                shutdown_stage == "interrupt"
                and deadline is not None
                and now >= deadline
                and process.poll() is None
            ):
                logger.warning("dashboard did not exit after %.1fs; forwarding SIGTERM", grace_seconds)
                _signal_process_group(process, signal.SIGTERM)
                shutdown_stage = "terminate"
                deadline = now + kill_after_seconds
            elif (
                shutdown_stage == "terminate"
                and deadline is not None
                and now >= deadline
                and process.poll() is None
            ):
                logger.error("dashboard did not exit after TERM; forwarding SIGKILL")
                _signal_process_group(process, signal.SIGKILL)
                shutdown_stage = "kill"
                deadline = None

            events = selector.select(timeout=0.25)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 16 * 1024)
                if chunk:
                    _log_output_chunk(logger, buffered, chunk)
                else:
                    selector.unregister(key.fileobj)

            return_code = process.poll()
            if return_code is not None:
                remaining = output.read()
                if remaining:
                    _log_output_chunk(logger, buffered, remaining)
                if buffered:
                    logger.info("dashboard: %s", buffered.decode("utf-8", errors="replace"))
                    buffered.clear()
                if requested_signal is not None:
                    logger.info("dashboard stopped cleanly after launchd shutdown request (rc=%s)", return_code)
                    return 0
                logger.warning("dashboard exited unexpectedly (rc=%s)", return_code)
                return return_code if return_code > 0 else 1
    finally:
        selector.close()
        output.close()
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        if process.poll() is None:
            _signal_process_group(process, signal.SIGKILL)
            process.wait(timeout=5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, dest="python_executable")
    parser.add_argument("--working-directory", required=True)
    parser.add_argument("--host", default=LOCKED_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log-directory", required=True)
    parser.add_argument("--max-log-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--backup-count", type=int, default=3)
    parser.add_argument("--grace-seconds", type=float, default=DEFAULT_GRACE_SECONDS)
    parser.add_argument("--kill-after-seconds", type=float, default=DEFAULT_KILL_AFTER_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.umask(0o077)
    python_executable = Path(args.python_executable)
    working_directory = Path(args.working_directory)
    log_directory = Path(args.log_directory).expanduser()

    if args.host != LOCKED_HOST:
        print(f"supervisor refused host {args.host!r}; only {LOCKED_HOST} is allowed", file=sys.stderr)
        return 64
    if not log_directory.is_absolute():
        print("log directory must be an absolute path", file=sys.stderr)
        return 64
    if args.max_log_bytes < 1024 or not 1 <= args.backup_count <= 20:
        print("invalid log rotation limits", file=sys.stderr)
        return 64
    if args.grace_seconds <= 0 or args.kill_after_seconds <= 0:
        print("shutdown timeouts must be positive", file=sys.stderr)
        return 64

    logger = make_logger(log_directory, args.max_log_bytes, args.backup_count)
    try:
        validate_runtime(python_executable, working_directory)
        command = child_command(python_executable, working_directory, args.host, args.port)
        return supervise(
            command,
            working_directory=working_directory,
            logger=logger,
            grace_seconds=args.grace_seconds,
            kill_after_seconds=args.kill_after_seconds,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.exception("supervisor preflight/start failed: %s", exc)
        return 78
    finally:
        close_logger(logger)


if __name__ == "__main__":
    raise SystemExit(main())
