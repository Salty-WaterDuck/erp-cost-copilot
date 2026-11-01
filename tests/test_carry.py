import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.close import build_carry, close_period, next_period
from costcontrol.money import q_money

ROOT = Path(__file__).resolve().parent.parent
OCT = ROOT / "data" / "demo_ningbo.json"
NOV = ROOT / "data" / "demo_ningbo_2026_11.json"


class PeriodTest(unittest.TestCase):
    def test_next_period(self):
        self.assertEqual(next_period("2026-10"), "2026-11")
        self.assertEqual(next_period("2026-12"), "2027-01")


class OctoberCarryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oct_run = run_cost(load_dataset(OCT))
        cls.carry = close_period(cls.oct_run).carry

    def test_two_orders_in_wip(self):
        self.assertIsNotNone(self.carry)
        self.assertEqual(self.carry.period, "2026-11")
        self.assertEqual(len(self.carry.wip), 2)
        self.assertEqual(self.carry.total_wip, Decimal("2345.47"))

    def test_wip_split_by_element(self):
        by_no = {w.order_no: w for w in self.carry.wip}
        po1004 = by_no["PO-1004"]
        self.assertEqual(po1004.cost_material, Decimal("2095.35"))
        self.assertEqual(po1004.cost_labor, Decimal("56.90"))
        self.assertEqual(po1004.cost_oh, Decimal("60.59"))
        self.assertEqual(po1004.total_cost, Decimal("2212.84"))

    def test_balances_chain(self):
        self.assertEqual(sum(self.carry.balances.values(), Decimal(0)), 0)
        self.assertEqual(self.carry.balances["5001"], self.carry.total_wip)
        self.assertNotIn("5101", self.carry.balances)  # 已转管理费用
        self.assertEqual(self.carry.balances["6602"], Decimal("260.00"))


class TwoPeriodConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oct_run = run_cost(load_dataset(OCT))
        cls.carry = build_carry(cls.oct_run)
        cls.nov = load_dataset(NOV)
        cls.nov_run = run_cost(cls.nov)

    def test_november_opening_matches_october_close(self):
        self.assertEqual(self.nov.opening_balances, self.carry.balances)
        self.assertEqual(self.nov.wip_opening, {w.order_no: w for w in self.carry.wip})

    def test_opening_cost_included_in_total(self):
        cost = self.nov_run.sheet.order("PO-1004")
        self.assertEqual(cost.opening_cost, Decimal("2212.84"))
        self.assertEqual(
            cost.total_cost, q_money(cost.opening_cost + cost.current_cost)
        )
        self.assertGreater(cost.total_cost, cost.current_cost)

    def test_opening_not_double_counted_in_standard(self):
        cost = self.nov_run.sheet.order("PO-1004")
        # 期初标准 + 本期增量标准 = 47 件的全量标准
        self.assertEqual(cost.std_total, Decimal("2296.14"))
        self.assertEqual(
            cost.std_total, q_money(cost.opening_std + cost.std_current)
        )
        self.assertAlmostEqual(
            float(cost.std_current), float(Decimal("165.82")), places=2
        )

    def test_variance_splits_into_opening_and_current(self):
        for cost in self.nov_run.sheet.orders:
            if cost.opening_cost == 0:
                continue
            with self.subTest(order=cost.order_no):
                self.assertLessEqual(
                    abs(
                        q_money(cost.variance_opening + cost.variance_current)
                        - cost.variance_total
                    ),
                    Decimal("0.01"),
                )

    def test_conservation_with_opening(self):
        for cost in self.nov_run.sheet.orders:
            with self.subTest(order=cost.order_no):
                self.assertEqual(
                    q_money(cost.finished_cost + cost.wip_cost), cost.total_cost
                )

    def test_wip_snapshot_matches_carry_after_november(self):
        carry = close_period(self.nov_run).carry
        self.assertEqual(carry.period, "2026-12")
        self.assertEqual(carry.total_wip, Decimal("312.63"))
        self.assertEqual(len(carry.wip), 1)
        self.assertEqual(carry.wip[0].order_no, "PO-1101")

    def test_missing_opening_is_rejected(self):
        ds = load_dataset(NOV)
        ds.wip_opening.pop("PO-1004")
        from costcontrol.dataset import validate

        with self.assertRaises(ValueError):
            validate(ds)

    def test_5001_mismatch_is_rejected(self):
        ds = load_dataset(NOV)
        ds.opening_balances["5001"] = Decimal("9999.99")
        from costcontrol.dataset import validate

        with self.assertRaises(ValueError):
            validate(ds)


class CarriedOrderUnitCostTest(unittest.TestCase):
    def test_completed_carry_reads_prior_cost(self):
        run = run_cost(load_dataset(NOV))
        cost = run.sheet.order("PO-1002")
        self.assertEqual(cost.qty_opening, Decimal("8.0000"))
        self.assertEqual(cost.opening_cost, Decimal("132.63"))
        # 期初 132.63 + 本期 12.61 = 145.24，单位成本 18.155
        self.assertEqual(cost.total_cost, Decimal("145.24"))
        self.assertEqual(cost.unit_cost, Decimal("18.1550"))


if __name__ == "__main__":
    unittest.main()
