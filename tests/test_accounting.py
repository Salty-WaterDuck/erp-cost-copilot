import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.accounting import build_journal, period_end
from costcontrol.money import q_money

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class JournalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.cost_run = run_cost(cls.ds)
        cls.book = build_journal(cls.cost_run)

    def test_every_entry_balanced(self):
        for entry in self.book.entries:
            with self.subTest(entry=entry.entry_no):
                self.assertTrue(entry.is_balanced)

    def test_book_balanced(self):
        self.assertTrue(self.book.is_balanced)
        self.assertGreater(self.book.debit_total, 0)

    def test_entry_numbers_sequential(self):
        numbers = [e.entry_no for e in self.book.entries]
        self.assertEqual(numbers, [f"2026-10-{i:03d}" for i in range(1, len(numbers) + 1)])

    def test_production_cost_balance_equals_wip(self):
        debit, credit = self.book.account_totals()["5001"]
        wip = sum((o.wip_cost for o in self.cost_run.sheet.orders), Decimal(0))
        self.assertEqual(q_money(debit - credit), q_money(wip))

    def test_finished_goods_receive_finished_cost(self):
        debit, _credit = self.book.account_totals()["1405"]
        expected = sum(
            (
                o.finished_cost
                for o in self.cost_run.sheet.orders
                if self.ds.material(o.product).mtype.value == "FINISHED"
            ),
            Decimal(0),
        )
        self.assertEqual(debit, q_money(expected))

    def test_semi_finished_account_nets_to_inventory(self):
        debit, credit = self.book.account_totals()["1406"]
        semi_finished = sum(
            (
                o.finished_cost
                for o in self.cost_run.sheet.orders
                if self.ds.material(o.product).mtype.value == "SEMI"
            ),
            Decimal(0),
        )
        semi_issued = sum(
            (
                line.amount
                for o in self.cost_run.sheet.orders
                for line in o.material_lines
                if self.ds.material(line.material).mtype.value == "SEMI"
            ),
            Decimal(0),
        )
        self.assertEqual(q_money(debit - credit), q_money(semi_finished - semi_issued))

    def test_depreciation_goes_to_accumulated_depreciation(self):
        # 设备折旧不应减少银行存款
        credits = {
            line.account: line.credit
            for entry in self.book.entries
            if entry.doc_type == "OVERHEAD"
            for line in entry.lines
        }
        self.assertGreater(credits.get("1602", Decimal(0)), 0)

    def test_trial_balance_sums_to_zero(self):
        rows = self.book.trial_balance(self.ds.opening_balances)
        self.assertEqual(q_money(sum((r.closing for r in rows), Decimal(0))), 0)
        self.assertEqual(
            q_money(sum((r.debit for r in rows), Decimal(0))),
            q_money(sum((r.credit for r in rows), Decimal(0))),
        )

    def test_opening_balance_kept(self):
        rows = {r.code: r for r in self.book.trial_balance(self.ds.opening_balances)}
        self.assertEqual(rows["1002"].opening, Decimal("500000.00"))
        self.assertEqual(rows["4001"].opening, Decimal("-615000.00"))

    def test_unbalanced_entry_rejected(self):
        from costcontrol.accounting import JournalLine

        with self.assertRaises(ValueError):
            JournalLine("1002", "进出同记", debit=Decimal(1), credit=Decimal(1))
        with self.assertRaises(ValueError):
            JournalLine("1002", "负数", debit=Decimal(-1))

    def test_period_end_handles_month_length(self):
        self.assertEqual(period_end("2026-02"), date(2026, 2, 28))
        self.assertEqual(period_end("2024-02"), date(2024, 2, 29))
        self.assertEqual(period_end("2026-10"), date(2026, 10, 31))


if __name__ == "__main__":
    unittest.main()
