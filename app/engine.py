"""业务编排层：把「界面上的一个动作」翻译成一次或多次真实的 sqlfluff 调用。

这里不实现任何 SQL 分析逻辑 —— 分析全部由上游 CLI 完成，本层只负责：
参数拼装（argv 数组）、结果解析、跨调用编排（修复后再复核）、差分、诊断映射。

安全边界：本层收到的参数**已经**过 `paths.py` 校验；本层不再接受任何字符串拼接命令。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import catalog as catalog_mod
from . import config, paths, runner
from .jobs import Job, JobCancelled

FIX_SUFFIX = "_lintloom_fix"
JSON_TAIL_CHARS = 4000


class EngineError(Exception):
    def __init__(self, message: str, code: str = "ENGINE") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FileRef:
    root: str
    rel: str
    abs: Path

    @property
    def size(self) -> int:
        try:
            return self.abs.stat().st_size
        except OSError:
            return 0


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > config.MAX_FILE_BYTES:
        raise EngineError(f"文件过大（>{config.MAX_FILE_BYTES // 1024 // 1024}MB）：{path.name}", "FILE_TOO_LARGE")
    return data.decode("utf-8", errors="replace")


class Engine:
    def __init__(self, settings: config.Settings, catalog: catalog_mod.CatalogService) -> None:
        self.settings = settings
        self.catalog = catalog

    # ================================================================ 基础
    def _argv(self, *args: str) -> list[str]:
        return [self.settings.python_exe, "-m", "sqlfluff", *args]

    def _run(
        self,
        job: Job,
        args: list[str],
        *,
        timeout: float,
        stdin_text: str | None = None,
    ) -> runner.ProcResult:
        job.check_cancelled()
        argv = self._argv(*args)
        job.log("cmd", runner.which_display(argv), argv=list(argv)[1:])
        res = runner.run(
            argv,
            runner.RunOptions(
                cwd=config.PROJECT_ROOT,
                timeout=timeout,
                stdin_text=stdin_text,
                on_spawn=job.attach,
            ),
        )
        job.log(
            "proc",
            f"退出码 {res.returncode} · {res.duration:.2f}s",
            **res.as_log(),
        )
        if res.stdout.strip():
            job.log("stdout", res.stdout[:JSON_TAIL_CHARS])
        if res.stderr.strip():
            job.log("stderr", res.stderr[:JSON_TAIL_CHARS])
        if res.timed_out:
            job.log("error", f"命令超时（{timeout}s），已终止进程树")
        return res

    def _rule_args(self, rules: list[str], exclude: list[str]) -> list[str]:
        args: list[str] = []
        if rules:
            args += ["-r", ",".join(rules)]
        if exclude:
            args += ["-e", ",".join(exclude)]
        return args

    def _json_from(self, text: str):
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    return None
            return None

    # ================================================================ 目标展开
    def expand_target(self, job: Job, root: str, rel: str) -> list[FileRef]:
        _, base = paths.resolve_under_root(
            self.settings, root, rel, must_exist=True, kind="any", allow_root=True
        )
        if base.is_file():
            _, checked = paths.resolve_under_root(self.settings, root, rel, must_exist=True, kind="file")
            return [FileRef(root, rel, checked)]

        found: list[FileRef] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in config.SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if Path(name).suffix.lower() not in config.ALLOWED_EXT:
                    continue
                abs_path = Path(dirpath) / name
                rel_path = abs_path.relative_to(self.settings.normalised_roots()[root]).as_posix()
                try:
                    _, checked = paths.resolve_under_root(
                        self.settings, root, rel_path, must_exist=True, kind="file"
                    )
                except paths.ValidationError as exc:
                    job.log("warn", f"跳过 {rel_path}：{exc}")
                    continue
                found.append(FileRef(root, rel_path, checked))
                if len(found) >= self.settings.max_files:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            job.log(
                "warn",
                f"目标文件数超过上限 {self.settings.max_files}，其余文件未纳入本次作业",
            )
        if not found:
            raise EngineError("目标目录下没有可处理的 SQL 文件（.sql / .jinja / .j2）", "NO_FILES")
        return found

    # ================================================================ 环境自检
    def probe_env(self, job: Job | None = None) -> dict:
        cat = self.catalog.get()
        cli_version = None
        cli_error = None
        try:
            res = runner.run(self._argv("--version"), runner.RunOptions(timeout=60.0))
            cli_version = res.stdout.strip() or res.stderr.strip()
        except runner.RunnerError as exc:
            cli_error = str(exc)

        roots = []
        for label, path in self.settings.normalised_roots().items():
            writable = os.access(str(path), os.W_OK)
            count = sum(
                1
                for _, _, files in os.walk(path)
                for f in files
                if Path(f).suffix.lower() in config.ALLOWED_EXT
            )
            roots.append(
                {
                    "label": label,
                    "path": str(path),
                    "writable": writable,
                    "sql_files": count,
                }
            )

        checks = [
            {
                "name": "解释器",
                "ok": True,
                "detail": self.settings.python_exe,
            },
            {
                "name": "sqlfluff CLI",
                "ok": bool(cli_version) and "version" in (cli_version or ""),
                "detail": cli_version or cli_error or "无法执行",
            },
            {
                "name": "能力目录（方言/规则）",
                "ok": cat.ok,
                "detail": f"{len(cat.dialects)} 个方言 / {len(cat.rules)} 条规则 · 来源 {cat.source}",
            },
            {
                "name": "写回开关",
                "ok": True,
                "detail": "允许应用修复" if self.settings.allow_apply else "只读模式（--read-only）",
            },
        ]
        return {
            "app": {"name": config.APP_NAME, "version": config.APP_VERSION},
            "upstream": {
                "repo": config.UPSTREAM_REPO,
                "pinned": config.UPSTREAM_PINNED,
                "version": cat.version,
                "cli": cli_version,
            },
            "python": {"exe": self.settings.python_exe, "version": _py_version()},
            "catalog": cat.as_dict(),
            "roots": roots,
            "checks": checks,
            "limits": {
                "timeout_max": self.settings.timeout_cap,
                "max_files": self.settings.max_files,
                "max_file_bytes": config.MAX_FILE_BYTES,
                "allowed_ext": sorted(config.ALLOWED_EXT),
            },
        }

    # ================================================================ 动作：lint
    def action_lint(
        self,
        job: Job,
        *,
        root: str,
        path: str,
        dialect: str,
        rules: list[str],
        exclude: list[str],
        noqa: bool,
        timeout: float,
    ) -> dict:
        started = time.perf_counter()
        files = self.expand_target(job, root, path)
        job.tick(0, len(files), "准备诊断")
        job.log("info", f"目标 {len(files)} 个文件 · 方言 {dialect}")

        results = []
        all_codes: set[str] = set()
        diagnosis: list[dict] = []
        total_violations = 0
        total_fixable = 0

        for index, ref in enumerate(files, 1):
            job.check_cancelled()
            args = ["lint", str(ref.abs), "-n", "-d", dialect, "-f", "json"]
            args += self._rule_args(rules, exclude)
            if not noqa:
                args.append("--disable-noqa")
            res = self._run(job, args, timeout=timeout)
            parsed = self._json_from(res.stdout)

            violations: list[dict] = []
            stats: dict = {}
            timings: dict = {}
            if isinstance(parsed, list) and parsed:
                entry = parsed[0]
                violations = entry.get("violations") or []
                stats = entry.get("statistics") or {}
                timings = entry.get("timings") or {}
            for violation in violations:
                all_codes.add(violation.get("code", ""))
                if violation.get("fixes"):
                    total_fixable += 1

            diagnosis.extend(self._diagnose(res, all_codes=set(v.get("code", "") for v in violations)))

            total_violations += len(violations)
            results.append(
                {
                    "file": ref.rel,
                    "root": ref.root,
                    "size": ref.size,
                    "sha256": sha256_of(ref.abs),
                    "violations": violations,
                    "statistics": stats,
                    "timings": {k: round(v, 6) for k, v in timings.items()},
                    "exit_code": res.returncode,
                    "duration": round(res.duration, 3),
                    "error": None if res.returncode in (0, 1) else (res.stderr or "").strip()[:800],
                }
            )
            job.tick(index, len(files), f"{ref.rel} · {len(violations)} 项")

        summary = self._summarise(results)
        return {
            "action": "lint",
            "files": results,
            "summary": {
                **summary,
                "violations": total_violations,
                "fixable": total_fixable,
                "files_scanned": len(files),
                "duration": round(time.perf_counter() - started, 3),
                "codes": sorted(c for c in all_codes if c),
            },
            "diagnosis": _dedupe_diagnosis(diagnosis),
        }

    # ============================================================ 动作：修复预览
    def action_fix_preview(
        self,
        job: Job,
        *,
        root: str,
        path: str,
        dialect: str,
        rules: list[str],
        exclude: list[str],
        noqa: bool,
        timeout: float,
    ) -> dict:
        started = time.perf_counter()
        files = self.expand_target(job, root, path)
        job.tick(0, len(files), "准备修复预览")
        job.log("info", "修复预览在临时副本上执行，绝不会改动源文件")

        work = Path(tempfile.mkdtemp(prefix="lintloom-preview-", dir=_work_root(self.settings)))
        out_files = []
        diagnosis: list[dict] = []
        changed_count = 0
        try:
            for index, ref in enumerate(files, 1):
                job.check_cancelled()
                original_text = _read_text(ref.abs)
                original_sha = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
                tmp_dir = work / f"{index:03d}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp_file = tmp_dir / ref.abs.name
                shutil.copy2(ref.abs, tmp_file)

                args = ["fix", str(tmp_file), "-n", "-d", dialect, "-x", FIX_SUFFIX]
                args += self._rule_args(rules, exclude)
                if not noqa:
                    args.append("--disable-noqa")
                res = self._run(job, args, timeout=timeout)
                diagnosis.extend(self._diagnose(res, all_codes=set()))

                fixed_path = tmp_file.with_name(tmp_file.stem + FIX_SUFFIX + tmp_file.suffix)
                fixed_text = ""
                if fixed_path.exists():
                    fixed_text = fixed_path.read_text(encoding="utf-8", errors="replace")
                changed = bool(fixed_text) and fixed_text != original_text
                if changed:
                    changed_count += 1

                diff = ""
                if changed:
                    diff = "".join(
                        difflib.unified_diff(
                            original_text.splitlines(keepends=True),
                            fixed_text.splitlines(keepends=True),
                            fromfile=f"a/{ref.rel}",
                            tofile=f"b/{ref.rel}",
                            n=3,
                        )
                    )

                # 真实闭环：把修复后的内容再喂回 lint（stdin），确认剩下什么
                remaining_codes: list[str] = []
                remaining = 0
                if changed:
                    after = self._run(
                        job,
                        ["lint", "-", "-n", "-d", dialect, "-f", "json", "--stdin-filename", str(ref.abs)]
                        + self._rule_args(rules, exclude),
                        timeout=timeout,
                        stdin_text=fixed_text,
                    )
                    parsed_after = self._json_from(after.stdout)
                    if isinstance(parsed_after, list) and parsed_after:
                        rem = parsed_after[0].get("violations") or []
                        remaining = len(rem)
                        remaining_codes = sorted({v.get("code", "") for v in rem if v.get("code")})

                out_files.append(
                    {
                        "file": ref.rel,
                        "root": ref.root,
                        "changed": changed,
                        "diff": diff,
                        "before_sha256": original_sha,
                        "before_bytes": len(original_text.encode("utf-8")),
                        "after_bytes": len(fixed_text.encode("utf-8")) if fixed_text else len(original_text.encode("utf-8")),
                        "remaining": remaining,
                        "remaining_codes": remaining_codes,
                        "exit_code": res.returncode,
                        "duration": round(res.duration, 3),
                        "note": "" if changed else "无自动可修复项",
                    }
                )
                job.tick(index, len(files), f"{ref.rel} · {'已改写' if changed else '无需改写'}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

        added = sum(f["diff"].count("\n+") - f["diff"].count("\n+++") for f in out_files if f["diff"])
        removed = sum(f["diff"].count("\n-") - f["diff"].count("\n---") for f in out_files if f["diff"])
        return {
            "action": "fix_preview",
            "files": out_files,
            "summary": {
                "files_scanned": len(files),
                "files_changed": changed_count,
                "lines_added": max(added, 0),
                "lines_removed": max(removed, 0),
                "duration": round(time.perf_counter() - started, 3),
            },
            "diagnosis": _dedupe_diagnosis(diagnosis),
        }

    # ============================================================ 动作：应用修复
    def action_fix_apply(
        self,
        job: Job,
        *,
        root: str,
        path: str,
        dialect: str,
        rules: list[str],
        exclude: list[str],
        noqa: bool,
        timeout: float,
        confirm: bool,
        expected: dict[str, str],
    ) -> dict:
        if not confirm:
            raise EngineError("应用修复必须显式确认（confirm=true）", "NOT_CONFIRMED")
        if not self.settings.allow_apply:
            raise EngineError("服务以只读模式启动，禁止写回磁盘", "READ_ONLY")

        started = time.perf_counter()
        files = self.expand_target(job, root, path)
        job.tick(0, len(files), "准备写回")
        job.log("warn", f"即将写回 {len(files)} 个文件，原件会先备份到 .lintloom-backups/")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_root = self.settings.backup_dir / stamp
        applied: list[dict] = []
        skipped: list[dict] = []
        diagnosis: list[dict] = []
        work = Path(tempfile.mkdtemp(prefix="lintloom-apply-", dir=_work_root(self.settings)))
        try:
            for index, ref in enumerate(files, 1):
                job.check_cancelled()
                current_sha = sha256_of(ref.abs)
                want = expected.get(ref.rel) if isinstance(expected, dict) else None
                if want and want != current_sha:
                    skipped.append(
                        {
                            "file": ref.rel,
                            "reason": "文件在预览之后被修改（sha256 不一致），已跳过以免覆盖他人改动",
                        }
                    )
                    job.log("warn", f"跳过 {ref.rel}：内容已变化")
                    job.tick(index, len(files), f"{ref.rel} · 跳过")
                    continue

                original_text = _read_text(ref.abs)
                tmp_dir = work / f"{index:03d}"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                tmp_file = tmp_dir / ref.abs.name
                shutil.copy2(ref.abs, tmp_file)

                args = ["fix", str(tmp_file), "-n", "-d", dialect, "-x", FIX_SUFFIX]
                args += self._rule_args(rules, exclude)
                if not noqa:
                    args.append("--disable-noqa")
                res = self._run(job, args, timeout=timeout)
                diagnosis.extend(self._diagnose(res, all_codes=set()))

                fixed_path = tmp_file.with_name(tmp_file.stem + FIX_SUFFIX + tmp_file.suffix)
                if not fixed_path.exists() or fixed_path.read_text(encoding="utf-8", errors="replace") == original_text:
                    skipped.append({"file": ref.rel, "reason": "无自动可修复项"})
                    job.tick(index, len(files), f"{ref.rel} · 无需改动")
                    continue

                backup_path = backup_root / ref.root / ref.rel
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ref.abs, backup_path)

                _atomic_write(ref.abs, fixed_path.read_bytes())
                new_sha = sha256_of(ref.abs)

                # 写回后立即复核，确认文件仍可解析
                after = self._run(
                    job,
                    ["lint", str(ref.abs), "-n", "-d", dialect, "-f", "json"]
                    + self._rule_args(rules, exclude),
                    timeout=timeout,
                )
                parsed_after = self._json_from(after.stdout)
                remaining = 0
                if isinstance(parsed_after, list) and parsed_after:
                    remaining = len(parsed_after[0].get("violations") or [])
                diagnosis.extend(self._diagnose(after, all_codes=set()))

                applied.append(
                    {
                        "file": ref.rel,
                        "root": ref.root,
                        "sha_before": current_sha,
                        "sha_after": new_sha,
                        "backup": str(backup_path.relative_to(config.PROJECT_ROOT)) if _is_within(backup_path, config.PROJECT_ROOT) else str(backup_path),
                        "remaining": remaining,
                    }
                )
                job.log("info", f"已写回 {ref.rel}（剩余 {remaining} 项）")
                job.tick(index, len(files), f"{ref.rel} · 已写回")
        finally:
            shutil.rmtree(work, ignore_errors=True)

        return {
            "action": "fix_apply",
            "applied": applied,
            "skipped": skipped,
            "backup_dir": str(backup_root) if applied else None,
            "summary": {
                "files_scanned": len(files),
                "files_written": len(applied),
                "files_skipped": len(skipped),
                "duration": round(time.perf_counter() - started, 3),
            },
            "diagnosis": _dedupe_diagnosis(diagnosis),
        }

    # ============================================================ 动作：语法树
    def action_parse(
        self,
        job: Job,
        *,
        root: str,
        path: str,
        dialect: str,
        timeout: float,
    ) -> dict:
        _, abs_path = paths.resolve_under_root(self.settings, root, path, must_exist=True, kind="file")
        job.tick(0, 2, "解析中")
        res = self._run(job, ["parse", str(abs_path), "-n", "-d", dialect, "-f", "json"], timeout=timeout)
        parsed = self._json_from(res.stdout)
        job.tick(1, 2, "解析完成")
        if not isinstance(parsed, list) or not parsed:
            raise EngineError(
                (res.stderr or res.stdout or "解析无输出").strip()[:400], "PARSE_FAILED"
            )
        entry = parsed[0]
        job.tick(2, 2, "完成")
        return {
            "action": "parse",
            "file": path,
            "root": root,
            "segments": entry.get("segments") or {},
            "exit_code": res.returncode,
            "duration": round(res.duration, 3),
            "diagnosis": _dedupe_diagnosis(self._diagnose(res, all_codes=set())),
        }

    # ============================================================ 动作：缓冲区
    def action_lint_text(
        self,
        job: Job,
        *,
        text: str,
        dialect: str,
        rules: list[str],
        exclude: list[str],
        noqa: bool,
        timeout: float,
    ) -> dict:
        _check_buffer(text)
        job.tick(0, 1, "诊断缓冲区")
        args = ["lint", "-", "-n", "-d", dialect, "-f", "json"] + self._rule_args(rules, exclude)
        if not noqa:
            args.append("--disable-noqa")
        res = self._run(job, args, timeout=timeout, stdin_text=text)
        parsed = self._json_from(res.stdout)
        violations = []
        if isinstance(parsed, list) and parsed:
            violations = parsed[0].get("violations") or []
        job.tick(1, 1, f"完成 · {len(violations)} 项")
        return {
            "action": "lint_text",
            "violations": violations,
            "summary": {
                "violations": len(violations),
                "fixable": sum(1 for v in violations if v.get("fixes")),
                "duration": round(res.duration, 3),
            },
            "diagnosis": _dedupe_diagnosis(
                self._diagnose(res, all_codes={v.get("code", "") for v in violations})
            ),
        }

    def action_fix_text(
        self,
        job: Job,
        *,
        text: str,
        dialect: str,
        rules: list[str],
        exclude: list[str],
        noqa: bool,
        timeout: float,
    ) -> dict:
        _check_buffer(text)
        job.tick(0, 2, "修复缓冲区")
        base = ["-n", "-d", dialect] + self._rule_args(rules, exclude)
        if not noqa:
            base.append("--disable-noqa")

        before = self._run(
            job, ["lint", "-", "-f", "json"] + base, timeout=timeout, stdin_text=text
        )
        fixed = self._run(job, ["fix", "-"] + base, timeout=timeout, stdin_text=text)
        if fixed.returncode not in (0, 1):
            raise EngineError((fixed.stderr or "修复失败").strip()[:400], "FIX_FAILED")

        fixed_text = fixed.stdout
        after = self._run(
            job, ["lint", "-", "-f", "json"] + base, timeout=timeout, stdin_text=fixed_text
        )
        before_v = self._violations_of(before.stdout)
        after_v = self._violations_of(after.stdout)
        job.tick(2, 2, f"完成 · {len(before_v)} → {len(after_v)}")
        diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                fixed_text.splitlines(keepends=True),
                fromfile="a/editor_buffer.sql",
                tofile="b/editor_buffer.sql",
                n=3,
            )
        )
        return {
            "action": "fix_text",
            "before": before_v,
            "after": after_v,
            "fixed_text": fixed_text,
            "diff": diff,
            "summary": {
                "before": len(before_v),
                "after": len(after_v),
                "fixable": sum(1 for v in before_v if v.get("fixes")),
                "duration": round(fixed.duration + before.duration + after.duration, 3),
            },
            "diagnosis": _dedupe_diagnosis(
                self._diagnose(before, all_codes={v.get("code", "") for v in before_v})
            ),
        }

    # ================================================================ 辅助
    def _violations_of(self, stdout: str) -> list[dict]:
        parsed = self._json_from(stdout)
        if isinstance(parsed, list) and parsed:
            return parsed[0].get("violations") or []
        return []

    def _summarise(self, results: list[dict]) -> dict:
        by_code: dict[str, int] = {}
        by_file: dict[str, int] = {}
        for item in results:
            by_file[item["file"]] = len(item["violations"])
            for violation in item["violations"]:
                code = violation.get("code") or "?"
                by_code[code] = by_code.get(code, 0) + 1
        return {
            "by_code": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
            "by_file": by_file,
            "clean_files": sum(1 for v in by_file.values() if v == 0),
        }

    def _diagnose(self, res: runner.ProcResult, *, all_codes: set[str]) -> list[dict]:
        out: list[dict] = []
        stderr = res.stderr or ""
        stdout = res.stdout or ""

        if res.timed_out:
            out.append(
                {
                    "severity": "error",
                    "code": "TIMEOUT",
                    "message": "命令在超时限制内未结束，进程树已被强制回收",
                    "hint": "缩小目标范围、减少规则数，或在左侧「超时」里提高上限（最高 300 秒）",
                    "detail": runner.which_display(res.argv),
                }
            )
            return out
        if res.cancelled:
            return out

        # 只在 stderr 里找错误特征。stdout 在 -f json 下是结构化数据，
        # 里面本来就有 "templating" 这类键名，用 stdout 匹配会误报模板错误。
        patterns = [
            ("DIALECT", re.compile(r"Unknown dialect '([^']+)'"), "方言不存在", "检查左上方言下拉；该清单由 sqlfluff 运行时提供，不要手输。"),
            ("PATH", re.compile(r"Specified path does not exist"), "目标路径不存在", "刷新左侧文件树后重新选择目标。"),
            ("ENV", re.compile(r"No module named '?sqlfluff'?|ModuleNotFoundError"), "当前解释器没有安装 sqlfluff", "运行 安装.cmd，或在启动参数里用 --python 指定已装 sqlfluff 的解释器。"),
            ("RULES", re.compile(r"allowlist unknown rule references: (\[[^\]]*\])"), "存在上游不认识的规则引用（已忽略）", "从右侧规则面板重新勾选，避免手输旧版规则码（L001 这类别名已废弃）。"),
            ("PERM", re.compile(r"Permission denied|Access is denied|WinError 5"), "文件或目录权限不足", "关闭占用该文件的程序，或换到 workspace 目录下的副本。"),
            ("TEMPLATE", re.compile(r"UndefinedError|jinja2\.exceptions|TemplatingException|Undefined jinja|dbt templater"), "模板渲染失败（Jinja/dbt 变量未定义）", "改用 ansi 方言先看语法，或为目标目录的 .sqlfluff 补上 templater 变量。"),
        ]
        for code, pattern, message, hint in patterns:
            match = pattern.search(stderr)
            if match:
                out.append(
                    {
                        "severity": "error" if code in ("DIALECT", "PATH", "ENV", "PERM") else "warn",
                        "code": code,
                        "message": message,
                        "hint": hint,
                        "detail": match.group(0)[:300],
                    }
                )

        if "PRS" in all_codes:
            out.append(
                {
                    "severity": "warn",
                    "code": "PARSE",
                    "message": "存在无法解析的 SQL 片段（PRS）",
                    "hint": "这类问题不会被自动修复：先修语法，或用 --ignore parsing 让其余规则继续工作。",
                    "detail": "violations 中出现 PRS 码",
                }
            )
        if res.returncode == 2 and not out:
            out.append(
                {
                    "severity": "error",
                    "code": "UPSTREAM_ERROR",
                    "message": "sqlfluff 以退出码 2 结束（执行期错误）",
                    "hint": "查看下方「运行日志」里的 stderr 原文。",
                    "detail": (stderr or stdout).strip()[:400],
                }
            )
        if res.returncode not in (0, 1, 2) and not out:
            out.append(
                {
                    "severity": "error",
                    "code": "CRASH",
                    "message": f"sqlfluff 异常退出（退出码 {res.returncode}）",
                    "hint": "可能是解释器环境损坏或内存不足，先跑「环境自检」。",
                    "detail": (stderr or stdout).strip()[:400],
                }
            )
        return out


# ==================================================================== 工具函数
def _py_version() -> str:
    import platform

    return platform.python_version()


def _work_root(settings: config.Settings) -> str:
    """临时目录根。放在可配置目录下，随作业结束整棵删除。"""
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return str(settings.work_dir)


def _check_buffer(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise EngineError("缓冲区内容为空", "EMPTY_BUFFER")
    if len(text) > config.MAX_BUFFER_CHARS:
        raise EngineError(f"缓冲区内容超过 {config.MAX_BUFFER_CHARS} 字符上限", "BUFFER_TOO_LARGE")


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".lintloom-write-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except OSError:
        # 跨卷时 os.replace 会失败，退化直写（备份已在调用方完成）
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        target.write_bytes(data)


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([str(path), str(root)])) == os.path.normcase(str(root))
    except ValueError:
        return False


def _dedupe_diagnosis(items: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    order = {"error": 0, "warn": 1, "info": 2}
    for item in sorted(items, key=lambda i: order.get(i.get("severity", "info"), 3)):
        key = (item.get("code"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
