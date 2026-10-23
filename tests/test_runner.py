"""执行器测试：无 shell、超时、进程树回收、输出截断、句柄清理。"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, runner
from app.runner import RunOptions, RunnerError, run

PY = sys.executable


class TestArgvContract(unittest.TestCase):
    def test_rejects_string(self):
        with self.assertRaises(RunnerError):
            run("echo hi")

    def test_rejects_empty(self):
        with self.assertRaises(RunnerError):
            run([])

    def test_rejects_non_string(self):
        with self.assertRaises(RunnerError):
            run([PY, "-c", "pass", 1])

    def test_rejects_nul(self):
        with self.assertRaises(RunnerError):
            run([PY, "-c", "pass\x00"])

    def test_missing_executable(self):
        with self.assertRaises(RunnerError):
            run(["definitely-not-a-real-binary-xyz"], RunOptions(timeout=5))


class TestNoShell(unittest.TestCase):
    """参数里的 shell 元字符必须原样传给子进程，而不是被解释。"""

    def test_metacharacters_are_literal(self):
        payload = 'a;b&&c|d$(whoami)`id`>out'
        res = run([PY, "-c", "import sys;print(sys.argv[1])", payload], RunOptions(timeout=20))
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), payload)

    def test_source_has_no_shell_true(self):
        app_dir = Path(__file__).resolve().parent.parent / "app"
        for path in app_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, f"{path.name} 出现了 shell=True")
            self.assertNotIn("os.system", text, f"{path.name} 出现了 os.system")
            self.assertNotIn("subprocess.getoutput", text, f"{path.name} 出现了 getoutput")


class TestTimeoutAndCleanup(unittest.TestCase):
    def test_timeout_is_reported(self):
        started = time.perf_counter()
        res = run([PY, "-c", "import time;time.sleep(20)"], RunOptions(timeout=1.0))
        elapsed = time.perf_counter() - started
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 12, "超时后未能及时回收")
        self.assertNotEqual(res.returncode, 0)

    def test_process_tree_is_killed(self):
        """子进程再生一个孙子进程；超时后孙子也不能活着写出 marker。"""
        import tempfile

        tmp = tempfile.mkdtemp(prefix="lintloom-kill-")
        marker = Path(tmp) / "grandchild.txt"
        script = (
            "import subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable,'-c',"
            f"'import time,pathlib;time.sleep(3);pathlib.Path(r\"{marker}\").write_text(\"alive\")'])\n"
            "time.sleep(30)\n"
        )
        res = run([PY, "-c", script], RunOptions(timeout=1.0))
        self.assertTrue(res.timed_out)
        time.sleep(4.5)
        self.assertFalse(marker.exists(), "孙子进程存活，说明只杀了直接子进程")

    def test_stdin_is_delivered(self):
        res = run(
            [PY, "-c", "import sys;print(sys.stdin.read().upper())"],
            RunOptions(timeout=20, stdin_text="hello lintloom"),
        )
        self.assertEqual(res.stdout.strip(), "HELLO LINTLOOM")

    def test_large_output_is_truncated(self):
        original = config.MAX_STREAM_BYTES
        config.MAX_STREAM_BYTES = 1000
        try:
            res = run([PY, "-c", "print('x'*50000)"], RunOptions(timeout=30))
        finally:
            config.MAX_STREAM_BYTES = original
        self.assertTrue(res.stdout_truncated)
        self.assertLessEqual(len(res.stdout), 1000)

    def test_stderr_captured_separately(self):
        res = run([PY, "-c", "import sys;sys.stderr.write('boom');print('ok')"], RunOptions(timeout=20))
        self.assertEqual(res.stdout.strip(), "ok")
        self.assertIn("boom", res.stderr)

    def test_utf8_round_trip(self):
        res = run([PY, "-c", "print('中文输出 ✓')"], RunOptions(timeout=20))
        self.assertIn("中文输出", res.stdout)

    def test_bounded_duration_recorded(self):
        res = run([PY, "-c", "pass"], RunOptions(timeout=20))
        self.assertGreaterEqual(res.duration, 0)
        self.assertLess(res.duration, 30)


class TestCancel(unittest.TestCase):
    def test_cancellable_process_kill(self):
        handle = runner.CancellableProcess()
        proc_box: dict = {}

        def spawn(proc):
            proc_box["p"] = proc
            handle.attach(proc)

        res_holder: dict = {}

        def worker():
            res_holder["r"] = run(
                [PY, "-c", "import time;time.sleep(20)"],
                RunOptions(timeout=60, on_spawn=spawn),
            )

        import threading

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(1.0)
        self.assertTrue(handle.cancel())
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "取消后执行线程没有退出")
        self.assertLess(res_holder["r"].duration, 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
