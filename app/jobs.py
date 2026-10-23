"""作业管理：进度、事件日志、取消、生命周期。

一次「点按钮」= 一个 Job。Job 在工作线程里跑，前端轮询 `to_dict()` 拿进度和事件，
所以进度是**真实**的（每跑完一个文件 tick 一次），不是假动画。
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import runner

MAX_EVENTS = 500
MAX_JOBS_KEPT = 60


class JobCancelled(Exception):
    """作业被用户取消。"""


@dataclass
class JobEvent:
    ts: float
    level: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "t": round(self.ts, 3),
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }


class Job:
    def __init__(self, action: str, params: dict) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.action = action
        self.params = params
        self.status = "pending"  # pending|running|done|error|cancelled|timeout
        self.progress = {"done": 0, "total": 0, "label": "", "percent": 0.0}
        self.events: deque[JobEvent] = deque(maxlen=MAX_EVENTS)
        self.result: dict | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel_flag = threading.Event()
        self.proc = runner.CancellableProcess()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 记录
    def log(self, level: str, message: str, **data: Any) -> None:
        with self._lock:
            self.events.append(
                JobEvent(ts=time.time() - self.created_at, level=level, message=message, data=data)
            )
        if level in ("error", "warn"):
            self.progress["label"] = message

    def tick(self, done: int, total: int, label: str = "") -> None:
        with self._lock:
            self.progress["done"] = done
            self.progress["total"] = total
            self.progress["percent"] = round(done / total * 100, 1) if total else 0.0
            if label:
                self.progress["label"] = label

    def attach(self, proc) -> None:
        self.proc.attach(proc)

    @property
    def cancelled(self) -> bool:
        return self.cancel_flag.is_set()

    def check_cancelled(self) -> None:
        if self.cancel_flag.is_set():
            raise JobCancelled()

    # ---------------------------------------------------------------- 序列化
    def to_dict(self, include_result: bool = True) -> dict:
        with self._lock:
            payload = {
                "id": self.id,
                "action": self.action,
                "status": self.status,
                "progress": dict(self.progress),
                "events": [e.as_dict() for e in self.events],
                "error": self.error,
                "error_code": self.error_code,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 3),
            }
            if include_result and self.result is not None:
                payload["result"] = self.result
        return payload


class JobManager:
    def __init__(self, max_concurrent: int = 2) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self._slot = threading.Semaphore(max_concurrent)
        self._shutdown = threading.Event()

    def submit(self, action: str, params: dict, fn: Callable[[Job], dict]) -> Job:
        job = Job(action, params)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        thread = threading.Thread(
            target=self._runner, args=(job, fn), name=f"job-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _runner(self, job: Job, fn: Callable[[Job], dict]) -> None:
        if self._shutdown.is_set():
            job.status = "cancelled"
            job.error = "服务正在关闭"
            return
        with self._slot:  # 限制并发，避免把 CPU 打满
            job.status = "running"
            job.started_at = time.time()
            job.log("info", f"作业启动：{job.action}")
            try:
                job.result = fn(job) or {}
                job.check_cancelled()
                job.status = "done"
                job.log("info", "作业完成")
            except JobCancelled:
                job.status = "cancelled"
                job.error = "已取消"
                job.error_code = "CANCELLED"
                job.log("warn", "作业被取消")
            except TimeoutError as exc:
                job.status = "timeout"
                job.error = str(exc)
                job.error_code = "TIMEOUT"
                job.log("error", f"超时：{exc}")
            except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死亡
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.error_code = getattr(exc, "code", "INTERNAL")
                job.log("error", job.error)
                job.log("trace", traceback.format_exc()[-2000:])
            finally:
                job.finished_at = time.time()
                job.progress["percent"] = (
                    100.0 if job.status in ("done",) else job.progress.get("percent", 0.0)
                )
                job.proc.cancel() if job.cancel_flag.is_set() else None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel_flag.set()
        job.log("warn", "收到取消请求，正在终止子进程")
        job.proc.cancel()
        return True

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = list(self._order)[-limit:]
            return [self._jobs[i].to_dict(include_result=False) for i in ids if i in self._jobs]

    def _prune_locked(self) -> None:
        while len(self._order) > MAX_JOBS_KEPT:
            old = self._order.popleft()
            job = self._jobs.get(old)
            if job and job.status in ("pending", "running"):
                self._order.append(old)  # 运行中的不淘汰，放到队尾再试
                break
            self._jobs.pop(old, None)

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status in ("pending", "running"):
                job.cancel_flag.set()
                job.proc.cancel()
