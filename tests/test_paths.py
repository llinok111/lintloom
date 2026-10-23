"""路径与参数校验测试 —— 这是安全边界，必须逐条锁死。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, paths


def make_settings(tmp: Path) -> config.Settings:
    (tmp / "samples").mkdir(parents=True, exist_ok=True)
    (tmp / "samples" / "a.sql").write_text("select 1\n", encoding="utf-8")
    (tmp / "samples" / "sub").mkdir(exist_ok=True)
    (tmp / "samples" / "sub" / "b.sql").write_text("select 2\n", encoding="utf-8")
    (tmp / "outside.sql").write_text("select 3\n", encoding="utf-8")
    return config.Settings(roots={"samples": tmp / "samples"}, open_browser=False)


class TestCleanRelativePath(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(paths.clean_relative_path("a.sql"), "a.sql")
        self.assertEqual(paths.clean_relative_path("sub/b.sql"), "sub/b.sql")
        self.assertEqual(paths.clean_relative_path("sub\\b.sql"), "sub/b.sql")
        self.assertEqual(paths.clean_relative_path("./sub//b.sql"), "sub/b.sql")

    def test_rejects_traversal(self):
        for bad in ("../a.sql", "sub/../../a.sql", "..", "sub/.."):
            with self.assertRaises(paths.ValidationError):
                paths.clean_relative_path(bad)

    def test_rejects_absolute(self):
        for bad in ("/etc/passwd", "C:\\Windows\\win.ini", "c:/x.sql", "\\\\server\\share\\a.sql", "~/a.sql"):
            with self.assertRaises(paths.ValidationError):
                paths.clean_relative_path(bad)

    def test_rejects_control_chars(self):
        for bad in ("a\x00.sql", "a\n.sql", "a\tb.sql", "a\x1f.sql"):
            with self.assertRaises(paths.ValidationError):
                paths.clean_relative_path(bad)

    def test_rejects_windows_trailing_dot_space(self):
        # Windows 会静默去掉结尾空格/点，必须提前拒绝，否则校验路径 != 实际路径
        for bad in ("a.sql.", "sub./b.sql", "a.sql..", "sub/a.sql. "):
            with self.assertRaises(paths.ValidationError, msg=bad):
                paths.clean_relative_path(bad)

    def test_trailing_space_is_trimmed_not_used(self):
        # 整串首尾空格先被 strip，得到的就是真正要用的路径，不存在歧义
        self.assertEqual(paths.clean_relative_path("  a.sql  "), "a.sql")

    def test_rejects_long(self):
        with self.assertRaises(paths.ValidationError):
            paths.clean_relative_path("a" * 600)

    def test_allow_root(self):
        for ok in ("", ".", "/", "  "):
            self.assertEqual(paths.clean_relative_path(ok, allow_root=True), ".")
        # allow_root 也不能把 traversal 变成根
        with self.assertRaises(paths.ValidationError):
            paths.clean_relative_path("sub/..", allow_root=True)


class TestResolveUnderRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lintloom-paths-")
        self.tmp = Path(self._tmp)
        self.settings = make_settings(self.tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ok(self):
        label, resolved = paths.resolve_under_root(self.settings, "samples", "a.sql")
        self.assertEqual(label, "samples")
        self.assertTrue(str(resolved).endswith("a.sql"))

    def test_traversal_blocked(self):
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "samples", "../outside.sql")

    def test_unknown_root(self):
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "../samples", "a.sql")
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "nope", "a.sql")

    def test_extension_whitelist(self):
        (self.tmp / "samples" / "evil.exe").write_text("x", encoding="utf-8")
        with self.assertRaises(paths.ValidationError) as ctx:
            paths.resolve_under_root(self.settings, "samples", "evil.exe")
        self.assertIn("不支持的文件类型", str(ctx.exception))

    def test_kind_check(self):
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "samples", "sub", kind="file")
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "samples", "a.sql", kind="dir")

    def test_missing(self):
        with self.assertRaises(paths.ValidationError):
            paths.resolve_under_root(self.settings, "samples", "nope.sql")

    @unittest.skipUnless(hasattr(os, "symlink"), "需要 symlink 支持")
    def test_symlink_escape(self):
        link = self.tmp / "samples" / "link.sql"
        target = self.tmp / "outside.sql"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建 symlink（Windows 需要管理员/开发者模式）")
        with self.assertRaises(paths.ValidationError) as ctx:
            paths.resolve_under_root(self.settings, "samples", "link.sql")
        self.assertIn("越界", str(ctx.exception))

    def test_realpath_guard_blocks_escape_without_symlink_privileges(self):
        """不依赖 symlink 权限，直接验证 realpath + commonpath 这道闸门本身。

        Windows 非管理员无法创建 symlink，但 realpath 逃逸检查必须可测，
        所以这里替换 realpath 的返回值，模拟「链接指向根目录之外」。
        """
        from unittest import mock

        outside = str(self.tmp / "outside.sql")
        real_realpath = os.path.realpath

        def fake_realpath(p, *a, **kw):
            text = str(p)
            if text.endswith("a.sql"):
                return outside
            return real_realpath(p, *a, **kw)

        with mock.patch("os.path.realpath", side_effect=fake_realpath):
            with self.assertRaises(paths.ValidationError) as ctx:
                paths.resolve_under_root(self.settings, "samples", "a.sql")
        self.assertIn("越界", str(ctx.exception))


class TestDialectAndRules(unittest.TestCase):
    ALLOWED = {"ansi", "postgres", "mysql"}

    def test_dialect_ok(self):
        self.assertEqual(paths.validate_dialect("ansi", self.ALLOWED), "ansi")

    def test_dialect_rejects_unknown(self):
        with self.assertRaises(paths.ValidationError):
            paths.validate_dialect("notadialect", self.ALLOWED)

    def test_dialect_rejects_injection(self):
        payloads = [
            "ansi; rm -rf /",
            "ansi && curl http://evil",
            "$(whoami)",
            "`id`",
            "ansi\n--x",
            "--config=/tmp/evil",
            "-d",
            "ansi'",
            "ansi|nc",
            "a" * 40,
            "",
        ]
        for payload in payloads:
            with self.assertRaises(paths.ValidationError, msg=payload):
                paths.validate_dialect(payload, self.ALLOWED)

    def test_rules_ok(self):
        refs = {"LT01", "layout.spacing", "core", "L001"}
        self.assertEqual(paths.validate_rule_list("LT01,layout.spacing", refs, "rules"), ["LT01", "layout.spacing"])
        self.assertEqual(paths.validate_rule_list(["core"], refs, "rules"), ["core"])
        self.assertEqual(paths.validate_rule_list(None, refs, "rules"), [])

    def test_rules_reject_unknown(self):
        with self.assertRaises(paths.ValidationError):
            paths.validate_rule_list("ZZ999", {"LT01"}, "rules")

    def test_rules_reject_injection(self):
        refs = {"LT01"}
        payloads = [
            "LT01; rm -rf /",
            "LT01 --config=/evil",
            "$(id)",
            "../../etc/passwd",
            "LT01|nc",
            "LT01`id`",
            ["LT01", "LT02 && id"],
            [1, 2],
        ]
        for payload in payloads:
            with self.assertRaises(paths.ValidationError, msg=str(payload)):
                paths.validate_rule_list(payload, refs, "rules")

    def test_rules_dedupe(self):
        self.assertEqual(paths.validate_rule_list("LT01,LT01,LT01", {"LT01"}, "rules"), ["LT01"])


class TestScalarValidation(unittest.TestCase):
    def test_timeout(self):
        self.assertEqual(paths.validate_timeout(None), config.TIMEOUT_DEFAULT)
        self.assertEqual(paths.validate_timeout("30"), 30.0)
        for bad in (0, -1, 10**9, "abc", True, float("nan"), float("inf")):
            with self.assertRaises(paths.ValidationError, msg=str(bad)):
                paths.validate_timeout(bad, 300)

    def test_bool(self):
        self.assertTrue(paths.validate_bool(True))
        self.assertFalse(paths.validate_bool(False))
        self.assertTrue(paths.validate_bool(None, True))
        for bad in ("true", 1, [], {}):
            with self.assertRaises(paths.ValidationError):
                paths.validate_bool(bad)

    def test_int(self):
        self.assertEqual(paths.validate_int(None, 1, 10, "n", 3), 3)
        self.assertEqual(paths.validate_int("5", 1, 10, "n", 3), 5)
        self.assertEqual(paths.validate_int(3.0, 1, 10, "n", 3), 3)
        for bad in (0, 11, "x", True, 1.5, "1.5", [], None if False else {}):
            with self.assertRaises(paths.ValidationError, msg=str(bad)):
                paths.validate_int(bad, 1, 10, "n", 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
