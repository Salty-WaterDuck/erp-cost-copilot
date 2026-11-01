import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.facts import build_variance_facts, facts_to_dicts
from costcontrol.llm_explainer import load_sense
from costcontrol.responsibility import (
    attribute,
    load_rules,
    render_report,
    render_template,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "demo_ningbo.json"


class FactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.org = cls.sense.raw.get("org", {})
        cls.facts = build_variance_facts(cls.cost_run, cls.org)

    def test_fact_count_and_total(self):
        self.assertEqual(len(self.facts), 31)
        total = sum((f.amount for f in self.facts), Decimal(0))
        self.assertLessEqual(abs(total - self.cost_run.sheet.variance_total), Decimal("0.01"))

    def test_po1001_material_facts(self):
        selected = {
            (f.cost_element, f.material): f
            for f in self.facts
            if f.order_no == "PO-1001"
        }
        self.assertEqual(selected[("MAT-PRICE-VAR", "RM-SS304")].amount, Decimal("178.50"))
        self.assertEqual(selected[("MAT-QTY-VAR", "RM-SS304")].amount, Decimal("285.60"))

    def test_semi_material_carries_upstream_orders(self):
        fact = next(
            f
            for f in self.facts
            if f.material == "SF-BODY" and f.cost_element == "MAT-PRICE-VAR"
        )
        self.assertEqual(fact.material_type, "SEMI")
        self.assertEqual(fact.evidence["upstream_orders"], "PO-1001")
        self.assertEqual(fact.amount, Decimal("680.13"))

    def test_material_facts_have_primary_cost_center(self):
        fact = next(
            f
            for f in self.facts
            if f.material == "RM-SS304" and f.cost_element == "MAT-QTY-VAR"
        )
        self.assertEqual(fact.cost_center, "CC-ASM")
        self.assertEqual(fact.work_center, "WC-ASM-1")
        self.assertEqual(fact.supplier, "宁波金属材料")

    def test_facts_are_json_serializable(self):
        payload = json.dumps(facts_to_dicts(self.facts), ensure_ascii=False)
        self.assertIn("MAT-PRICE-VAR", payload)
        self.assertIn("RM-SS304", payload)


class AttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.org = cls.sense.raw.get("org", {})
        cls.facts = build_variance_facts(cls.cost_run, cls.org)
        cls.results = attribute(cls.facts, cls.sense)
        cls.by_key = {
            (a.fact.cost_element, a.fact.material, a.fact.order_no): a
            for a in cls.results
        }

    def test_rules_loaded(self):
        rules, fallback = load_rules(self.sense)
        self.assertEqual(len(rules), 7)
        self.assertEqual(fallback, "成本会计复核")

    def test_raw_price_goes_to_purchasing(self):
        item = self.by_key[("MAT-PRICE-VAR", "PT-THERMO", "PO-1001")]
        self.assertEqual(item.owner, "采购部")
        self.assertEqual(item.confidence, "高")
        self.assertEqual(item.missing_evidence, [])

    def test_quantity_variance_goes_to_cost_center(self):
        item = self.by_key[("MAT-QTY-VAR", "RM-SS304", "PO-1001")]
        self.assertEqual(item.owner, "总装车间")
        self.assertEqual(item.secondary, "WC-ASM-1 产线")
        self.assertEqual(item.confidence, "高")

    def test_semi_price_goes_to_upstream(self):
        item = self.by_key[("MAT-PRICE-VAR", "SF-BODY", "PO-1003")]
        self.assertEqual(item.owner, "上游工单责任车间")
        self.assertEqual(item.secondary, "成本会计")
        self.assertEqual(item.confidence, "高")

    def test_labor_efficiency_goes_to_line(self):
        item = next(
            a
            for a in self.results
            if a.fact.cost_element == "LABOR-EFF-VAR" and a.fact.order_no == "PO-1001"
        )
        self.assertEqual(item.owner, "WC-ASM-1 产线负责人")

    def test_all_facts_attributed_in_demo(self):
        self.assertTrue(all(item.matched for item in self.results))

    def test_missing_supplier_lowers_confidence(self):
        item = next(
            a
            for a in self.results
            if a.fact.cost_element == "MAT-PRICE-VAR" and a.fact.material == "PT-CORD"
        )
        self.assertEqual(item.owner, "采购部")
        self.assertEqual(item.confidence, "中")
        self.assertIn("supplier", item.missing_evidence)

    def test_unmatched_rule_falls_back(self):
        attribution = dict(self.sense.raw["attribution"])
        attribution["rules"] = [
            rule
            for rule in attribution["rules"]
            if rule["when"].get("cost_element") != "OH-SPEND-VAR"
        ]
        custom = replace(self.sense, raw={**self.sense.raw, "attribution": attribution})
        results = attribute(self.facts, custom)
        item = next(
            a for a in results if a.fact.cost_element == "OH-SPEND-VAR"
        )
        self.assertFalse(item.matched)
        self.assertEqual(item.owner, "成本会计复核")
        self.assertEqual(item.confidence, "低")

    def test_report_contains_owners_and_confidence(self):
        text = render_report(self.results, self.sense)
        self.assertIn("差异归因报告", text)
        self.assertIn("采购部", text)
        self.assertIn("总装车间", text)
        self.assertIn("置信度", text)
        self.assertIn("不替代审批", text)


class TemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.org = cls.sense.raw.get("org", {})
        cls.facts = build_variance_facts(cls.cost_run, cls.org)

    def test_cost_center_resolves_to_name(self):
        fact = next(f for f in self.facts if f.cost_center == "CC-ASM")
        self.assertEqual(render_template("{{ cost_center }}", fact, self.org), "总装车间")

    def test_unknown_or_empty_field_returns_empty(self):
        fact = next(f for f in self.facts if not f.supplier)
        self.assertEqual(render_template("{{ supplier }}", fact, self.org), "")
        self.assertEqual(render_template("{{ nope }}", fact, self.org), "")

    def test_static_template_unchanged(self):
        fact = self.facts[0]
        self.assertEqual(render_template("采购部", fact, self.org), "采购部")


if __name__ == "__main__":
    unittest.main()
