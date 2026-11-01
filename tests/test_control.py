import unittest
from dataclasses import replace
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.dataset import validate
from costcontrol.models import Severity

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class ControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))

    def test_unallocated_overhead_is_critical(self):
        alerts = [a for a in self.cost_run.sheet.alerts if a.rule == "OH-UNALLOCATED"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, Severity.CRITICAL)
        self.assertEqual(alerts[0].cost_center, "CC-MNT")

    def test_material_overuse_detected(self):
        alerts = [a for a in self.cost_run.sheet.alerts if a.rule == "MAT-OVERUSE"]
        self.assertTrue(any(a.material == "RM-SS304" for a in alerts))

    def test_no_false_overuse_for_material_issued_to_wip(self):
        # PO-1004 领料 200 件，对应产量 200 件（150 完工 + 47 在产 + 3 报废），
        # 在产按 100% 投料折约当量，因此这不是超耗，不应误报。
        alerts = [
            a
            for a in self.cost_run.sheet.alerts
            if a.rule == "MAT-OVERUSE" and a.order_no == "PO-1004"
        ]
        self.assertEqual(alerts, [])

    def test_over_budget_alert(self):
        alerts = [a for a in self.cost_run.sheet.alerts if a.rule == "OH-OVERBUDGET"]
        centers = {a.cost_center for a in alerts}
        self.assertEqual(centers, {"CC-ASM", "CC-QA", "CC-MNT"})
        asm = next(a for a in alerts if a.cost_center == "CC-ASM")
        self.assertEqual(asm.amount, 300)

    def test_alerts_sorted_by_severity(self):
        order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
        values = [order[a.severity] for a in self.cost_run.sheet.alerts]
        self.assertEqual(values, sorted(values))

    def test_tolerance_can_silence_order_overspend(self):
        run = run_cost(
            load_dataset(DATA),
            {"order_variance_tolerance": 1, "material_qty_tolerance": 1},
        )
        self.assertNotIn("ORDER-OVERSPEND", {a.rule for a in run.sheet.alerts})


class DatasetTest(unittest.TestCase):
    def test_validation_rejects_over_completion(self):
        ds = load_dataset(DATA)
        order = ds.orders[0]
        bad = replace(
            order, qty_completed=order.qty_planned, qty_scrapped=order.qty_planned
        )
        with self.assertRaises(ValueError):
            bad.validate()

    def test_unknown_material_in_issue_rejected(self):
        ds = load_dataset(DATA)
        ds.issues[0] = replace(ds.issues[0], material="NOPE")
        with self.assertRaises(ValueError):
            validate(ds)

    def test_unknown_work_center_rejected(self):
        ds = load_dataset(DATA)
        ds.labor[0] = replace(ds.labor[0], cost_center="CC-NOPE")
        with self.assertRaises(ValueError):
            validate(ds)


if __name__ == "__main__":
    unittest.main()
