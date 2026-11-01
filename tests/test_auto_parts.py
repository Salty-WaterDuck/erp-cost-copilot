import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from costcontrol.auto_parts import load_period, reconcile, render_second_layer
from costcontrol.cli import main
from costcontrol.run import run_cost

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "auto_reports"


class AutoPartsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_period(DATA, "2026-06")
        cls.cost_run = run_cost(cls.ds, prefer_master_price=True)

    def test_dataset_shape(self):
        self.assertEqual(len(self.ds.orders), 1)
        self.assertEqual(self.ds.orders[0].order_no, "MO-2606-001")
        self.assertEqual(len(self.ds.issues), 4)
        self.assertEqual(len(self.ds.labor), 3)
        self.assertEqual(len(self.ds.overhead), 18)
        self.assertEqual(len(self.ds.cost_centers), 3)

    def test_actual_costs(self):
        cost = self.cost_run.sheet.orders[0]
        self.assertEqual(cost.material_cost, Decimal("1153696.40"))
        self.assertEqual(cost.labor_cost, Decimal("293700.20"))
        self.assertEqual(cost.overhead_cost, Decimal("265900.00"))
        self.assertEqual(cost.current_cost, Decimal("1713296.60"))
        self.assertEqual(cost.wip_cost, Decimal("13855.29"))

    def test_reconcile_with_erp(self):
        recons = reconcile(self.cost_run, DATA, "2026-06")
        self.assertEqual(len(recons), 1)
        item = recons[0]
        self.assertEqual(item.material, item.erp_material)
        self.assertEqual(item.overhead, item.erp_overhead)
        self.assertEqual(item.erp_three, Decimal("1713296.40"))
        self.assertEqual(item.total - item.erp_three, Decimal("0.20"))
        self.assertLess(item.unit, item.erp_unit)

    def test_standard_inconsistency(self):
        text = render_second_layer(self.cost_run, DATA, "2026-06")
        self.assertIn("-37.35%", text)
        self.assertIn("11.4 倍", text)
        self.assertIn("六项差异暂不可信", text)
        self.assertIn("废品损失 17,100.00", text)


class AutoPartsCliTest(unittest.TestCase):
    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--auto-parts", str(DATA), "--month", "2026-06"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("第二层成本复算", text)

    def test_missing_month(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--auto-parts", str(DATA)])
        self.assertEqual(code, 2)
        self.assertIn("--month", out.getvalue() + err.getvalue())

    def test_unknown_period(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--auto-parts", str(DATA), "--month", "2030-01"])
        self.assertEqual(code, 2)
        self.assertIn("没有生产订单", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
