import io
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from costcontrol.audit import detect_anomalies
from costcontrol.cli import main
from costcontrol.dataset import load_dataset
from costcontrol.diagnosis import diagnose_facts
from costcontrol.facts import build_variance_facts
from costcontrol.llm_explainer import load_sense
from costcontrol.run import run_cost
from costcontrol.tasks import TaskBoard, build_board, compare, render_board

ROOT = Path(__file__).resolve().parent.parent
DIRTY = ROOT / "data" / "dirty_erp_export.json"
FIXED = ROOT / "data" / "dirty_erp_export_fixed.json"


def make_dir() -> Path:
    path = ROOT / f".test-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_and_diagnose(path: Path):
    ds = load_dataset(path)
    cost_run = run_cost(ds)
    sense = load_sense()
    facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
    return ds, cost_run, diagnose_facts(facts, cost_run), detect_anomalies(ds, cost_run)


class TaskBoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds, cls.cost_run, cls.diagnoses, cls.anomalies = run_and_diagnose(DIRTY)
        cls.board = build_board(
            cls.anomalies, cls.cost_run, cls.diagnoses, as_of=date(2026, 10, 31)
        )

    def test_tasks(self):
        self.assertEqual(len(self.board.tasks), 4)
        self.assertEqual(len(self.board.open_tasks), 4)
        first = self.board.tasks[0]
        self.assertEqual(first.due_date, "2026-11-01")
        self.assertEqual(first.severity, "高")
        self.assertTrue(first.task_id.startswith("T-"))

    def test_save_and_load(self):
        directory = make_dir()
        try:
            path = directory / "tasks.json"
            self.board.save(path)
            loaded = TaskBoard.load(path)
            self.assertEqual(len(loaded.tasks), 4)
            self.assertEqual(loaded.snapshot["unallocated"], "1460.00")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_mark_done(self):
        directory = make_dir()
        try:
            path = directory / "tasks.json"
            self.board.save(path)
            loaded = TaskBoard.load(path)
            loaded.mark_done(loaded.tasks[0].task_id, "已补录")
            self.assertEqual(len(loaded.open_tasks), 3)
            self.assertEqual(loaded.tasks[0].status, "done")
            self.assertEqual(loaded.tasks[0].note, "已补录")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_compare_with_fixed(self):
        _ds, _run, after_diag, after_anomalies = run_and_diagnose(FIXED)
        comparison = compare(self.board, _run, after_diag, after_anomalies)
        self.assertEqual(len(comparison.resolved), 3)
        self.assertTrue(any("CC-MNT" in title for title in comparison.remaining))
        rows = {row[0]: row for row in comparison.rows}
        self.assertEqual(rows["未分配制造费用"][1], "1,460.00")
        self.assertEqual(rows["未分配制造费用"][2], "260.00")
        self.assertEqual(rows["数据异常条数"][2], "1")


class TaskCliTest(unittest.TestCase):
    def setUp(self):
        self.directory = make_dir()

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_generate_and_close(self):
        path = self.directory / "tasks.json"
        code, text = self.run_cli(
            "--data",
            str(DIRTY),
            "--tasks",
            "--task-file",
            str(path),
            "--as-of",
            "2026-10-31",
        )
        self.assertEqual(code, 0)
        self.assertIn("排雷工单", text)
        self.assertTrue(path.exists())

        board = TaskBoard.load(path)
        ids = [task.task_id for task in board.tasks]
        args = ["--data", str(FIXED), "--task-file", str(path)]
        for task_id in ids:
            args.extend(["--task-done", task_id])
        code, text = self.run_cli(*args)
        self.assertEqual(code, 0)
        self.assertIn("已消除", text)
        self.assertIn("CC-MNT", text)
        self.assertIn("自动复跑对比", text)

    def test_rerun(self):
        path = self.directory / "tasks.json"
        self.run_cli(
            "--data", str(DIRTY), "--tasks", "--task-file", str(path),
            "--as-of", "2026-10-31",
        )
        code, text = self.run_cli(
            "--data", str(FIXED), "--rerun-tasks", "--task-file", str(path)
        )
        self.assertEqual(code, 0)
        self.assertIn("补证后复跑", text)
        self.assertIn("PO-1002 单位成本", text)


if __name__ == "__main__":
    unittest.main()
