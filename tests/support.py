"""测试夹具：一个隔离的临时项目根，避免测试污染真实 samples/。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import config  # noqa: E402
from app.catalog import CatalogService  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.jobs import Job  # noqa: E402

SAMPLE_SQL = {
    "messy.sql": (
        "select a.id,a.name , sum(b.amount) as total\n"
        "from orders a join order_items b on a.id=b.order_id\n"
        "where a.created_at > '2024-01-01' and b.status<>'cancelled'\n"
        "group by a.id,a.name\n"
        "having sum(b.amount)>100\n"
        "order by 3 desc\n"
    ),
    "clean.sql": "select id from t\n",
    "broken.sql": "select from where\n",
    "sub/inner.sql": "SELECT x FROM y WHERE z = 1\n",
}


def have_sqlfluff() -> bool:
    try:
        import sqlfluff  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class TempProject(unittest.TestCase):
    """每个用例一个干净的根目录 + 独立的 Settings。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not have_sqlfluff():
            raise unittest.SkipTest("当前解释器没有 sqlfluff，跳过依赖真实 CLI 的集成测试")

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="lintloom-test-")
        self.root = Path(self._tmp) / "samples"
        self.root.mkdir(parents=True)
        for rel, text in SAMPLE_SQL.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        self.settings = config.Settings(
            python_exe=sys.executable,
            roots={"samples": self.root},
            allow_apply=True,
            open_browser=False,
            work_dir=Path(self._tmp) / ".work",
            backup_dir=Path(self._tmp) / ".backups",
        )
        self.catalog = CatalogService(sys.executable)
        self.engine = Engine(self.settings, self.catalog)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def new_job(self, action: str = "test") -> Job:
        return Job(action, {})


def dry_run(argv, *args, **kwargs):
    """把 run 换成记录器，用于单元测试（不真的起进程）。"""
    dry_run.calls.append((tuple(argv), kwargs))
    return None
