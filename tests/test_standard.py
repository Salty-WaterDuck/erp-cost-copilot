import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset
from costcontrol.models import BomLine
from costcontrol.standard import StandardCosts

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class StandardCostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.std = StandardCosts(cls.ds)

    def test_depth(self):
        self.assertEqual(self.std.depth("RM-SS304"), 0)
        self.assertEqual(self.std.depth("SF-BODY"), 1)
        self.assertEqual(self.std.depth("FG-KT100"), 2)

    def test_semi_finished_standard_cost_includes_conversion(self):
        # 材料 0.8×1.02×12 + 温控器 6.5 + 螺丝 0.2 = 16.492
        # 加工 0.05h × (28 + 30) = 2.9  → 19.392
        self.assertEqual(self.std.unit_cost("SF-BODY"), Decimal("19.3920"))
        self.assertEqual(self.std.unit_cost("SF-BASE"), Decimal("17.8820"))

    def test_finished_standard_cost_rolls_up(self):
        # 19.392 + 17.882 + 4.2 + 1.5 + (0.08+0.02)×(28+30) = 48.854
        self.assertEqual(self.std.unit_cost("FG-KT100"), Decimal("48.8540"))
        card = self.std.card("FG-KT100")
        self.assertEqual(card.material_cost, Decimal("42.9740"))
        self.assertEqual(card.total, Decimal("48.8540"))

    def test_material_requirements_include_scrap(self):
        req = self.std.material_requirements("SF-BODY")
        self.assertEqual(req["RM-SS304"], Decimal("0.8160"))
        self.assertEqual(req["PT-THERMO"], Decimal("1.0000"))

    def test_card_lists_routing_by_cost_center(self):
        card = self.std.card("FG-KT100")
        centers = {line.cost_center for line in card.labor_lines}
        self.assertEqual(centers, {"CC-ASM", "CC-QA"})
        self.assertEqual(card.labor_cost, Decimal("2.8800"))

    def test_bom_cycle_detected(self):
        ds = load_dataset(DATA)
        ds.bom["SF-BODY"] = list(ds.bom["SF-BODY"]) + [
            BomLine(parent="SF-BODY", component="FG-KT100", qty_per=Decimal(1))
        ]
        with self.assertRaises(ValueError) as ctx:
            StandardCosts(ds).unit_cost("FG-KT100")
        self.assertIn("循环", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
