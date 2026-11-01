import unittest
from decimal import Decimal
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.accounting import build_journal
from costcontrol.close import close_period
from costcontrol.money import q_money

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class CloseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DATA)
        cls.cost_run = run_cost(cls.ds)

    def test_default_close_transfers_unallocated_overhead(self):
        report = close_period(self.cost_run)
        self.assertTrue(report.can_close)
        self.assertIsNotNone(report.adjustment)
        self.assertEqual(report.adjustment.doc_type, "CLOSING")

        rows = {r.code: r for r in report.journal.trial_balance(self.ds.opening_balances)}
        self.assertEqual(rows["5101"].closing, 0)
        self.assertEqual(rows["6602"].closing, Decimal("260.00"))

    def test_strict_close_blocked_by_unallocated_overhead(self):
        report = close_period(self.cost_run, strict=True)
        self.assertFalse(report.can_close)
        self.assertEqual(len(report.blocking_checks), 1)
        self.assertEqual(report.blocking_checks[0].code, "OH-UNALLOCATED")
        self.assertIsNone(report.adjustment)

    def test_production_cost_equals_wip(self):
        report = close_period(self.cost_run)
        rows = {r.code: r for r in report.journal.trial_balance(self.ds.opening_balances)}
        wip = sum((o.wip_cost for o in self.cost_run.sheet.orders), Decimal(0))
        self.assertEqual(rows["5001"].closing, q_money(wip))

    def test_close_keeps_journal_balanced(self):
        report = close_period(self.cost_run)
        self.assertTrue(report.journal.is_balanced)

    def test_trial_balance_after_close(self):
        report = close_period(self.cost_run)
        rows = report.journal.trial_balance(self.ds.opening_balances)
        self.assertEqual(q_money(sum((r.closing for r in rows), Decimal(0))), 0)

    def test_unbalanced_book_blocks_close(self):
        from costcontrol.accounting import JournalBook, JournalLine

        book = build_journal(self.cost_run)
        bad = JournalBook(period="2026-10", entries=list(book.entries))
        bad.add("MANUAL", "人为制造不平", [JournalLine("1002", "只借不贷", debit=Decimal("1.00"))])
        report = close_period(self.cost_run, journal=bad)
        self.assertFalse(report.can_close)
        self.assertIn("LEDGER-UNBALANCED", {c.code for c in report.blocking_checks})

    def test_close_is_idempotent(self):
        journal = build_journal(self.cost_run)
        first = close_period(self.cost_run, journal=journal)
        self.assertIsNotNone(first.adjustment)
        second = close_period(self.cost_run, journal=journal)
        self.assertIsNone(second.adjustment)
        self.assertTrue(second.can_close)
        rows = {r.code: r for r in second.journal.trial_balance(self.ds.opening_balances)}
        self.assertEqual(rows["5101"].closing, 0)
        self.assertEqual(rows["6602"].closing, Decimal("260.00"))


if __name__ == "__main__":
    unittest.main()
