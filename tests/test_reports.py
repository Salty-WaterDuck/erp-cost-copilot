import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from costcontrol.cli import main
from costcontrol.reports import analyze, load_bundle, render_analysis

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "auto_reports"


class LoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)

    def test_counts(self):
        self.assertEqual(len(self.bundle.structure), 5)
        self.assertEqual(len(self.bundle.inventory), 18)
        self.assertEqual(len(self.bundle.orders), 18)
        self.assertEqual(len(self.bundle.restoration), 6)
        self.assertEqual(len(self.bundle.daily), 18)
        self.assertEqual(len(self.bundle.scrap), 6)

    def test_product_and_totals(self):
        self.assertEqual(self.bundle.product, "制动卡钳总成")
        total = next(c for c in self.bundle.structure if c.is_total)
        self.assertEqual(total.avg_2025, Decimal("450.8"))
        self.assertEqual(total.avg_2026, Decimal("469.2"))

    def test_order_properties(self):
        order = self.bundle.orders[0]
        self.assertEqual(order.order_no, "MO-2501-001")
        self.assertEqual(order.total_cost, Decimal("1668727.00"))
        self.assertAlmostEqual(float(order.computed_unit), 344.07, places=2)
        self.assertAlmostEqual(float(order.recon_pct or 0), 0.0, places=2)

    def test_scrap_weighted_price(self):
        first = self.bundle.scrap[0]
        self.assertEqual(first.period, "2025Q1")
        self.assertEqual(first.weight, Decimal("1200"))
        self.assertEqual(first.price, Decimal("17.0"))


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)
        cls.analysis = analyze(cls.bundle)
        cls.by_code = {finding.code: finding for finding in cls.analysis.findings}

    def test_structure_drift(self):
        finding = self.by_code["STRUCTURE-DRIFT"]
        self.assertIn("+4.08%", finding.detail)
        self.assertIn("+6.46%", finding.detail)
        self.assertIn("制造费用", finding.detail)

    def test_order_outlier(self):
        finding = self.by_code["ORDER-OUTLIER"]
        self.assertIn("2025-10", finding.detail)
        self.assertIn("354.05", finding.detail)
        self.assertIn("4.62%", finding.detail)

    def test_scrap_price_volume_split(self):
        finding = self.by_code["SCRAP-RECOVERY-TREND"]
        self.assertIn("-15.19%", finding.detail)
        self.assertIn("-4727", finding.detail)
        self.assertIn("-4873", finding.detail)
        self.assertIn("17.56→16.24", finding.detail)

    def test_inventory_gap_is_minor(self):
        finding = self.by_code["INVENTORY-BALANCE"]
        self.assertEqual(finding.severity, "低")
        self.assertIn("0.10", finding.detail)

    def test_clean_checks_not_flagged(self):
        # 新版数据：单位成本勾稽、还原守恒、构成不重复、期间连续
        for code in (
            "UNIT-COST-RECON",
            "RESTORATION-TOTAL",
            "SEMI-DOUBLE-COUNT",
            "DATA-GAPS",
        ):
            self.assertNotIn(code, self.by_code, code)

    def test_findings_sorted_by_severity(self):
        order = {"高": 0, "中": 1, "低": 2}
        values = [order[f.severity] for f in self.analysis.findings]
        self.assertEqual(values, sorted(values))

    def test_render(self):
        text = render_analysis(self.analysis)
        self.assertIn("成本体检报告", text)
        self.assertIn("量价", text.replace("价格因素", "量价"))
        self.assertIn("净产量", text)


class CliReportsTest(unittest.TestCase):
    def test_cli_reports(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--reports", str(DATA)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("成本体检报告", text)

    def test_missing_directory(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--reports", str(DATA / "missing")])
        self.assertEqual(code, 2)
        self.assertIn("读取报表失败", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
