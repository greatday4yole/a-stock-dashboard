#!/usr/bin/env python3
"""Build the encrypted static site served by GitHub Pages."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

STATIC_FILES = (
    "index.html",
    "styles.css",
    "app.js",
    "autopilot.html",
    "autopilot.js",
    "vendor/echarts.min.js",
)


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def derive_key(password: str, meta: dict[str, Any]) -> bytes:
    salt = base64.b64decode(str(meta["salt"]))
    iterations = int(meta.get("iterations") or 210000)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_bytes(key: bytes, plaintext: bytes) -> bytes:
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return iv + ciphertext


def encrypt_json(key: bytes, payload: Any) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encrypt_bytes(key, raw)


def rewrite_html(html: str, is_autopilot: bool) -> str:
    html = html.replace('href="/styles.css', 'href="styles.css')
    html = html.replace('href="/autopilot.html', 'href="autopilot.html')
    html = html.replace('href="/"', 'href="./"')
    html = html.replace('src="/vendor/', 'src="vendor/')
    html = html.replace('src="/app.js', 'src="app.js')
    html = html.replace('src="/autopilot.js', 'src="autopilot.js')
    # Personal notes are intentionally not published. Keep the surrounding
    # sentence but drop the dangling link.
    html = re.sub(r'<a href="/notes/[^"]+"[^>]*>([^<]*)</a>', r"\1", html)
    if 'name="robots"' not in html:
        html = html.replace(
            "</head>",
            '  <meta name="robots" content="noindex,nofollow" />\n  </head>',
        )
    if "static-api-shim.js" not in html:
        html = html.replace(
            "</head>",
            '  <script src="static-api-shim.js"></script>\n  </head>',
        )
    return html


def copy_static(source: Path, out: Path) -> None:
    for relative in STATIC_FILES:
        src = source / relative
        if not src.is_file():
            raise SystemExit(f"missing static asset: {src}")
        dst = out / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for page in ("index.html", "autopilot.html"):
        path = out / page
        path.write_text(
            rewrite_html(path.read_text(encoding="utf-8"), page == "autopilot.html"),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build encrypted static dashboard")
    parser.add_argument("--source", default="dashboard")
    parser.add_argument("--out", default="site")
    parser.add_argument("--export-dir", default="export", help="directory containing static-api/*.json")
    parser.add_argument("--password", required=True)
    parser.add_argument("--meta", default="build/crypto-meta.json")
    parser.add_argument("--previous", default="", help="previous encrypted static-api cache")
    parser.add_argument("--full", action="store_true", help="mark this build as a full refresh")
    args = parser.parse_args()

    source = Path(args.source)
    out = Path(args.out)
    export_dir = Path(args.export_dir) / "static-api"
    previous = Path(args.previous) if args.previous else None
    meta_path = Path(args.meta)
    if not meta_path.is_file():
        raise SystemExit(f"missing crypto metadata: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    key = derive_key(args.password, meta)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    log("copying static assets")
    copy_static(source, out)

    shim = Path("build/static-api-shim.js")
    if not shim.is_file():
        raise SystemExit(f"missing shim: {shim}")
    shutil.copy2(shim, out / "static-api-shim.js")

    static_api = out / "static-api"
    static_api.mkdir(parents=True, exist_ok=True)

    # Fresh exports are plaintext until this point. Encrypt, then delete the
    # plaintext copy so the Pages artifact contains no readable position data.
    fresh_count = 0
    kept_previous: list[str] = []
    if not export_dir.is_dir():
        raise SystemExit(f"missing export directory: {export_dir}")
    for path in sorted(export_dir.glob("*.json")):
        if path.name.startswith("manifest-export"):
            continue
        key_name = path.name[: -len(".json")]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log(f"skip unreadable export: {path.name}")
            continue
        previous_file = previous / "static-api" / f"{key_name}.json.enc" if previous else None
        if (
            isinstance(payload, dict)
            and payload.get("error_code") == "STATIC_EXPORT_FAILED"
            and previous_file
            and previous_file.is_file()
        ):
            shutil.copy2(previous_file, static_api / f"{key_name}.json.enc")
            kept_previous.append(key_name)
        else:
            (static_api / f"{key_name}.json.enc").write_bytes(encrypt_json(key, payload))
            fresh_count += 1

    # Fast runs skip the slow endpoints; reuse the latest encrypted snapshot.
    if previous and (previous / "static-api").is_dir():
        for previous_file in sorted((previous / "static-api").glob("*.json.enc")):
            target = static_api / previous_file.name
            if not target.exists():
                shutil.copy2(previous_file, target)
                kept_previous.append(previous_file.name[: -len(".json.enc")])

    verify_payload = {
        "ok": True,
        "site": "a-stock-dashboard",
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (static_api / "verify.json.enc").write_bytes(encrypt_json(key, verify_payload))

    manifest_keys = sorted(
        path.name[: -len(".json.enc")]
        for path in static_api.glob("*.json.enc")
        if path.name != "verify.json.enc"
    )
    manifest = {
        "generated_at": verify_payload["generated_at"],
        "full": bool(args.full),
        "count": len(manifest_keys),
        "keys": manifest_keys,
    }
    (static_api / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    shutil.copy2(meta_path, out / "crypto-meta.json")

    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / "404.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>未找到</title>'
        '<p>页面不存在。<a href="./">返回持仓行情</a></p>',
        encoding="utf-8",
    )

    log(
        f"built {out} fresh={fresh_count} reused={len(set(kept_previous))} "
        f"keys={len(manifest_keys)} full={bool(args.full)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
