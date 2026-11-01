import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol.yaml_lite import YamlError, load_file, loads

ROOT = Path(__file__).resolve().parent.parent
SENSE = ROOT / "config_templates" / "discrete_manufacturing.yaml"


class YamlLiteTest(unittest.TestCase):
    def test_nested_map_and_list(self):
        data = loads(
            """
company: 恒宇
period: 2026-10
totals:
  cost: 1.50
  ok: true
  empty: null
notes:
  - 第一条
  - "第二条 # 不是注释"
"""
        )
        self.assertEqual(data["company"], "恒宇")
        self.assertEqual(data["totals"]["cost"], Decimal("1.50"))
        self.assertIs(data["totals"]["ok"], True)
        self.assertIsNone(data["totals"]["empty"])
        self.assertEqual(data["notes"], ["第一条", "第二条 # 不是注释"])

    def test_comments_are_stripped(self):
        data = loads(
            """
# 头部注释
a: 1  # 行尾注释
b: "x#y"
"""
        )
        self.assertEqual(data, {"a": 1, "b": "x#y"})

    def test_inline_list(self):
        data = loads("basis: [machine_hours, labor_hours]\n")
        self.assertEqual(data["basis"], ["machine_hours", "labor_hours"])

    def test_empty_key_value(self):
        data = loads("a:\nb: 1\n")
        self.assertIsNone(data["a"])
        self.assertEqual(data["b"], 1)

    def test_reject_tab_indent(self):
        with self.assertRaises(YamlError):
            loads("a:\n\tb: 1\n")

    def test_list_of_maps(self):
        data = loads(
            """
items:
  - key: value
    other: 2
  - key: b
"""
        )
        self.assertEqual(
            data["items"], [{"key": "value", "other": 2}, {"key": "b"}]
        )

    def test_inline_map(self):
        data = loads("when: { cost_element: MAT-PRICE-VAR }\n")
        self.assertEqual(data["when"], {"cost_element": "MAT-PRICE-VAR"})

    def test_reject_missing_colon(self):
        with self.assertRaises(YamlError):
            loads("a:\n  broken line\n")


class SenseFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_file(SENSE)

    def test_identity(self):
        self.assertEqual(self.data["industry"], "discrete_manufacturing")
        self.assertEqual(self.data["label"], "离散制造（组装型）")

    def test_rule_map(self):
        self.assertEqual(self.data["rule_map"]["OH-UNALLOCATED"], "unallocated_overhead")
        self.assertEqual(self.data["rule_map"]["MAT-OVERUSE"], "unreturned_material")

    def test_anomalies_have_actionable_fields(self):
        for key, item in self.data["anomalies"].items():
            with self.subTest(anomaly=key):
                self.assertIn("label", item)
                self.assertIn("signal", item)
                self.assertIn("suggestion", item)
                self.assertIn("owner", item)

    def test_prompt_config(self):
        self.assertGreaterEqual(self.data["prompt"]["max_items"], 3)
        self.assertTrue(self.data["prompt"]["forbidden"])

    def test_attribution_rules(self):
        attribution = self.data["attribution"]
        self.assertEqual(attribution["fallback"], "成本会计复核")
        self.assertGreaterEqual(len(attribution["rules"]), 6)
        first = attribution["rules"][0]
        self.assertIn("when", first)
        self.assertIn("owner", first)


if __name__ == "__main__":
    unittest.main()
