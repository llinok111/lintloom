#!/usr/bin/env python
"""LintLoom 启动入口。

用法：
    python run.py                 # 默认 127.0.0.1:8731，自动开浏览器
    python run.py --port 8801     # 换端口
    python run.py --read-only     # 只允许诊断/预览，禁止写回
    python run.py --root work=D:\\sql   # 追加一个根目录（运维可信输入）
    python run.py --selftest      # 只做环境自检并退出（CI/排查用）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app import config
from app.catalog import CatalogService
from app.engine import Engine
from app.paths import DIALECT_RE

LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lintloom",
        description="LintLoom —— 把 sqlfluff 的诊断 / 修复 / 语法树变成可操作界面",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8731, help="监听端口（0 表示自动分配）")
    parser.add_argument("--python", default=sys.executable, help="运行 sqlfluff 的解释器路径")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="追加可访问的根目录（可重复），例如 --root work=D:\\sql",
    )
    parser.add_argument("--read-only", action="store_true", help="禁止「应用修复」写回磁盘")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--timeout-cap", type=float, default=config.TIMEOUT_MAX, help="单命令超时上限（秒）")
    parser.add_argument("--selftest", action="store_true", help="打印环境自检 JSON 后退出")
    return parser


def build_settings(args: argparse.Namespace) -> config.Settings:
    roots = dict(config.DEFAULT_ROOTS)
    for spec in args.root:
        if "=" not in spec:
            raise SystemExit(f"--root 需要 LABEL=PATH 形式，收到: {spec!r}")
        label, raw_path = spec.split("=", 1)
        label = label.strip()
        if not LABEL_RE.match(label):
            raise SystemExit(f"根目录标识非法: {label!r}（只允许小写字母开头、含小写字母数字_ -）")
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            raise SystemExit(f"根目录不存在或不是目录: {path}")
        roots[label] = path

    if not (1 <= args.port <= 65535):
        raise SystemExit("端口必须在 1-65535 之间（0 表示自动分配由系统决定）")
    return config.Settings(
        host=args.host,
        port=args.port,
        python_exe=args.python,
        roots=roots,
        timeout_cap=max(5.0, min(args.timeout_cap, 3600.0)),
        allow_apply=not args.read_only,
        open_browser=not args.no_browser,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = build_settings(args)

    if args.selftest:
        cat = CatalogService(settings.python_exe)
        engine = Engine(settings, cat)
        payload = engine.probe_env()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["catalog"]["ok"] else 3

    from app.server import serve_forever

    serve_forever(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
