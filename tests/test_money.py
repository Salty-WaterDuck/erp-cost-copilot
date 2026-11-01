import unittest
from decimal import Decimal

from costcontrol.money import allocate, allocate_to_dict, pct, q_money, q_qty, safe_div


class MoneyTest(unittest.TestCase):
    def test_quantize_half_up(self):
        self.assertEqual(q_money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(q_money(Decimal("1.004")), Decimal("1.00"))
        self.assertEqual(q_qty("1.23456"), Decimal("1.2346"))

    def test_allocate_sums_exactly(self):
        total = Decimal("3900")
        weights = [Decimal("38"), Decimal("27"), Decimal("40"), Decimal("14")]
        parts = allocate(total, weights)
        self.assertEqual(sum(parts), total)
        self.assertEqual(len(parts), 4)

    def test_allocate_puts_remainder_on_largest_fraction(self):
        # 100 按 1:1:1 分，100/3 = 33.3333，余 1 分给第一个（小数部分并列时按下标）
        parts = allocate(Decimal("100"), [Decimal(1)] * 3)
        self.assertEqual(sum(parts), Decimal("100"))
        self.assertEqual(parts.count(Decimal("33.34")), 1)
        self.assertEqual(parts.count(Decimal("33.33")), 2)

    def test_allocate_zero_weight_rejected(self):
        with self.assertRaises(ZeroDivisionError):
            allocate(Decimal("100"), [Decimal(0), Decimal(0)])

    def test_allocate_negative_weight_rejected(self):
        with self.assertRaises(ValueError):
            allocate(Decimal("100"), [Decimal(1), Decimal(-1)])

    def test_allocate_to_dict(self):
        result = allocate_to_dict(Decimal("10"), {"a": Decimal(1), "b": Decimal(1)})
        self.assertEqual(result, {"a": Decimal("5.00"), "b": Decimal("5.00")})

    def test_safe_div(self):
        self.assertIsNone(safe_div(Decimal(1), Decimal(0)))
        self.assertEqual(safe_div(Decimal(10), Decimal(4)), Decimal("2.5"))

    def test_pct(self):
        self.assertEqual(pct(Decimal("25"), Decimal("200")), Decimal("12.50"))


if __name__ == "__main__":
    unittest.main()
