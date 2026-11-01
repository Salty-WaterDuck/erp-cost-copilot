import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.audit import detect_anomalies, render_anomalies

ROOT = Path(__file__).resolve().parent.parent
DIRTY = ROOT / "data" / "dirty_erp_export.json"


class DirtyDataAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DIRTY)
        cls.cost_run = run_cost(cls.ds)
        cls.anomalies = detect_anomalies(cls.ds, cls.cost_run)
        cls.by_code = {}
        for item in cls.anomalies:
            cls.by_code.setdefault(item.code, []).append(item)

    def test_three_typical_errors(self):
        self.assertIn("ORDER-NO-LABOR", self.by_code)
        self.assertIn("NEGATIVE-ISSUE-NO-RETURN", self.by_code)
        self.assertIn("DRIVER-ZERO", self.by_code)

    def test_order_without_labor(self):
        item = self.by_code["ORDER-NO-LABOR"][0]
        self.assertIn("PO-1002", item.title)
        self.assertEqual(item.severity, "高")
        self.assertIn("报工 0 小时", item.detail)
        self.assertIn("补录报工单", item.action)

    def test_negative_issue_without_return(self):
        item = self.by_code["NEGATIVE-ISSUE-NO-RETURN"][0]
        self.assertIn("MI-1016", item.title)
        self.assertEqual(item.amount, Decimal("176.00"))
        self.assertIn("仓库主管", item.owner)

    def test_driver_zero_center(self):
        items = self.by_code["DRIVER-ZERO"]
        self.assertTrue(any("CC-PAINT" in item.title for item in items))
        item = next(item for item in items if "CC-PAINT" in item.title)
        self.assertEqual(item.amount, Decimal("1200.00"))

    def test_sorted_by_severity(self):
        order = {"高": 0, "中": 1, "低": 2}
        values = [order[item.severity] for item in self.anomalies]
        self.assertEqual(values, sorted(values))

    def test_render(self):
        text = render_anomalies(self.anomalies)
        self.assertIn("数据排雷", text)
        self.assertIn("完工未报工", text)
        self.assertIn("负数领用", text)
        self.assertIn("分摊动因为 0", text)


if __name__ == "__main__":
    unittest.main()
