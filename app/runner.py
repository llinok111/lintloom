"""受控子进程执行器。

唯一的进程创建入口。所有对外部命令的调用都必须经过 `run()`，以统一保证：

1. **无 shell**：`argv` 必须是字符串列表，`shell=False` 是硬编码的。参数原样传给
   execve/CreateProcess，不会被 `& | > ; $()` 解释 —— 这是防命令注入的第一道墙。
2. **超时**：到点杀掉整个进程树，结果是 `timed_out=True` 而不是异常。
3. **资源清理**：无论正常结束、超时还是被取消，管道、临时句柄都会关闭；
   进程树用 `taskkill /T`（Windows）或 `killpg`（POSIX）整体回收，避免孤儿进程。
4. **输出有界**：stdout/stderr 超过上限即截断并打标记，防止 8MB 的日志把内存打爆。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import config

IS_WINDOWS = os.name == "nt"

# Windows 下不弹控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

    def as_log(self) -> dict:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
        }


@dataclass
class RunOptions:
    cwd: os.PathLike | str | None = None
    timeout: float = config.TIMEOUT_DEFAULT
    stdin_text: str | None = None
    env_extra: dict[str, str] = field(default_factory=dict)
    on_spawn: Callable[[subprocess.Popen], None] | None = None


def _decode(data: bytes) -> tuple[str, bool]:
    text = data.decode("utf-8", errors="replace")
    if len(text) > config.MAX_STREAM_BYTES:
        return text[: config.MAX_STREAM_BYTES], True
    return text, False


def _base_env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    # 固定编码，避免中文 Windows 上 cp936/utf-8 混乱导致 JSON 解析失败
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NO_COLOR"] = "1"
    # 不让子进程继承可能劫持行为的启动钩子
    env.pop("PYTHONSTARTUP", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    """回收进程树。Windows 用 taskkill /T，POSIX 用 killpg。"""
    if proc.poll() is not None:
        return
    pid = proc.pid
    if IS_WINDOWS:
        taskkill = shutil.which("taskkill") or "taskkill"
        try:
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run(argv: Sequence[str], options: RunOptions | None = None) -> ProcResult:
    """执行一条外部命令。argv 必须是非空字符串序列。"""
    if isinstance(argv, (str, bytes)):
        raise RunnerError("argv 必须是字符串列表（禁止 shell 字符串）")
    argv = list(argv)
    if not argv:
        raise RunnerError("argv 不能为空")
    for item in argv:
        if not isinstance(item, str):
            raise RunnerError("argv 只能包含字符串")
        if "\x00" in item:
            raise RunnerError("argv 含 NUL 字节")

    opts = options or RunOptions()
    env = _base_env(opts.env_extra)
    stdin_bytes = opts.stdin_text.encode("utf-8") if opts.stdin_text is not None else None

    kwargs: dict = {
        "cwd": str(opts.cwd) if opts.cwd else None,
        "env": env,
        "stdin": subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,  # 硬编码：永不走 shell
        "close_fds": True,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # 便于整组回收

    started = time.perf_counter()
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as exc:
        raise RunnerError(f"找不到可执行文件: {argv[0]}") from exc
    except OSError as exc:
        raise RunnerError(f"启动进程失败: {exc}") from exc

    if opts.on_spawn:
        try:
            opts.on_spawn(proc)
        except Exception:  # noqa: BLE001 - 回调失败不应该影响任务
            pass

    timed_out = False
    cancelled = False
    try:
        out_b, err_b = _communicate(proc, stdin_bytes, opts.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        out_b, err_b = _drain(proc)
    finally:
        duration = time.perf_counter() - started
        _close_pipes(proc)

    if proc.returncode is None:
        rc = -1
    else:
        rc = proc.returncode
    # 被外部 kill（取消）时 Windows 返回码是 1 / POSIX 是 -9
    cancelled = (not timed_out) and rc != 0 and getattr(proc, "_lintloom_cancelled", False)

    stdout, out_trunc = _decode(out_b)
    stderr, err_trunc = _decode(err_b)
    return ProcResult(
        argv=tuple(argv),
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        duration=duration,
        timed_out=timed_out,
        cancelled=cancelled,
        stdout_truncated=out_trunc,
        stderr_truncated=err_trunc,
        pid=proc.pid,
    )


def _communicate(proc: subprocess.Popen, stdin_bytes: bytes | None, timeout: float):
    try:
        return proc.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise


def _drain(proc: subprocess.Popen) -> tuple[bytes, bytes]:
    """进程被杀后把管道里剩余数据读干净，否则残留句柄会泄漏。"""
    if proc.stdin:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        out, err = proc.communicate(timeout=10)
        return out or b"", err or b""
    except subprocess.TimeoutExpired:
        return b"", b""


def _close_pipes(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass
    # 进程已结束，回收句柄表
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class CancellableProcess:
    """把子进程句柄暴露给作业层，供「取消」使用。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.cancelled = False

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._proc = proc
            if self.cancelled:
                kill_tree(proc)

    def cancel(self) -> bool:
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc is None:
            return False
        kill_tree(proc)
        try:
            setattr(proc, "_lintloom_cancelled", True)
        except AttributeError:
            pass
        return True

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc else None


def which_display(argv: Sequence[str]) -> str:
    """把一个 argv 渲染成可在界面上显示的（带安全引号）命令行。"""
    parts = []
    for item in argv:
        if item == "" or any(ch in item for ch in ' \t"'):
            parts.append('"' + item.replace('"', '\\"') + '"')
        else:
            parts.append(item)
    if sys.platform == "win32":
        return " ".join(parts)
    return " ".join(parts)
