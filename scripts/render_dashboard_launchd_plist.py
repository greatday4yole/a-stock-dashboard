#!/usr/bin/env python3
"""Render a launchd LaunchAgent plist for the local portfolio dashboard.

The renderer deliberately accepts no credential arguments.  It emits only
absolute executable/data paths and pins the dashboard listener to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_LABEL = "com.roglu.ldlchinastock.dashboard"
DEFAULT_PORT = 8765
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$")


def _absolute_path(value: str | Path, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {path}")
    return path


def build_plist(
    *,
    repo_root: str | Path,
    python_executable: str | Path,
    log_directory: str | Path,
    label: str = DEFAULT_LABEL,
    port: int = DEFAULT_PORT,
    node_extra_ca_certs: str | Path | None = None,
) -> dict[str, Any]:
    """Return the complete, credential-free LaunchAgent configuration."""

    if not LABEL_RE.fullmatch(label):
        raise ValueError(f"invalid launchd label: {label!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535: {port}")

    root = _absolute_path(repo_root, "repo_root")
    python = _absolute_path(python_executable, "python_executable")
    log_dir = _absolute_path(log_directory, "log_directory")
    supervisor = root / "scripts" / "launchd_dashboard_supervisor.py"
    dashboard = root / "scripts" / "dashboard_server.py"

    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {root}")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"python_executable is not executable: {python}")
    if not supervisor.is_file():
        raise ValueError(f"supervisor is missing: {supervisor}")
    if not dashboard.is_file():
        raise ValueError(f"dashboard server is missing: {dashboard}")

    environment = {
        "PYTHONUNBUFFERED": "1",
        "LC_CTYPE": "UTF-8",
    }
    if node_extra_ca_certs is not None:
        ca_bundle = _absolute_path(node_extra_ca_certs, "node_extra_ca_certs")
        if not ca_bundle.is_file() or not os.access(ca_bundle, os.R_OK):
            raise ValueError(f"node_extra_ca_certs is not a readable file: {ca_bundle}")
        if b"PRIVATE KEY" in ca_bundle.read_bytes():
            raise ValueError("node_extra_ca_certs must not contain a private key")
        environment["NODE_EXTRA_CA_CERTS"] = str(ca_bundle)

    return {
        "Label": label,
        "ProgramArguments": [
            str(python),
            str(supervisor),
            "--python",
            str(python),
            "--working-directory",
            str(root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-directory",
            str(log_dir),
            "--max-log-bytes",
            str(5 * 1024 * 1024),
            "--backup-count",
            "3",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 15,
        # The supervisor owns bounded rotating logs.  Discarding its inherited
        # descriptors prevents launchd from creating a second unbounded log.
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": environment,
    }


def write_plist(payload: dict[str, Any], output: str) -> None:
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
    if output == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return

    destination = _absolute_path(output, "output")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--python", required=True, dest="python_executable")
    parser.add_argument("--log-directory", required=True)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--node-extra-ca-certs")
    parser.add_argument("--output", required=True, help="absolute path, or - for stdout")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        payload = build_plist(
            repo_root=args.repo_root,
            python_executable=args.python_executable,
            log_directory=args.log_directory,
            label=args.label,
            port=args.port,
            node_extra_ca_certs=args.node_extra_ca_certs,
        )
        write_plist(payload, args.output)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"launchd plist render failed: {exc}") from exc


if __name__ == "__main__":
    main()
