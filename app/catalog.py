"""上游能力目录：方言清单、规则清单。

真实来源两条：
1. 主路径 —— 启动一个独立 Python 子进程跑 `app/catalog_probe.py`，走 sqlfluff 的公开
   Python API（`Linter.rule_tuples()` / `dialect_readout()`），得到权威清单。
2. 兜底 —— 若探针失败（例如上游 API 变更），退化为解析 `sqlfluff dialects` /
   `sqlfluff rules` 的**文本输出**。注意 `dialects` 表格会按列宽截断方言名
   （实测 `materialize` 被切成 `materializ` + 换行 `e`），所以兜底解析必须做续行合并，
   并在结果里标注 `source`，让界面能提示「清单可能不完整」。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

from . import config, runner
from .runner import RunOptions

TTL_SECONDS = 300.0

_DIALECT_LINE = re.compile(r"^([a-z][a-z0-9]*):\s+(.*)$")
_RULE_LINE = re.compile(r"^([A-Z]{2}[0-9]{2}):\s+\[([a-z0-9_.]+)\]\s*(.*)$")
_INHERITS = re.compile(r"\[inherits from '([a-z0-9]+)'\]")


@dataclass
class Catalog:
    ok: bool = False
    source: str = "unavailable"
    version: str = "unknown"
    dialects: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    error: str | None = None
    hint: str | None = None
    fetched_at: float = 0.0

    @property
    def dialect_labels(self) -> set[str]:
        return {d["label"] for d in self.dialects}

    @property
    def rule_refs(self) -> set[str]:
        """可用于 -r / -e 的合法引用：规则码 + 规则名 + 分组名。"""
        refs: set[str] = set()
        for rule in self.rules:
            refs.add(rule["code"])
            refs.add(rule["name"])
            refs.update(rule.get("aliases") or [])
            refs.update(rule.get("groups") or [])
        return refs

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "source": self.source,
            "version": self.version,
            "dialects": self.dialects,
            "rules": self.rules,
            "groups": self.groups,
            "error": self.error,
            "hint": self.hint,
            "fetched_at": self.fetched_at,
            "dialect_count": len(self.dialects),
            "rule_count": len(self.rules),
        }


class CatalogService:
    def __init__(self, python_exe: str | None = None) -> None:
        self._python = python_exe
        self._lock = threading.Lock()
        self._cached: Catalog | None = None
        self._last_attempt = 0.0

    def get(self, force: bool = False) -> Catalog:
        with self._lock:
            now = time.time()
            fresh = (
                self._cached is not None
                and self._cached.ok
                and (now - self._cached.fetched_at) < TTL_SECONDS
            )
            if fresh and not force:
                return self._cached
            # 失败结果缓存 10 秒，避免用户狂点刷新把进程打爆
            if (
                not force
                and self._cached is not None
                and not self._cached.ok
                and (now - self._last_attempt) < 10
            ):
                return self._cached
            self._last_attempt = now
            self._cached = self._probe()
            return self._cached

    # ------------------------------------------------------------------ 探针
    def _python_exe(self) -> str:
        import sys

        return self._python or sys.executable

    def _probe(self) -> Catalog:
        py = self._python_exe()
        script = config.PROJECT_ROOT / "app" / "catalog_probe.py"
        argv = [py, str(script)]
        try:
            res = runner.run(
                argv,
                RunOptions(cwd=config.PROJECT_ROOT, timeout=60.0),
            )
        except runner.RunnerError as exc:
            return self._fallback(str(exc))

        if res.ok and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError as exc:
                return self._fallback(f"探针输出不是合法 JSON: {exc}")
            if data.get("ok"):
                return Catalog(
                    ok=True,
                    source="python-api",
                    version=data.get("version", "unknown"),
                    dialects=data.get("dialects", []),
                    rules=data.get("rules", []),
                    groups=data.get("groups", []),
                    fetched_at=time.time(),
                )
            return self._fallback(data.get("error", "探针失败"), data.get("hint"))
        return self._fallback(
            (res.stderr or res.stdout or "探针无输出").strip()[:500]
        )

    def _fallback(self, error: str, hint: str | None = None) -> Catalog:
        """退化为解析 CLI 文本输出。"""
        cat = Catalog(ok=False, source="cli-text", error=error, hint=hint, fetched_at=time.time())
        py = self._python_exe()
        try:
            d = runner.run([py, "-m", "sqlfluff", "dialects", "-n"], RunOptions(timeout=60.0))
            cat.dialects = _parse_dialects_text(d.stdout)
            r = runner.run([py, "-m", "sqlfluff", "rules", "-n"], RunOptions(timeout=60.0))
            cat.rules = _parse_rules_text(r.stdout)
            cat.groups = sorted({g for rule in cat.rules for g in rule.get("groups", [])})
            cat.ok = bool(cat.dialects and cat.rules)
            if cat.ok:
                cat.error = None
                cat.hint = hint or "目录来自 CLI 文本解析（含表格截断续行合并），可能不完整"
        except runner.RunnerError as exc:
            cat.error = f"{error} / CLI 兜底也失败: {exc}"
        return cat


def _parse_dialects_text(text: str) -> list[dict]:
    out: list[dict] = []
    prev: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.startswith("====") or line.startswith("sqlfluff"):
            continue
        match = _DIALECT_LINE.match(line)
        if match:
            label, rest = match.group(1), match.group(2)
            inherits = _INHERITS.search(rest)
            desc = _INHERITS.sub("", rest).strip()
            # CLI 写法是 "<描述> dialect [inherits from 'x']"，去掉尾部的类型词
            if desc.lower().endswith(" dialect"):
                desc = desc[: -len(" dialect")].strip()
            prev = {
                "label": label,
                "name": desc or label,
                "inherits_from": inherits.group(1) if inherits else "nothing",
            }
            out.append(prev)
            continue
        # 续行：方言名/描述被 80 列截断后换了行
        if prev is not None and line and not line.startswith(" "):
            chunk = line.strip()
            if chunk and len(prev["label"]) >= 8:
                prev["label"] += chunk
    return out


def _parse_rules_text(text: str) -> list[dict]:
    out: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("===="):
            continue
        match = _RULE_LINE.match(line)
        if match:
            current = {
                "code": match.group(1),
                "name": match.group(2),
                "description": match.group(3).strip(),
                "groups": [],
                "aliases": [],
            }
            out.append(current)
            continue
        if current is not None:
            stripped = line.strip()
            if stripped.startswith("groups:"):
                body = stripped[len("groups:") :]
                parts = [p.strip() for p in body.split("aliases:")[0].split(",")]
                current["groups"] = [p for p in parts if p]
                if "aliases:" in body:
                    aliases = [a.strip() for a in body.split("aliases:")[1].split(",")]
                    current["aliases"] = [a for a in aliases if a]
            elif stripped and not stripped.startswith("groups:"):
                # 描述续行
                if current["description"] and not stripped[0].isalpha():
                    continue
                if not current["groups"]:
                    current["description"] = (current["description"] + " " + stripped).strip()
    return out
