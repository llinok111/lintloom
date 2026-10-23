"""HTTP 层测试：真实起服务（端口 0）、真实请求、真实跑作业。"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server as server_mod
from support import TempProject


class ServerCase(TempProject):
    def setUp(self):
        super().setUp()
        self.server, self.app = server_mod.make_server(self.settings)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = self.app.token

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        super().tearDown()

    # ------------------------------------------------------------ helpers
    def request(self, path, method="GET", body=None, token=True, headers=None):
        url = self.base + path
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs["X-LintLoom-Token"] = self.token
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json_of(self, path, method="GET", body=None, **kw):
        status, raw = self.request(path, method, body, **kw)
        return status, json.loads(raw.decode("utf-8"))

    def run_job(self, payload, max_wait=120):
        status, data = self.json_of("/api/jobs", "POST", payload)
        self.assertEqual(status, 202, data)
        job_id = data["data"]["job_id"]
        deadline = time.time() + max_wait
        while time.time() < deadline:
            st, snap = self.json_of(f"/api/jobs/{job_id}")
            self.assertEqual(st, 200)
            if snap["data"]["status"] not in ("pending", "running"):
                return snap["data"]
            time.sleep(0.2)
        self.fail("作业超时未结束")


class TestStaticAndToken(ServerCase):
    def test_index_served_with_token(self):
        status, raw = self.request("/", token=False)
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn(self.token, html)
        self.assertIn("LintLoom", html)
        self.assertNotIn("%%TOKEN%%", html)

    def test_assets_served(self):
        for path in ("/assets/app.js", "/assets/styles.css"):
            status, raw = self.request(path, token=False)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(raw), 500)

    def test_api_requires_token(self):
        status, payload = self.json_of("/api/env", token=False)
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_wrong_token_rejected(self):
        status, raw = self.request("/api/env", token=False, headers={"X-LintLoom-Token": "deadbeef"})
        self.assertEqual(status, 403)

    def test_cross_origin_rejected(self):
        status, raw = self.request("/api/env", token=True, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_allowed(self):
        status, raw = self.request("/api/env", token=True, headers={"Origin": self.base})
        self.assertEqual(status, 200)

    def test_unknown_route(self):
        status, _ = self.request("/nope", token=False)
        self.assertEqual(status, 404)


class TestApiReads(ServerCase):
    def test_env(self):
        status, payload = self.json_of("/api/env")
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertTrue(data["catalog"]["ok"])
        self.assertGreater(len(data["catalog"]["dialects"]), 5)
        self.assertGreater(len(data["catalog"]["rules"]), 10)
        self.assertEqual(data["upstream"]["repo"], "sqlfluff/sqlfluff")

    def test_catalog(self):
        status, payload = self.json_of("/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["source"], "python-api")

    def test_tree_root(self):
        status, payload = self.json_of("/api/tree?root=samples&path=")
        self.assertEqual(status, 200)
        names = {e["name"] for e in payload["data"]["entries"]}
        self.assertIn("messy.sql", names)
        self.assertIn("sub", names)

    def test_tree_traversal_blocked(self):
        status, payload = self.json_of("/api/tree?root=samples&path=../")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "VALIDATION")

    def test_file_read(self):
        status, payload = self.json_of("/api/file?root=samples&path=messy.sql")
        self.assertEqual(status, 200)
        self.assertIn("select", payload["data"]["content"])
        self.assertEqual(len(payload["data"]["sha256"]), 64)

    def test_file_outside_blocked(self):
        status, payload = self.json_of("/api/file?root=samples&path=../../etc/passwd")
        self.assertEqual(status, 400)

    def test_unknown_root_blocked(self):
        status, payload = self.json_of("/api/tree?root=whatever&path=")
        self.assertEqual(status, 400)


class TestJobs(ServerCase):
    def test_lint_job_end_to_end(self):
        snap = self.run_job({
            "action": "lint", "root": "samples", "path": ".", "dialect": "ansi", "timeout": 90,
        })
        self.assertEqual(snap["status"], "done")
        result = snap["result"]
        self.assertEqual(result["action"], "lint")
        self.assertGreater(result["summary"]["violations"], 0)
        self.assertGreaterEqual(len(result["files"]), 3)
        # 进度必须是真实推进过的
        self.assertEqual(snap["progress"]["percent"], 100.0)
        self.assertTrue(any(e["level"] == "cmd" for e in snap["events"]))

    def test_job_records_real_argv(self):
        snap = self.run_job({
            "action": "lint", "root": "samples", "path": "clean.sql", "dialect": "ansi", "timeout": 90,
        })
        cmd_events = [e for e in snap["events"] if e["level"] == "cmd"]
        self.assertTrue(cmd_events)
        argv = cmd_events[0]["data"]["argv"]
        self.assertIn("sqlfluff", argv[:4])
        self.assertIn("lint", argv)

    def test_parse_job(self):
        snap = self.run_job({
            "action": "parse", "root": "samples", "path": "messy.sql", "dialect": "ansi", "timeout": 90,
        })
        self.assertEqual(snap["status"], "done")
        self.assertIn("file", snap["result"]["segments"])

    def test_lint_text_job(self):
        snap = self.run_job({
            "action": "lint_text", "text": "select a,b from t", "dialect": "ansi", "timeout": 90,
        })
        self.assertEqual(snap["status"], "done")
        self.assertGreater(snap["result"]["summary"]["violations"], 0)

    def test_fix_preview_then_apply(self):
        target = self.root / "messy.sql"
        original = target.read_text(encoding="utf-8")

        preview = self.run_job({
            "action": "fix_preview", "root": "samples", "path": "messy.sql",
            "dialect": "ansi", "timeout": 90,
        })
        self.assertEqual(preview["status"], "done")
        entry = preview["result"]["files"][0]
        self.assertTrue(entry["changed"])
        # 预览阶段绝对不能动源文件
        self.assertEqual(target.read_text(encoding="utf-8"), original)

        applied = self.run_job({
            "action": "fix_apply", "root": "samples", "path": "messy.sql",
            "dialect": "ansi", "timeout": 90, "confirm": True,
            "expected": {"messy.sql": entry["before_sha256"]},
        })
        self.assertEqual(applied["status"], "done")
        self.assertEqual(applied["result"]["summary"]["files_written"], 1)
        fixed = target.read_text(encoding="utf-8")
        self.assertNotEqual(fixed, original)
        self.assertIn("select\n", fixed.lower())
        # 备份内容必须等于写回前的原始内容
        backup = Path(applied["result"]["backup_dir"]) / "samples" / "messy.sql"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)

    def test_apply_without_confirm_rejected(self):
        status, payload = self.json_of("/api/jobs", "POST", {
            "action": "fix_apply", "root": "samples", "path": "messy.sql",
            "dialect": "ansi", "timeout": 60,
        })
        # confirm 缺省为 False -> 作业提交成功但执行期拒绝
        self.assertEqual(status, 202)
        job_id = payload["data"]["job_id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            st, snap = self.json_of(f"/api/jobs/{job_id}")
            if snap["data"]["status"] not in ("pending", "running"):
                break
            time.sleep(0.2)
        self.assertEqual(snap["data"]["status"], "error")
        self.assertEqual(snap["data"]["error_code"], "NOT_CONFIRMED")

    def test_bad_action_rejected(self):
        status, payload = self.json_of("/api/jobs", "POST", {"action": "rm_rf"})
        self.assertEqual(status, 400)

    def test_injection_in_dialect_rejected(self):
        status, payload = self.json_of("/api/jobs", "POST", {
            "action": "lint", "root": "samples", "path": "clean.sql",
            "dialect": "ansi; rm -rf /", "timeout": 60,
        })
        self.assertEqual(status, 400)

    def test_injection_in_path_rejected(self):
        for bad in ("..%2F..%2Fetc", "../../etc/passwd", "C:\\Windows\\win.ini", "a\x00.sql"):
            status, payload = self.json_of("/api/jobs", "POST", {
                "action": "lint", "root": "samples", "path": bad,
                "dialect": "ansi", "timeout": 60,
            })
            self.assertEqual(status, 400, bad)

    def test_wrong_content_type_rejected(self):
        status, raw = self.request(
            "/api/jobs", "POST", body=None,
            headers={"Content-Type": "text/plain", "Content-Length": "0"},
        )
        self.assertIn(status, (400, 411))

    def test_job_not_found(self):
        status, payload = self.json_of("/api/jobs/nonexistent")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
