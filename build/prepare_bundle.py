#!/usr/bin/env python3
"""Create the encrypted config/data bundle used by the scheduled deployment.

The bundle contains the minimum runtime state the dashboard server needs to
reconstruct live quotes, portfolio P&L, risk, news, paper-trading and autopilot
views.  It is encrypted with AES-256-CBC + PBKDF2 before it is committed to the
public deployment repository.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

INTRADAY_RE = re.compile(r"intraday-pnl-(\d{4}-\d{2}-\d{2})\.jsonl$")

SINGLE_DATA_FILES = (
    "daily-pnl-history.json",
    "dashboard-news-cache.json",
    "dashboard-market-news-cache.json",
    "dashboard-quote-cache.json",
    "portfolio-performance.json",
    "market-snapshot.json",
    "macro-snapshot.json",
    "risk-snapshot.json",
    "position-advice-state.json",
    "position-advice-audit.jsonl",
    "forward-validation.jsonl",
    "news-sentiment-log.jsonl",
    "school-signal-log.jsonl",
    "data-retention-state.json",
)

DATA_DIRS = (
    "intraday-pnl-backfill",
    "paper-trading",
    "autopilot",
    "portfolio-analysis-2026-08-31",
)


def log(message: str) -> None:
    print(f"[bundle] {message}", flush=True)


def copy_file(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def recent_files(directory: Path, pattern: re.Pattern[str], limit: int) -> list[Path]:
    matches: list[tuple[str, Path]] = []
    for path in directory.glob("*.jsonl"):
        match = pattern.search(path.name)
        if match:
            matches.append((match.group(1), path))
    matches.sort(reverse=True)
    return [path for _, path in matches[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build encrypted source-data bundle")
    parser.add_argument("--project", required=True, help="LDLChinaStock-V1.1 project root")
    parser.add_argument("--out", required=True, help="bundle output directory")
    parser.add_argument("--history-days", type=int, default=10)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    key = os.environ.get("SOURCE_DATA_KEY", "")
    if len(key) < 16:
        print("SOURCE_DATA_KEY must be set to a strong passphrase (>=16 chars)", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="asd-bundle-") as tmp:
        stage = Path(tmp)
        config_src = project / "config"
        if not config_src.is_dir():
            raise SystemExit(f"missing config directory: {config_src}")
        shutil.copytree(config_src, stage / "config")

        data_out = stage / "data"
        data_out.mkdir(parents=True, exist_ok=True)
        copied = 0
        for name in SINGLE_DATA_FILES:
            if copy_file(project / "data" / name, data_out / name):
                copied += 1
        for name in DATA_DIRS:
            src = project / "data" / name
            if src.is_dir():
                shutil.copytree(src, data_out / name)
                copied += 1

        # Extra portfolio-specific P&L caches.
        for path in (project / "data").glob("daily-pnl-history-*.json"):
            copy_file(path, data_out / path.name)

        # Intraday minute bars: only the most recent dates are needed for the
        # dashboard's date selector.  Keep the same date window for main and
        # each extra portfolio.
        intraday_src = project / "data" / "intraday-pnl"
        intraday_dst = data_out / "intraday-pnl"
        if intraday_src.is_dir():
            intraday_dst.mkdir(parents=True, exist_ok=True)
            for path in recent_files(intraday_src, INTRADAY_RE, args.history_days):
                copy_file(path, intraday_dst / path.name)
            for portfolio_dir in sorted(intraday_src.glob("portfolios/*")):
                if not portfolio_dir.is_dir():
                    continue
                target = intraday_dst / "portfolios" / portfolio_dir.name
                target.mkdir(parents=True, exist_ok=True)
                for path in recent_files(portfolio_dir, INTRADAY_RE, args.history_days):
                    copy_file(path, target / path.name)
            copied += 1

        # The personal index snapshot lives under the dashboard directory but is
        # personal data, so it is decrypted into the server tree at build time.
        personal_index = project / "dashboard" / "personal-index"
        if personal_index.is_dir():
            shutil.copytree(
                personal_index,
                stage / "dashboard" / "personal-index",
                ignore=shutil.ignore_patterns("*.pre-rebuild.json", ".wtest"),
            )
            copied += 1

        tar_path = out_dir / "source-data.tar.gz"
        with tarfile.open(tar_path, "w:gz") as archive:
            for child in sorted(stage.iterdir()):
                archive.add(child, arcname=child.name)
        log(f"staged {copied} data groups, tar={tar_path.stat().st_size} bytes")

        encrypted_path = out_dir / "source-data.tar.gz.enc"
        env = dict(os.environ)
        subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-iter",
                "200000",
                "-salt",
                "-in",
                str(tar_path),
                "-out",
                str(encrypted_path),
                "-pass",
                "env:SOURCE_DATA_KEY",
            ],
            check=True,
            env=env,
        )
        tar_path.unlink(missing_ok=True)
        log(f"encrypted bundle: {encrypted_path} ({encrypted_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
