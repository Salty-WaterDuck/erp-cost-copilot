import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.attribution import VarianceTracer
from costcontrol.models import MaterialType
from costcontrol.money import ZERO, q_money
from costcontrol.restoration import Restorer, finished_elements

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class RestorationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.cost_run = run_cost(cls.ds)
        cls.restorer = Restorer(cls.cost_run)

    def test_restored_total_equals_finished_cost(self):
        items = self.restorer.all()
        self.assertEqual(len(items), 4)
        for item in items:
            with self.subTest(order=item.order_no):
                self.assertEqual(item.total, item.finished_cost)

    def test_material_detail_sums_to_material(self):
        for item in self.restorer.all():
            with self.subTest(order=item.order_no):
                self.assertEqual(
                    q_money(sum(item.material_detail.values(), ZERO)), item.material
                )

    def test_semi_finished_order_is_not_restored(self):
        # PO-1001 没有领用半成品，账面即原始
        item = self.restorer.restore("PO-1001")
        self.assertEqual(item.transfer_in, 0)
        self.assertEqual(item.material, item.book_material)
        self.assertEqual(item.labor, item.book_labor)
        self.assertEqual(item.overhead, item.book_overhead)

    def test_finished_order_moves_upstream_conversion_out_of_material(self):
        item = self.restorer.restore("PO-1003")
        self.assertGreater(item.transfer_in, 0)
        self.assertLess(item.material, item.book_material)
        self.assertGreater(item.labor, item.book_labor)
        self.assertGreater(item.overhead, item.book_overhead)

    def test_restoration_preserves_book_labor_plus_overhead_gap(self):
        item = self.restorer.restore("PO-1003")
        moved = item.book_material - item.material
        added = (item.labor - item.book_labor) + (item.overhead - item.book_overhead)
        self.assertLessEqual(abs(moved - added), Decimal("0.01"))

    def test_finished_elements_sum_to_finished_cost(self):
        for cost in self.cost_run.sheet.orders:
            material, labor, overhead = finished_elements(cost)
            with self.subTest(order=cost.order_no):
                self.assertEqual(
                    q_money(material + labor + overhead), cost.finished_cost
                )

    def test_wip_order_restores_only_finished_share(self):
        item = self.restorer.restore("PO-1004")
        self.assertEqual(item.total, item.finished_cost)
        self.assertLess(item.book_material, self.cost_run.sheet.order("PO-1004").material_cost)


class VarianceTraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.cost_run = run_cost(cls.ds)
        cls.tracer = VarianceTracer(cls.cost_run)

    def test_own_plus_pass_through_equals_total(self):
        for trace in self.tracer.all():
            with self.subTest(order=trace.order_no):
                self.assertLessEqual(
                    abs(trace.own_total + trace.pass_through - trace.total),
                    Decimal("0.01"),
                )

    def test_pass_through_is_semi_price_variance(self):
        cost = self.cost_run.sheet.order("PO-1003")
        expected = q_money(
            sum(
                (
                    line.price_variance
                    for line in cost.material_lines
                    if self.ds.material(line.material).mtype is MaterialType.SEMI
                ),
                ZERO,
            )
        )
        self.assertEqual(self.tracer.trace("PO-1003").pass_through, expected)

    def test_order_without_semi_has_no_pass_through(self):
        trace = self.tracer.trace("PO-1001")
        self.assertEqual(trace.pass_through, 0)
        self.assertEqual(trace.upstream, [])

    def test_upstream_components_sum_to_amount(self):
        trace = self.tracer.trace("PO-1003")
        self.assertEqual(len(trace.upstream), 2)
        for item in trace.upstream:
            self.assertEqual(q_money(sum(item.components.values(), ZERO)), item.amount)

    def test_upstream_are_the_producers(self):
        trace = self.tracer.trace("PO-1003")
        self.assertEqual({item.order_no for item in trace.upstream}, {"PO-1001", "PO-1002"})
        self.assertEqual(sum((item.qty for item in trace.upstream), ZERO), Decimal("900"))

    def test_total_pass_through_equals_sum_of_upstream(self):
        trace = self.tracer.trace("PO-1004")
        self.assertEqual(
            q_money(sum((item.amount for item in trace.upstream), ZERO)),
            trace.pass_through,
        )


class ThreeLevelBomTest(unittest.TestCase):
    """在演示数据上再加一层半成品，验证成本还原能逐层递归到底。"""

    def setUp(self):
        from costcontrol.models import (
            BomLine,
            Material,
            MaterialIssue,
            ProductionOrder,
            RoutingOp,
        )

        ds = load_dataset(DATA)
        ds.materials["SF-PLATE"] = Material(
            "SF-PLATE", "不锈钢片", MaterialType.SEMI, "件", Decimal("0")
        )
        ds.bom["SF-PLATE"] = [BomLine("SF-PLATE", "RM-SS304", Decimal("1"), Decimal("0"))]
        ds.bom["SF-BODY"] = [
            BomLine("SF-BODY", "SF-PLATE", Decimal("0.8"), Decimal("0.02"))
        ] + [line for line in ds.bom["SF-BODY"] if line.component != "RM-SS304"]
        ds.routing["SF-PLATE"] = [
            RoutingOp("SF-PLATE", 10, "CC-ASM", Decimal("0.01"), "下料")
        ]
        ds.orders.append(
            ProductionOrder(
                "PO-1000",
                "SF-PLATE",
                Decimal("595"),
                Decimal("595"),
                Decimal("0"),
                Decimal("0"),
            )
        )
        ds.issues = [
            issue
            for issue in ds.issues
            if not (issue.order_no == "PO-1001" and issue.material == "RM-SS304")
        ]
        ds.issues.append(
            MaterialIssue("MI-0900", "PO-1000", "RM-SS304", Decimal("595"), Decimal("12.30"))
        )
        ds.issues.append(
            MaterialIssue("MI-0901", "PO-1001", "SF-PLATE", Decimal("595"), Decimal("12.30"))
        )
        self.ds = ds
        self.cost_run = run_cost(ds)
        self.restorer = Restorer(self.cost_run)

    def test_depth_is_three(self):
        depths = {
            code: self.cost_run.standard.depth(code)
            for code in ("RM-SS304", "SF-PLATE", "SF-BODY", "FG-KT100")
        }
        self.assertEqual(depths["SF-PLATE"], 1)
        self.assertEqual(depths["SF-BODY"], 2)
        self.assertEqual(depths["FG-KT100"], 3)

    def test_restoration_reaches_raw_material(self):
        item = self.restorer.restore("PO-1003")
        self.assertIn("RM-SS304", item.material_detail)
        self.assertNotIn("SF-PLATE", item.material_detail)
        self.assertNotIn("SF-BODY", item.material_detail)
        self.assertEqual(item.total, item.finished_cost)

    def test_three_level_conversion_moves_to_labor_and_overhead(self):
        item = self.restorer.restore("PO-1003")
        self.assertGreater(item.labor, item.book_labor)
        self.assertGreater(item.overhead, item.book_overhead)


if __name__ == "__main__":
    unittest.main()
