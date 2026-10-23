"""打包：生成确定性 ZIP（排除虚拟环境、缓存、运行期产物）。

用法： python tools/package.py [输出目录]
输出： <输出目录>/LintLoom-1.0.0.zip
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"

EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".lintloom-work",
    ".lintloom-backups",
    ".tmp",
    "node_modules",
    "shots",
    "dist",
    "build",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".zip"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}

INCLUDE_TOP = [
    "app",
    "tests",
    "tools",
    "samples",
    "workspace",
    "docs",
    "run.py",
    "requirements.txt",
    "README.md",
    "SOURCES.md",
    "LICENSE",
    "NOTICE",
    ".gitignore",
    "install.cmd",
    "start.cmd",
    "test.cmd",
]


def iter_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_TOP:
        target = PROJECT / name
        if not target.exists():
            continue
        if target.is_file():
            files.append(target)
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(PROJECT)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIX or path.name in EXCLUDE_NAMES:
                continue
            files.append(path)
    return files


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0]) if argv else PROJECT.parent / "deliver14"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"LintLoom-{VERSION}.zip"

    files = iter_files()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            arc = Path("LintLoom") / path.relative_to(PROJECT)
            zf.write(path, arc.as_posix())

    size = zip_path.stat().st_size
    print(f"{zip_path}  ({size / 1024:.1f} KB, {len(files)} files)")
    for path in files:
        print("  +", path.relative_to(PROJECT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
