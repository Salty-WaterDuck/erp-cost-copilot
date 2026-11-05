import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path

from costcontrol.cli import main
from costcontrol.driver_facts import load_driver_dataset
from costcontrol.driver_rules import audit_drivers, render_audit

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "abc_driver_sample.json"


class DriverAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_driver_dataset(SAMPLE)
        cls.report = audit_drivers(cls.dataset)
        cls.by_event = {gate.fact.event_id: gate for gate in cls.report.gates}
        cls.codes = {issue.code for issue in cls.report.issues}

    def test_dataset_shape(self):
        self.assertEqual(len(self.dataset.facts), 43)
        self.assertEqual(len(self.dataset.resources), 4)
        self.assertEqual(len(self.dataset.activities), 5)

    def test_gate_counts(self):
        self.assertEqual(
            self.report.counts(),
            {"accepted": 32, "review": 1, "estimated": 1, "rejected": 9},
        )

    def test_typical_error_codes(self):
        for code in (
            "ZERO-OR-NEGATIVE",
            "OVERLAP",
            "DUPLICATE",
            "UNIT-MISMATCH",
            "MISSING-COST-OBJECT",
            "STATE-MISMATCH",
            "CAPACITY-EXCEEDED",
            "LATE-ARRIVAL",
            "STALE-SENSOR",
            "DRIVER-TOTAL-MISMATCH",
        ):
            self.assertIn(code, self.codes)

    def test_estimated_fact_enters_with_flag(self):
        gate = self.by_event["EV-ERR-10"]
        self.assertEqual(gate.status, "estimated")
        self.assertEqual(gate.confidence, "B")

    def test_rejected_zero_and_negative(self):
        self.assertEqual(self.by_event["EV-ERR-01"].status, "rejected")
        self.assertEqual(self.by_event["EV-ERR-02"].status, "rejected")

    def test_reconciliation(self):
        issue = next(
            item for item in self.report.issues if item.code == "DRIVER-TOTAL-MISMATCH"
        )
        self.assertIn("差 4", issue.detail)
        self.assertEqual(self.report.actual_totals["喷涂"], Decimal("279"))

    def test_render(self):
        text = render_audit(self.report)
        self.assertIn("ABC 动因数据前置校验", text)
        self.assertIn("Gate 汇总", text)
        self.assertIn("可追溯示例", text)
        self.assertIn("拒绝", text)


class DriverCliTest(unittest.TestCase):
    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--abc-check", str(SAMPLE)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("ABC 动因数据前置校验", text)
        self.assertIn("Gate 汇总", text)

    def test_missing_file(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--abc-check", str(ROOT / "data" / "missing.json")])
        self.assertEqual(code, 2)
        self.assertIn("ABC 动因校验失败", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
