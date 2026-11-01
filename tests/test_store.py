import io
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from costcontrol import load_dataset, run_cost
from costcontrol.cli import main
from costcontrol.close import close_period
from costcontrol.store import PeriodStatus, Store, StoreError

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"
NOV = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo_2026_11.json"
# Windows 沙箱下 %TEMP% 与 0700 目录可能不可写，测试临时库统一放在工作区内
ROOT = Path(__file__).resolve().parent.parent


def make_temp_dir() -> Path:
    path = ROOT / f".test-{uuid4().hex[:10]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def shift_period(ds, period: str):
    """把一整套数据平移到另一个期间（单据号加前缀，避免主键冲突）。"""
    prefix = period[-2:] + "-"
    return replace(
        ds,
        period=period,
        orders=[replace(o, order_no=prefix + o.order_no) for o in ds.orders],
        issues=[
            replace(i, doc_no=prefix + i.doc_no, order_no=prefix + i.order_no)
            for i in ds.issues
        ],
        labor=[
            replace(r, doc_no=prefix + r.doc_no, order_no=prefix + r.order_no)
            for r in ds.labor
        ],
        overhead=[replace(o, doc_no=prefix + o.doc_no) for o in ds.overhead],
        opening_balances={},
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = make_temp_dir()
        self.path = self.tmp_dir / "cost.db"
        self.store = Store(self.path)
        self.store.init_schema()
        self.ds = load_dataset(DATA)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


class RoundTripTest(StoreTestCase):
    def test_round_trip_preserves_dataset(self):
        self.store.save_period(self.ds)
        loaded = self.store.load_period("2026-10")
        self.assertEqual(loaded.company, self.ds.company)
        self.assertEqual(len(loaded.orders), 4)
        self.assertEqual(len(loaded.issues), 15)
        self.assertEqual(len(loaded.labor), 6)
        self.assertEqual(len(loaded.overhead), 8)
        self.assertEqual(loaded.opening_balances, self.ds.opening_balances)
        self.assertEqual(loaded.params, self.ds.params)

    def test_round_trip_produces_same_cost(self):
        self.store.save_period(self.ds)
        loaded = self.store.load_period("2026-10")
        original = run_cost(self.ds)
        restored = run_cost(loaded)
        self.assertEqual(restored.sheet.total_cost, original.sheet.total_cost)
        self.assertEqual(restored.sheet.variance_total, original.sheet.variance_total)
        self.assertEqual(
            [o.order_no for o in restored.sheet.orders],
            [o.order_no for o in original.sheet.orders],
        )

    def test_decimals_survive_storage(self):
        self.store.save_period(self.ds)
        loaded = self.store.load_period("2026-10")
        order = next(o for o in loaded.orders if o.order_no == "PO-1004")
        self.assertEqual(order.wip_progress, Decimal("0.4"))
        issue = next(i for i in loaded.issues if i.doc_no == "MI-1001")
        self.assertEqual(issue.unit_price, Decimal("12.30"))

    def test_idempotent_import(self):
        self.store.save_period(self.ds)
        self.store.save_period(self.ds)
        self.assertEqual(len(self.store.load_period("2026-10").issues), 15)

    def test_period_isolation(self):
        self.store.save_period(self.ds)
        self.store.save_period(shift_period(self.ds, "2026-11"))
        october = self.store.load_period("2026-10")
        november = self.store.load_period("2026-11")
        self.assertTrue(all(o.order_no.startswith("PO-") for o in october.orders))
        self.assertTrue(all(o.order_no.startswith("11-") for o in november.orders))
        self.assertEqual(len(october.orders), 4)
        self.assertEqual(len(november.orders), 4)


class PeriodStatusTest(StoreTestCase):
    def test_new_period_is_open(self):
        info = self.store.save_period(self.ds)
        self.assertIs(info.status, PeriodStatus.OPEN)
        self.assertTrue(info.is_open)
        self.assertIsNone(info.closed_at)

    def test_close_and_reopen(self):
        self.store.save_period(self.ds)
        closed = self.store.set_status("2026-10", PeriodStatus.CLOSED)
        self.assertFalse(closed.is_open)
        self.assertIsNotNone(closed.closed_at)
        reopened = self.store.set_status("2026-10", PeriodStatus.OPEN)
        self.assertTrue(reopened.is_open)
        self.assertIsNone(reopened.closed_at)

    def test_cannot_overwrite_closed_period(self):
        self.store.save_period(self.ds)
        self.store.set_status("2026-10", PeriodStatus.CLOSED)
        with self.assertRaises(StoreError):
            self.store.save_period(self.ds)

    def test_cannot_delete_closed_period(self):
        self.store.save_period(self.ds)
        self.store.set_status("2026-10", PeriodStatus.CLOSED)
        with self.assertRaises(StoreError):
            self.store.delete_period("2026-10")

    def test_unknown_period(self):
        with self.assertRaises(StoreError):
            self.store.set_status("1999-01", PeriodStatus.CLOSED)
        with self.assertRaises(StoreError):
            self.store.load_period("1999-01")

    def test_list_periods(self):
        self.store.save_period(self.ds)
        self.store.save_period(shift_period(self.ds, "2026-11"))
        infos = self.store.list_periods()
        self.assertEqual([i.period for i in infos], ["2026-10", "2026-11"])
        self.assertTrue(all(i.orders == 4 for i in infos))


class CarryStoreTest(StoreTestCase):
    def test_close_writes_next_period_opening(self):
        self.store.save_period(self.ds)
        report = close_period(run_cost(self.ds))
        self.assertIsNotNone(report.carry)
        self.store.close_with_carry("2026-10", report.carry)

        info = self.store.period_info("2026-11")
        self.assertIsNotNone(info)
        self.assertTrue(info.is_open)

        november = self.store.load_period("2026-11")
        self.assertEqual(november.opening_balances, report.carry.balances)
        self.assertEqual(len(november.wip_opening), 2)
        self.assertEqual(len(november.orders), 2)
        self.assertTrue(all(o.carried_from == "2026-10" for o in november.orders))

    def test_import_november_preserves_opening(self):
        self.store.save_period(self.ds)
        report = close_period(run_cost(self.ds))
        self.store.close_with_carry("2026-10", report.carry)

        nov = load_dataset(NOV)
        self.store.save_period(nov)
        loaded = self.store.load_period("2026-11")
        self.assertEqual(loaded.wip_opening, nov.wip_opening)
        self.assertEqual(len(loaded.orders), 3)
        restored = run_cost(loaded)
        self.assertEqual(restored.sheet.total_cost, run_cost(nov).sheet.total_cost)

    def test_schema_version_is_two(self):
        row = self.store.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        self.assertEqual(row["value"], "2")


class UninitializedTest(unittest.TestCase):
    def test_reading_uninitialized_database(self):
        tmp_dir = make_temp_dir()
        store = Store(tmp_dir / "empty.db")
        try:
            with self.assertRaises(StoreError):
                store.load_period("2026-10")
        finally:
            store.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = make_temp_dir()
        self.path = self.tmp_dir / "cost.db"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_db_workflow(self):
        code, _ = self.run_cli("--db", str(self.path), "--init")
        self.assertEqual(code, 0)

        code, text = self.run_cli(
            "--db", str(self.path), "--import-json", str(DATA)
        )
        self.assertEqual(code, 0)
        self.assertIn("2026-10", text)

        code, text = self.run_cli("--db", str(self.path), "--periods")
        self.assertEqual(code, 0)
        self.assertIn("开放", text)

        code, text = self.run_cli(
            "--db", str(self.path), "--period", "2026-10", "--close"
        )
        self.assertEqual(code, 0)
        self.assertIn("已结账", text)

        code, text = self.run_cli("--db", str(self.path), "--periods")
        self.assertIn("已结账", text)

        # 已结账期间不允许再次导入
        code, text = self.run_cli(
            "--db", str(self.path), "--import-json", str(DATA)
        )
        self.assertEqual(code, 2)
        self.assertIn("已结账", text)

    def test_missing_period(self):
        self.run_cli("--db", str(self.path), "--init")
        code, text = self.run_cli("--db", str(self.path), "--period", "2026-10")
        self.assertEqual(code, 2)
        self.assertIn("不存在", text)

    def test_db_requires_period(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--db", str(self.path))


if __name__ == "__main__":
    unittest.main()
