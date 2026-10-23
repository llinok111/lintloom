"""LintLoom 运行期配置。

设计原则：
1. 所有可执行路径、根目录白名单、上限常量集中在此，方便审计。
2. 前端（浏览器）无权新增根目录 —— 根目录只能由启动参数指定（运维可信输入），
   否则任何本地页面都能借 API 读取任意磁盘路径。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "LintLoom"
APP_VERSION = "1.0.0"

# 上游被封装的目标（版本经 api.github.com 核实，见 docs/SOURCES.md）
UPSTREAM_REPO = "sqlfluff/sqlfluff"
UPSTREAM_PINNED = "4.4.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许前端访问的目录根。key 为逻辑名，值为真实目录（启动时 realpath 归一）。
DEFAULT_ROOTS = {
    "samples": PROJECT_ROOT / "samples",
    "workspace": PROJECT_ROOT / "workspace",
}

WORK_DIR = PROJECT_ROOT / ".lintloom-work"
BACKUP_DIR = PROJECT_ROOT / ".lintloom-backups"

# 单次作业最多扫描的文件数（防止误选超大目录导致长时间占用）
MAX_FILES_PER_JOB = 200
# 单文件大小上限（字节），超过直接拒绝，避免把 500MB 文件读进内存
MAX_FILE_BYTES = 8 * 1024 * 1024
# 编辑器缓冲区（stdin 模式）文本上限
MAX_BUFFER_CHARS = 200_000
# 子进程 stdout/stderr 截断上限
MAX_STREAM_BYTES = 8 * 1024 * 1024
# HTTP 请求体上限
MAX_BODY_BYTES = 1 * 1024 * 1024

TIMEOUT_DEFAULT = 60.0
TIMEOUT_MAX = 300.0
TIMEOUT_MIN = 1.0

# 允许操作的文件类型（白名单，任何不在其中的路径一律拒绝）
ALLOWED_EXT = frozenset({".sql", ".jinja", ".j2"})
# 允许「写回磁盘」的文件类型（比 ALLOWED_EXT 更严）
WRITABLE_EXT = frozenset({".sql", ".jinja", ".j2"})

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".lintloom-work",
        ".lintloom-backups",
        ".tmp",
        "dist",
        "build",
    }
)


@dataclass
class Settings:
    """进程级配置。"""

    host: str = "127.0.0.1"
    port: int = 8731
    python_exe: str = field(default_factory=lambda: sys.executable)
    roots: dict[str, Path] = field(default_factory=lambda: dict(DEFAULT_ROOTS))
    timeout_cap: float = TIMEOUT_MAX
    allow_apply: bool = True
    max_files: int = MAX_FILES_PER_JOB
    open_browser: bool = True
    # 临时工作目录与备份目录可注入，测试用例会指向临时目录，避免污染项目
    work_dir: Path = field(default_factory=lambda: WORK_DIR)
    backup_dir: Path = field(default_factory=lambda: BACKUP_DIR)

    def normalised_roots(self) -> dict[str, Path]:
        """realpath 归一后的根目录表。不存在的根直接剔除。"""
        out: dict[str, Path] = {}
        for label, path in self.roots.items():
            try:
                real = Path(os.path.realpath(path))
            except OSError:
                continue
            if real.is_dir():
                out[label] = real
        return out

    def root_map(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.normalised_roots().items()}
