"""上游能力目录探针（子进程执行，输出 JSON 到 stdout）。

为什么用一个独立脚本而不是直接在当前进程里 import sqlfluff？
- 主服务进程对上游库零依赖：没装 sqlfluff 时界面仍然能起来并给出「环境缺失」诊断，
  而不是整个进程 ImportError 崩掉。
- 每次探测都是独立进程，上游的模块级副作用不会污染服务进程。
- 这里只调用 sqlfluff 的**公开 API**（`sqlfluff.core.Linter.rule_tuples()` 与
  `sqlfluff.core.dialects.dialect_readout()`），不读私有属性。

本脚本不含任何用户输入，是一个静态文件，因此不存在注入面。
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        import sqlfluff
        from sqlfluff.core import FluffConfig, Linter
        from sqlfluff.core.dialects import dialect_readout
    except Exception as exc:  # noqa: BLE001
        json.dump(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "sqlfluff 未安装到当前解释器环境，请先运行 安装.cmd / pip install -r requirements.txt",
            },
            sys.stdout,
            ensure_ascii=False,
        )
        return 3

    cfg = FluffConfig(overrides={"dialect": "ansi"})
    linter = Linter(config=cfg)
    rules = [
        {
            "code": t.code,
            "name": t.name,
            "description": t.description,
            "groups": list(t.groups),
            "aliases": list(t.aliases),
        }
        for t in linter.rule_tuples()
    ]
    dialects = [
        {
            "label": d.label,
            "name": d.name,
            "inherits_from": d.inherits_from or "nothing",
        }
        for d in dialect_readout()
    ]
    json.dump(
        {
            "ok": True,
            "version": getattr(sqlfluff, "__version__", "unknown"),
            "dialects": sorted(dialects, key=lambda d: d["label"]),
            "rules": rules,
            "groups": sorted({g for r in rules for g in r["groups"]}),
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
