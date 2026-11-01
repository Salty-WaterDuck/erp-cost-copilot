import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.cli import main
from costcontrol.diagnosis import diagnose_facts, render_diagnosis
from costcontrol.facts import build_variance_facts
from costcontrol.llm_explainer import load_sense

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "demo_ningbo.json"


class DiagnosisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.facts = build_variance_facts(
            cls.cost_run, cls.sense.raw.get("org", {})
        )
        cls.diagnoses = diagnose_facts(cls.facts, cls.cost_run)
        cls.by_key = {
            (d.fact.cost_element, d.fact.material, d.fact.order_no): d
            for d in cls.diagnoses
        }

    def test_every_fact_diagnosed(self):
        self.assertEqual(len(self.diagnoses), len(self.facts))
        self.assertTrue(all(d.top is not None for d in self.diagnoses))

    def test_material_overuse_checks_returns(self):
        item = self.by_key[("MAT-QTY-VAR", "RM-SS304", "PO-1001")]
        self.assertEqual(item.top.code, "OVERUSE")
        self.assertEqual(item.confidence, "中")
        self.assertTrue(any("已退料 5" in line for line in item.top.evidence))
        self.assertIn("未入账退料", item.top.falsifier)

    def test_material_saving(self):
        item = self.by_key[("MAT-QTY-VAR", "RM-ABS", "PO-1002")]
        self.assertEqual(item.top.code, "SAVING")

    def test_transfer_price_traces_upstream(self):
        item = self.by_key[("MAT-PRICE-VAR", "SF-BODY", "PO-1003")]
        self.assertEqual(item.top.code, "TRANSFER")
        self.assertEqual(item.confidence, "高")
        self.assertTrue(any("PO-1001" in line for line in item.top.evidence))

    def test_labor_overtime(self):
        item = next(
            d
            for d in self.diagnoses
            if d.fact.cost_element == "LABOR-EFF-VAR"
            and d.fact.order_no == "PO-1003"
            and d.fact.cost_center == "CC-ASM"
        )
        self.assertEqual(item.top.code, "OVERTIME")
        self.assertEqual(item.confidence, "高")

    def test_overhead_spending(self):
        item = next(
            d
            for d in self.diagnoses
            if d.fact.cost_element == "OH-SPEND-VAR" and d.fact.order_no == "PO-1001"
        )
        self.assertEqual(item.top.code, "SPENDING")

    def test_purchase_price(self):
        item = self.by_key[("MAT-PRICE-VAR", "PT-THERMO", "PO-1001")]
        self.assertEqual(item.top.code, "PURCHASE")
        self.assertTrue(any("东莞温控" in line for line in item.top.evidence))

    def test_candidates_have_falsifiers(self):
        for diagnosis in self.diagnoses:
            with self.subTest(fact=diagnosis.fact.fact_id):
                self.assertTrue(diagnosis.top.falsifier)

    def test_report(self):
        text = render_diagnosis(self.diagnoses)
        self.assertIn("差异诊断", text)
        self.assertIn("反证", text)
        self.assertIn("备选", text)
        self.assertIn("不是概率", text)


class DiagnosisCliTest(unittest.TestCase):
    def test_cli_diagnose(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--diagnose"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("差异诊断", text)
        self.assertIn("主因", text)


if __name__ == "__main__":
    unittest.main()
