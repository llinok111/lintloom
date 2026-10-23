"""引擎集成测试：调用真实 sqlfluff，断言真实输出。"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths
from support import SAMPLE_SQL, TempProject


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestLint(TempProject):
    def test_lint_directory(self):
        job = self.new_job("lint")
        result = self.engine.action_lint(
            job,
            root="samples",
            path=".",
            dialect="ansi",
            rules=[],
            exclude=[],
            noqa=True,
            timeout=90,
        )
        summary = result["summary"]
        self.assertEqual(summary["files_scanned"], 4)
        self.assertGreater(summary["violations"], 0)
        self.assertIn("LT09", summary["codes"])
        self.assertIn("AL01", summary["codes"])
        # 每个文件都要有真实的上游计时
        for item in result["files"]:
            self.assertIn("timings", item)
            self.assertIn("parsing", item["timings"])
            self.assertGreater(item["timings"]["parsing"], 0)
        self.assertEqual(job.progress["done"], 4)
        self.assertEqual(job.progress["percent"], 100.0)

    def test_clean_file_has_no_violation(self):
        job = self.new_job("lint")
        result = self.engine.action_lint(
            job, root="samples", path="clean.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        self.assertEqual(result["summary"]["violations"], 0)
        self.assertEqual(result["summary"]["clean_files"], 1)

    def test_rule_filter_narrows_result(self):
        job = self.new_job("lint")
        wide = self.engine.action_lint(
            job, root="samples", path="messy.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        narrow = self.engine.action_lint(
            self.new_job("lint"), root="samples", path="messy.sql", dialect="ansi",
            rules=["LT01"], exclude=[], noqa=True, timeout=90,
        )
        self.assertLess(narrow["summary"]["violations"], wide["summary"]["violations"])
        self.assertEqual(set(narrow["summary"]["codes"]), {"LT01"})

    def test_parse_error_is_detected(self):
        job = self.new_job("lint")
        result = self.engine.action_lint(
            job, root="samples", path="broken.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        codes = set(result["summary"]["codes"])
        self.assertIn("PRS", codes)
        self.assertTrue(any(d["code"] == "PARSE" for d in result["diagnosis"]))

    def test_unknown_dialect_is_rejected_before_running(self):
        with self.assertRaises(paths.ValidationError):
            paths.validate_dialect("notadialect", {"ansi"})

    def test_expand_cap(self):
        self.settings.max_files = 2
        job = self.new_job("lint")
        files = self.engine.expand_target(job, "samples", ".")
        self.assertEqual(len(files), 2)
        self.assertTrue(any("上限" in e.message for e in job.events))


class TestFixPreview(TempProject):
    def test_preview_does_not_touch_disk(self):
        target = self.root / "messy.sql"
        before = sha(target)
        job = self.new_job("fix_preview")
        result = self.engine.action_fix_preview(
            job, root="samples", path="messy.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        self.assertEqual(sha(target), before, "预览把源文件改了")
        entry = result["files"][0]
        self.assertTrue(entry["changed"])
        self.assertIn("--- a/messy.sql", entry["diff"])
        self.assertIn("+++ b/messy.sql", entry["diff"])
        self.assertGreater(entry["remaining"], -1)
        self.assertLess(entry["remaining"], 14, "修复后剩余问题应显著减少")
        self.assertEqual(entry["before_sha256"], before)

    def test_preview_on_clean_file(self):
        job = self.new_job("fix_preview")
        result = self.engine.action_fix_preview(
            job, root="samples", path="clean.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        self.assertFalse(result["files"][0]["changed"])
        self.assertEqual(result["files"][0]["diff"], "")


class TestFixApply(TempProject):
    def test_apply_writes_and_backs_up(self):
        target = self.root / "messy.sql"
        original = target.read_text(encoding="utf-8")
        original_sha = sha(target)

        preview = self.engine.action_fix_preview(
            self.new_job("fix_preview"), root="samples", path="messy.sql",
            dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
        )
        expected = {preview["files"][0]["file"]: original_sha}

        job = self.new_job("fix_apply")
        result = self.engine.action_fix_apply(
            job, root="samples", path="messy.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
            confirm=True, expected=expected,
        )
        self.assertEqual(result["summary"]["files_written"], 1)
        new_text = target.read_text(encoding="utf-8")
        self.assertNotEqual(new_text, original)
        self.assertIn("select\n", new_text.lower())
        self.assertNotEqual(sha(target), original_sha)

        applied = result["applied"][0]
        backup = Path(result["backup_dir"]) / applied["root"] / applied["file"]
        self.assertTrue(backup.is_file(), f"备份文件不存在: {backup}")
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_apply_requires_confirm(self):
        from app.engine import EngineError

        with self.assertRaises(EngineError) as ctx:
            self.engine.action_fix_apply(
                self.new_job("fix_apply"), root="samples", path="messy.sql",
                dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
                confirm=False, expected={},
            )
        self.assertEqual(ctx.exception.code, "NOT_CONFIRMED")

    def test_apply_refuses_stale_sha(self):
        target = self.root / "messy.sql"
        preview = self.engine.action_fix_preview(
            self.new_job("fix_preview"), root="samples", path="messy.sql",
            dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
        )
        stale = {"messy.sql": "0" * 64}
        result = self.engine.action_fix_apply(
            self.new_job("fix_apply"), root="samples", path="messy.sql",
            dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
            confirm=True, expected=stale,
        )
        self.assertEqual(result["summary"]["files_written"], 0)
        self.assertEqual(result["summary"]["files_skipped"], 1)
        self.assertIn("sha256", result["skipped"][0]["reason"])
        self.assertEqual(sha(target), preview["files"][0]["before_sha256"])

    def test_read_only_mode_blocks_apply(self):
        from app.engine import EngineError

        self.settings.allow_apply = False
        with self.assertRaises(EngineError) as ctx:
            self.engine.action_fix_apply(
                self.new_job("fix_apply"), root="samples", path="messy.sql",
                dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
                confirm=True, expected={},
            )
        self.assertEqual(ctx.exception.code, "READ_ONLY")


class TestParse(TempProject):
    def test_parse_tree(self):
        job = self.new_job("parse")
        result = self.engine.action_parse(
            job, root="samples", path="messy.sql", dialect="ansi", timeout=90
        )
        root = result["segments"]
        self.assertIn("file", root)
        # sqlfluff 的 parse JSON 结构是 file -> statement -> select_statement
        self.assertIn("statement", root["file"])
        self.assertIn("select_statement", root["file"]["statement"])
        self.assertEqual(job.progress["done"], 2)


class TestBufferActions(TempProject):
    TEXT = "select a,b from t where x=1"

    def test_lint_text(self):
        job = self.new_job("lint_text")
        result = self.engine.action_lint_text(
            job, text=self.TEXT, dialect="ansi", rules=[], exclude=[],
            noqa=True, timeout=90,
        )
        codes = {v["code"] for v in result["violations"]}
        self.assertIn("LT09", codes)
        self.assertIn("LT12", codes)

    def test_fix_text(self):
        job = self.new_job("fix_text")
        result = self.engine.action_fix_text(
            job, text=self.TEXT, dialect="ansi", rules=[], exclude=[],
            noqa=True, timeout=90,
        )
        self.assertLess(result["summary"]["after"], result["summary"]["before"])
        self.assertIn("\n", result["fixed_text"])
        self.assertIn("+", result["diff"])

    def test_empty_buffer_rejected(self):
        from app.engine import EngineError

        with self.assertRaises(EngineError):
            self.engine.action_lint_text(
                self.new_job("lint_text"), text="   ", dialect="ansi",
                rules=[], exclude=[], noqa=True, timeout=90,
            )

    def test_oversized_buffer_rejected(self):
        from app import config
        from app.engine import EngineError

        with self.assertRaises(EngineError):
            self.engine.action_lint_text(
                self.new_job("lint_text"), text="a" * (config.MAX_BUFFER_CHARS + 1),
                dialect="ansi", rules=[], exclude=[], noqa=True, timeout=90,
            )


class TestDiagnosisFalsePositive(TempProject):
    def test_clean_run_has_no_template_warning(self):
        """stdout 里的 timings.templating 键名不能被误判成模板错误。"""
        job = self.new_job("lint")
        result = self.engine.action_lint(
            job, root="samples", path="clean.sql", dialect="ansi",
            rules=[], exclude=[], noqa=True, timeout=90,
        )
        self.assertEqual(result["diagnosis"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
