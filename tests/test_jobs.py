"""作业层测试：生命周期、取消、失败归类、进度、淘汰。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.jobs import Job, JobCancelled, JobManager


class TestJobLifecycle(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager(max_concurrent=2)

    def tearDown(self):
        self.manager.shutdown()

    def _wait(self, job_id, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.manager.get(job_id)
            if job and job.status not in ("pending", "running"):
                return job
            time.sleep(0.05)
        self.fail("作业未在预期时间内结束")

    def test_success_and_progress(self):
        def work(job: Job):
            job.tick(1, 3, "第一步")
            job.log("info", "hello")
            job.tick(3, 3, "完成")
            return {"value": 42}

        job = self.manager.submit("demo", {}, work)
        done = self._wait(job.id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.result["value"], 42)
        self.assertEqual(done.progress["done"], 3)
        self.assertEqual(done.progress["percent"], 100.0)
        self.assertTrue(any(e.message == "hello" for e in done.events))
        self.assertIsNotNone(done.started_at)
        self.assertIsNotNone(done.finished_at)

    def test_failure_captured(self):
        def work(job: Job):
            raise RuntimeError("boom")

        done = self._wait(self.manager.submit("demo", {}, work).id)
        self.assertEqual(done.status, "error")
        self.assertIn("boom", done.error)
        self.assertEqual(done.error_code, "INTERNAL")

    def test_timeout_status(self):
        def work(job: Job):
            raise TimeoutError("命令超时")

        done = self._wait(self.manager.submit("demo", {}, work).id)
        self.assertEqual(done.status, "timeout")
        self.assertEqual(done.error_code, "TIMEOUT")

    def test_cancel_running_job(self):
        def work(job: Job):
            for _ in range(200):
                time.sleep(0.02)
                job.check_cancelled()
            return {}

        job = self.manager.submit("demo", {}, work)
        time.sleep(0.3)
        self.assertTrue(self.manager.cancel(job.id))
        done = self._wait(job.id)
        self.assertEqual(done.status, "cancelled")
        self.assertEqual(done.error_code, "CANCELLED")

    def test_cancel_unknown(self):
        self.assertFalse(self.manager.cancel("nope"))

    def test_events_capped(self):
        def work(job: Job):
            for i in range(2000):
                job.log("info", f"line {i}")
            return {}

        done = self._wait(self.manager.submit("demo", {}, work).id)
        self.assertLessEqual(len(done.events), 500)

    def test_prune_keeps_bounded(self):
        from app import jobs as jobs_mod

        original = jobs_mod.MAX_JOBS_KEPT
        jobs_mod.MAX_JOBS_KEPT = 5
        try:
            ids = []
            for _ in range(12):
                job = self.manager.submit("demo", {}, lambda j: {})
                ids.append(job.id)
                self._wait(job.id)
            self.assertLessEqual(len(self.manager.list_recent(limit=100)), 5)
        finally:
            jobs_mod.MAX_JOBS_KEPT = original

    def test_shutdown_cancels_running(self):
        def work(job: Job):
            for _ in range(400):
                time.sleep(0.02)
                job.check_cancelled()
            return {}

        job = self.manager.submit("demo", {}, work)
        time.sleep(0.2)
        self.manager.shutdown()
        done = self._wait(job.id, timeout=10)
        self.assertIn(done.status, ("cancelled", "done"))

    def test_to_dict_shape(self):
        done = self._wait(self.manager.submit("demo", {}, lambda j: {"a": 1}).id)
        payload = done.to_dict()
        for key in ("id", "action", "status", "progress", "events", "duration"):
            self.assertIn(key, payload)
        self.assertNotIn("result", done.to_dict(include_result=False))


class TestJobCancelFlag(unittest.TestCase):
    def test_check_cancelled_raises(self):
        job = Job("x", {})
        job.cancel_flag.set()
        with self.assertRaises(JobCancelled):
            job.check_cancelled()


if __name__ == "__main__":
    unittest.main(verbosity=2)
