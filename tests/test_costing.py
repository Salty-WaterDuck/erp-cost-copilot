import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.allocation import allocate_overhead
from costcontrol.money import q_money

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class AllocationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.alloc = allocate_overhead(cls.ds)

    def test_pool_collected_by_cost_center(self):
        self.assertEqual(self.alloc.pools["CC-ASM"], Decimal("3900.00"))
        self.assertEqual(self.alloc.pools["CC-QA"], Decimal("320.00"))
        self.assertEqual(self.alloc.pools["CC-MNT"], Decimal("260.00"))

    def test_allocated_plus_unallocated_equals_pools(self):
        total_pool = sum(self.alloc.pools.values())
        total_alloc = sum(self.alloc.allocated.values())
        self.assertEqual(total_alloc + self.alloc.unallocated_total, total_pool)

    def test_cost_center_without_driver_is_unallocated(self):
        # 设备维护组发生了费用，但没有工单报工消耗它的工时
        self.assertEqual(self.alloc.unallocated["CC-MNT"], Decimal("260.00"))
        self.assertNotIn("CC-MNT", {c for c, _ in self.alloc.allocated})

    def test_actual_rate_reflects_actual_pool(self):
        # 总装车间预算 3,600 / 120h = 30，实际 3,900 / 119h ≈ 32.7731
        self.assertEqual(self.alloc.driver_totals["CC-ASM"], Decimal("119"))
        self.assertEqual(self.alloc.rates["CC-ASM"], Decimal("32.7731"))

    def test_order_share_proportional(self):
        asm = {
            order: amount
            for (center, order), amount in self.alloc.allocated.items()
            if center == "CC-ASM"
        }
        self.assertAlmostEqual(
            float(asm["PO-1003"] / asm["PO-1002"]), 40 / 27, places=4
        )


class CostingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.cost_run = run_cost(cls.ds)
        cls.sheet = cls.cost_run.sheet

    def test_cost_conservation_per_order(self):
        for cost in self.sheet.orders:
            with self.subTest(order=cost.order_no):
                accounted = q_money(cost.finished_cost + cost.wip_cost)
                self.assertEqual(accounted, cost.total_cost)

    def test_variance_components_tie_to_total(self):
        # 各分项独立取整到分，允许每张工单 1 分以内的尾差
        for cost in self.sheet.orders:
            components = (
                cost.material_price_variance
                + cost.material_qty_variance
                + cost.labor_rate_variance
                + cost.labor_efficiency_variance
                + cost.overhead_spending_variance
                + cost.overhead_efficiency_variance
            )
            with self.subTest(order=cost.order_no):
                self.assertLessEqual(abs(components - cost.variance_total), Decimal("0.01"))

    def test_semi_finished_transferred_at_actual_cost(self):
        body = self.sheet.order("PO-1001")
        fg = self.sheet.order("PO-1003")
        line = next(l for l in fg.material_lines if l.material == "SF-BODY")
        self.assertEqual(line.unit_price, body.unit_cost)

    def test_transfer_price_differs_from_standard_when_upstream_overspends(self):
        fg = self.sheet.order("PO-1003")
        line = next(l for l in fg.material_lines if l.material == "SF-BODY")
        standard = self.cost_run.standard.unit_cost("SF-BODY")
        self.assertGreater(line.unit_price, standard)
        self.assertGreater(line.price_variance, 0)

    def test_standard_qty_uses_equivalent_units(self):
        # PO-1004 投料 200 件，完工 150 + 报废 3 + 在产 47，投料程度 100%
        # 因此材料标准允许量应为 200，而不是 153
        cost = self.sheet.order("PO-1004")
        line = next(l for l in cost.material_lines if l.material == "SF-BODY")
        self.assertEqual(line.std_qty, Decimal("200.0000"))
        self.assertEqual(line.qty_variance, Decimal("0.00"))

    def test_wip_carries_cost(self):
        cost = self.sheet.order("PO-1004")
        self.assertGreater(cost.wip_cost, 0)
        self.assertLess(cost.wip_cost, cost.total_cost)

    def test_scrap_cost_borne_by_good_units(self):
        cost = self.sheet.order("PO-1001")
        self.assertGreater(cost.scrap_cost, 0)
        # 废品损失计入完工成本，由良品负担；单位成本 = 完工成本 / 完工数量
        self.assertEqual(
            cost.unit_cost, (cost.finished_cost / cost.qty_completed).quantize(Decimal("0.0001"))
        )
        self.assertLess(cost.scrap_cost, cost.finished_cost)

    def test_product_summary_weighted_average(self):
        fg = next(p for p in self.sheet.products if p.product == "FG-KT100")
        self.assertEqual(fg.qty_completed, Decimal("600"))
        orders = [o for o in self.sheet.orders if o.product == "FG-KT100"]
        expected = sum((o.finished_cost for o in orders), Decimal(0)) / 600
        self.assertLessEqual(abs(fg.unit_cost - expected), Decimal("0.0001"))

    def test_finished_cost_of_fully_completed_order(self):
        cost = self.sheet.order("PO-1003")
        self.assertEqual(cost.wip_cost, Decimal("0.00"))
        self.assertEqual(cost.finished_cost, cost.total_cost)


class TransferFallbackTest(unittest.TestCase):
    def test_semi_without_actual_cost_falls_back_to_standard(self):
        ds = load_dataset(DATA)
        ds.orders = [o for o in ds.orders if o.order_no != "PO-1001"]
        run = run_cost(ds)
        cost = run.sheet.order("PO-1003")
        line = next(l for l in cost.material_lines if l.material == "SF-BODY")
        self.assertTrue(cost.used_standard_transfer)
        self.assertEqual(line.unit_price, run.standard.unit_cost("SF-BODY"))
        self.assertTrue(any("标准成本" in n for n in cost.notes))


if __name__ == "__main__":
    unittest.main()
