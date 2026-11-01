"""记账凭证：把「算出来的成本」翻译成借贷分录。

成本模块和总账的接口就在这里：材料领用、人工归集、制造费用发生与分配、
完工入库、废品损失，每一笔都要能落到科目上，且借贷必须平衡。

科目映射刻意保持简单（见 ``accounts``），因为凭证的价值不在科目多，而在于
**每一分钱的去向都能被解释**：生产成本借方 = 材料 + 人工 + 制造费用，
贷方 = 完工入库 + 废品损失，余额 = 期末在产。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .accounts import ACCOUNTS, account, expense_account
from .models import Dataset, MaterialType
from .money import ZERO, q_money
from .run import CostRun


def period_end(period: str) -> date:
    """会计期间 → 期末日期。"""
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class JournalLine:
    account: str
    summary: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.debit < 0 or self.credit < 0:
            raise ValueError("分录金额不能为负数")
        if self.debit != 0 and self.credit != 0:
            raise ValueError("同一分录不能同时记借方和贷方")

    @property
    def amount(self) -> Decimal:
        return self.debit if self.debit != 0 else self.credit

    @property
    def side(self) -> str:
        return "借" if self.debit != 0 else "贷"


def dr(code: str, summary: str, amount: Decimal) -> JournalLine:
    return JournalLine(code, summary, debit=q_money(amount))


def cr(code: str, summary: str, amount: Decimal) -> JournalLine:
    return JournalLine(code, summary, credit=q_money(amount))


@dataclass
class JournalEntry:
    entry_no: str
    entry_date: date
    doc_type: str
    summary: str
    lines: list[JournalLine]

    @property
    def debit_total(self) -> Decimal:
        return q_money(sum((l.debit for l in self.lines), ZERO))

    @property
    def credit_total(self) -> Decimal:
        return q_money(sum((l.credit for l in self.lines), ZERO))

    @property
    def is_balanced(self) -> bool:
        return self.debit_total == self.credit_total and self.debit_total > 0


@dataclass
class TrialBalanceRow:
    code: str
    name: str
    opening: Decimal
    debit: Decimal
    credit: Decimal

    @property
    def closing(self) -> Decimal:
        return q_money(self.opening + self.debit - self.credit)

    @property
    def direction(self) -> str:
        return "借" if self.closing >= 0 else "贷"


@dataclass
class JournalBook:
    period: str
    entries: list[JournalEntry] = field(default_factory=list)

    def add(self, doc_type: str, summary: str, lines: list[JournalLine]) -> JournalEntry:
        entry = JournalEntry(
            entry_no=f"{self.period}-{len(self.entries) + 1:03d}",
            entry_date=period_end(self.period),
            doc_type=doc_type,
            summary=summary,
            lines=lines,
        )
        self.entries.append(entry)
        return entry

    @property
    def debit_total(self) -> Decimal:
        return q_money(sum((e.debit_total for e in self.entries), ZERO))

    @property
    def credit_total(self) -> Decimal:
        return q_money(sum((e.credit_total for e in self.entries), ZERO))

    @property
    def is_balanced(self) -> bool:
        return self.debit_total == self.credit_total

    def account_totals(self) -> dict[str, tuple[Decimal, Decimal]]:
        totals: dict[str, tuple[Decimal, Decimal]] = {}
        for entry in self.entries:
            for line in entry.lines:
                debit, credit = totals.get(line.account, (ZERO, ZERO))
                totals[line.account] = (debit + line.debit, credit + line.credit)
        return {code: (q_money(d), q_money(c)) for code, (d, c) in totals.items()}

    def trial_balance(self, opening: dict[str, Decimal] | None = None) -> list[TrialBalanceRow]:
        """试算平衡表：期初 + 本期发生 = 期末，借方为正。"""
        opening = opening or {}
        totals = self.account_totals()
        codes = set(totals) | set(opening)
        rows: list[TrialBalanceRow] = []
        for code in sorted(codes):
            debit, credit = totals.get(code, (ZERO, ZERO))
            rows.append(
                TrialBalanceRow(
                    code=code,
                    name=account(code).name,
                    opening=q_money(opening.get(code, ZERO)),
                    debit=debit,
                    credit=credit,
                )
            )
        return rows


def build_journal(run: CostRun) -> JournalBook:
    ds: Dataset = run.dataset
    sheet = run.sheet
    book = JournalBook(period=ds.period)

    # 1. 材料领用：借 生产成本，贷 原材料 / 自制半成品
    for cost in sheet.orders:
        raw = sum(
            (
                line.amount
                for line in cost.material_lines
                if ds.material(line.material).mtype is MaterialType.RAW
            ),
            ZERO,
        )
        semi = sum(
            (
                line.amount
                for line in cost.material_lines
                if ds.material(line.material).mtype is MaterialType.SEMI
            ),
            ZERO,
        )
        total = q_money(raw + semi)
        if total == 0:
            continue
        lines = [dr("5001", f"{cost.order_no} 领料", total)]
        if raw:
            lines.append(cr("1403", f"{cost.order_no} 原材料", raw))
        if semi:
            lines.append(cr("1406", f"{cost.order_no} 半成品转入", semi))
        book.add("MATERIAL", f"{cost.order_no} 材料领用", lines)

    # 2. 直接人工：借 生产成本，贷 应付职工薪酬
    for cost in sheet.orders:
        amount = q_money(sum((l.amount for l in cost.labor_lines), ZERO))
        if amount == 0:
            continue
        book.add(
            "LABOR",
            f"{cost.order_no} 直接人工",
            [dr("5001", f"{cost.order_no} 人工", amount), cr("2211", "计提工资", amount)],
        )

    # 3. 制造费用发生：借 制造费用，贷 银行存款 / 累计折旧 / 应付职工薪酬
    by_center: dict[str, dict[str, Decimal]] = {}
    for item in ds.overhead:
        target = expense_account(item.expense_type)
        by_center.setdefault(item.cost_center, {}).setdefault(target, ZERO)
        by_center[item.cost_center][target] += item.amount
    for center, funding in by_center.items():
        total = q_money(sum(funding.values(), ZERO))
        if total == 0:
            continue
        lines = [dr("5101", f"{center} 费用归集", total)]
        for code, amount in sorted(funding.items()):
            lines.append(cr(code, f"{center} {account(code).name}", amount))
        book.add("OVERHEAD", f"{center} 制造费用发生", lines)

    # 4. 制造费用分配：借 生产成本，贷 制造费用
    allocated_by_center: dict[str, Decimal] = {}
    for (center_code, _order), amount in run.overhead.allocated.items():
        allocated_by_center[center_code] = allocated_by_center.get(center_code, ZERO) + amount
    for center, amount in sorted(allocated_by_center.items()):
        amount = q_money(amount)
        if amount == 0:
            continue
        book.add(
            "OVERHEAD_ALLOC",
            f"{center} 制造费用分配",
            [dr("5001", f"{center} 分配转入", amount), cr("5101", f"{center} 费用分配", amount)],
        )

    # 5. 完工入库：借 库存商品 / 自制半成品，贷 生产成本
    for cost in sheet.orders:
        if cost.finished_cost <= 0:
            continue
        mtype = ds.material(cost.product).mtype
        target = "1405" if mtype is MaterialType.FINISHED else "1406"
        book.add(
            "COMPLETION",
            f"{cost.order_no} 完工入库",
            [
                dr(target, f"{cost.order_no} 完工入库", cost.finished_cost),
                cr("5001", f"{cost.order_no} 成本结转", cost.finished_cost),
            ],
        )

    return book


__all__ = [
    "ACCOUNTS",
    "JournalBook",
    "JournalEntry",
    "JournalLine",
    "TrialBalanceRow",
    "build_journal",
    "cr",
    "dr",
    "period_end",
]
