import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from costcontrol.auto_parts import load_period
from costcontrol.cli import main
from costcontrol.run import run_cost
from costcontrol.sap_baseline import analyze, load_sap_card, render_baseline

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "auto_reports"


class SapCardTest(unittest.TestCase):
    def test_card_components(self):
        card = load_sap_card(DATA)
        self.assertEqual(card.total, Decimal("456.93"))
        self.assertEqual(card.labor, Decimal("88.14"))
        self.assertEqual(card.overhead, Decimal("82.91"))
        self.assertEqual(card.outsourcing, Decimal("10.97"))
        # 分项四舍五入尾差并入材料，保证四项之和等于合计
        self.assertEqual(
            card.material + card.labor + card.overhead + card.outsourcing,
            card.total,
        )


class BaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_period(DATA, "2026-06")
        cls.cost_run = run_cost(ds, prefer_master_price=True)
        cls.result = analyze(cls.cost_run, DATA, "2026-06")
        cls.by_key = {item.key: item for item in cls.result.elements}

    def test_quantity_and_totals(self):
        self.assertEqual(self.result.quantity, Decimal("5128"))
        self.assertEqual(self.result.actual_total, Decimal("1713296.60"))
        self.assertEqual(self.result.standard_total, Decimal("2343137.04"))
        self.assertEqual(self.result.variance_total, Decimal("-629840.44"))

    def test_material_variance(self):
        item = self.by_key["material"]
        self.assertEqual(item.standard, Decimal("1409738.48"))
        self.assertEqual(item.variance, Decimal("-256042.08"))
        self.assertAlmostEqual(float(item.rate or 0), -18.16, places=2)

    def test_systemic_bias(self):
        self.assertTrue(self.result.systemic)
        self.assertLess(self.result.unit_actual, self.result.unit_standard)

    def test_render(self):
        text = render_baseline(self.result)
        self.assertIn("SAP 标准卡基准", text)
        self.assertIn("-26.88%", text)
        self.assertIn("系统性偏差", text)
        self.assertIn("外协", text)
        self.assertIn("不建议把差额归责", text)


class BaselineCliTest(unittest.TestCase):
    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["--auto-parts", str(DATA), "--month", "2026-06", "--sap-baseline"]
            )
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("SAP 标准卡基准", text)


if __name__ == "__main__":
    unittest.main()
