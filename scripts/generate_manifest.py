#!/usr/bin/env python3
"""重新生成 MANIFEST.sha256（只覆盖确定性内容）。

为什么需要这个脚本
------------------
旧的清单把 `data/` 下每天都会变的行情缓存、盘中账本、建议审计链也纳入校验，
导致 `shasum -c MANIFEST.sha256` 几乎总是失败。一份「永远 FAILED」的校验清单
等于没有校验——真正被篡改的源码会淹没在大量预期内的数据差异里。

因此清单只覆盖**可复现、不应随意变动**的内容：

* 源码：`scripts/`、`dashboard/`（前端代码与静态资源）
* 配置：`config/`、`package.json`、依赖锁与 requirements
* 文档与基线：`*.md`、`docs/`
* 测试：`tests/`

明确排除：`data/`（运行时数据）、`node_modules/`、`.venv/`、`data/runtime/`、
缓存、清单自身与临时文件。

用法
----
    .venv/bin/python scripts/generate_manifest.py            # 重写 MANIFEST.sha256
    .venv/bin/python scripts/generate_manifest.py --check    # 只校验，不写入

`--check` 在存在差异时返回退出码 1，可直接用于 CI。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.sha256"

# 纳入校验的顶层路径（相对仓库根）。
INCLUDE_DIRS = ("scripts", "dashboard", "config", "docs", "tests")
INCLUDE_FILES = (
    ".gitignore",
    ".nvmrc",
    "BASELINE.md",
    "README.md",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "requirements-a-stock-data.txt",
    "requirements-lock.txt",
)

# 即使位于 INCLUDE_DIRS 内也必须排除的路径片段。
EXCLUDE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "vendor",  # 第三方前端库，由上游发布，不随本项目演进
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".swp")
EXCLUDE_NAMES = {".DS_Store", "MANIFEST.sha256"}
# 个人指数底稿是每天刷新的运行时快照，其历史由 rebuild 元数据锚定。
EXCLUDE_RELATIVE = {"dashboard/personal-index/index_data.json"}


def _iter_candidates() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for dirname in INCLUDE_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDE_PARTS for part in relative.parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            if path.name in EXCLUDE_NAMES:
                continue
            if relative.as_posix() in EXCLUDE_RELATIVE:
                continue
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_existing() -> dict[str, str]:
    if not MANIFEST.is_file():
        return {}
    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if digest and name:
            entries[name.strip()] = digest.strip()
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the deterministic MANIFEST.sha256")
    parser.add_argument("--check", action="store_true", help="verify only, do not write")
    args = parser.parse_args(argv)

    candidates = _iter_candidates()
    current = {path.relative_to(ROOT).as_posix(): _digest(path) for path in candidates}
    existing = _read_existing()

    added = sorted(set(current) - set(existing))
    removed = sorted(set(existing) - set(current))
    changed = sorted(name for name in set(current) & set(existing) if current[name] != existing[name])

    if args.check:
        for label, names in (("added", added), ("removed", removed), ("changed", changed)):
            for name in names:
                print(f"{label}: {name}")
        if added or removed or changed:
            print(
                f"MANIFEST 不一致：新增 {len(added)}，删除 {len(removed)}，变更 {len(changed)}",
                file=sys.stderr,
            )
            return 1
        print(f"MANIFEST 一致（{len(current)} 个文件）")
        return 0

    lines = [f"{current[name]}  {name}" for name in sorted(current)]
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {MANIFEST.relative_to(ROOT)}：{len(current)} 个文件")
    if added or removed or changed:
        print(f"  新增 {len(added)}，删除 {len(removed)}，变更 {len(changed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
