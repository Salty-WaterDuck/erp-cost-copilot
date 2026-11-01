"""期末结账：把「能不能结账」变成一个可解释的检查清单。

结账不是点一下按钮，而是一组判定：账平不平、费用分没分完、在产留没留对、
凭证和成本计算单能不能对上。任何一条过不去，都应该拦住结账，而不是带病关账。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .accounting import JournalBook, JournalEntry, build_journal, cr, dr
from .control import ControlThresholds
from .models import OpeningWip, Severity
from .money import ZERO, q_money, safe_div
from .run import CostRun


def next_period(period: str) -> str:
    """2026-12 → 2027-01。"""
    year, month = (int(part) for part in period.split("-"))
    month += 1
    if month > 12:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


@dataclass
class CarryForward:
    """关账后带到下一期的东西：在产快照 + 总账期末余额。"""

    source_period: str
    period: str
    wip: list[OpeningWip]
    balances: dict[str, Decimal]

    @property
    def total_wip(self) -> Decimal:
        return q_money(sum((w.total_cost for w in self.wip), ZERO))


@dataclass
class CloseCheck:
    code: str
    severity: Severity
    message: str
    blocking: bool = False
    amount: Decimal = ZERO

    def format(self) -> str:
        tag = {"INFO": "提示", "WARNING": "警告", "CRITICAL": "严重"}[self.severity.value]
        flag = "阻断" if self.blocking else "允许"
        return f"[{tag}·{flag}] {self.code}: {self.message}"


@dataclass
class CloseReport:
    period: str
    checks: list[CloseCheck]
    journal: JournalBook
    adjustment: JournalEntry | None = None
    carry: CarryForward | None = None

    @property
    def blocking_checks(self) -> list[CloseCheck]:
        return [c for c in self.checks if c.blocking]

    @property
    def can_close(self) -> bool:
        return not self.blocking_checks


def close_period(
    run: CostRun,
    journal: JournalBook | None = None,
    strict: bool = False,
) -> CloseReport:
    """执行期末结账检查。

    ``strict=True`` 时，未分配制造费用会直接阻断结账；默认按实务做法把它们
    转入管理费用，并在检查清单里留痕。
    """
    ds = run.dataset
    book = journal or build_journal(run)
    checks: list[CloseCheck] = []
    adjustment: JournalEntry | None = None

    # 1. 凭证借贷平衡
    if book.is_balanced:
        checks.append(
            CloseCheck(
                "LEDGER-BALANCED",
                Severity.INFO,
                f"凭证借贷平衡，合计 {book.debit_total}",
            )
        )
    else:
        checks.append(
            CloseCheck(
                "LEDGER-UNBALANCED",
                Severity.CRITICAL,
                f"借贷不平：借方 {book.debit_total}，贷方 {book.credit_total}",
                blocking=True,
            )
        )

    # 2. 期初余额平衡
    opening_total = sum(ds.opening_balances.values(), ZERO)
    if opening_total == 0:
        debit_opening = q_money(
            sum((v for v in ds.opening_balances.values() if v > 0), ZERO)
        )
        checks.append(
            CloseCheck(
                "OPENING-BALANCED",
                Severity.INFO,
                f"期初余额平衡，借方合计 {debit_opening}",
            )
        )
    else:
        checks.append(
            CloseCheck(
                "OPENING-UNBALANCED",
                Severity.CRITICAL,
                f"期初余额借贷不平，差额 {opening_total}",
                blocking=True,
            )
        )

    # 3. 未分配制造费用：以制造费用科目余额为准，而不是重算费用池，
    #    这样重复调用结账也不会重复结转。
    overhead_debit, overhead_credit = book.account_totals().get("5101", (ZERO, ZERO))
    unallocated = max(ZERO, q_money(overhead_debit - overhead_credit))
    if unallocated > 0:
        if strict:
            checks.append(
                CloseCheck(
                    "OH-UNALLOCATED",
                    Severity.CRITICAL,
                    f"未分配制造费用 {unallocated} 未处理，严格模式禁止结账",
                    blocking=True,
                    amount=unallocated,
                )
            )
        else:
            adjustment = transfer_unallocated(run, book)
            assert adjustment is not None
            checks.append(
                CloseCheck(
                    "OH-UNALLOCATED",
                    Severity.WARNING,
                    f"未分配制造费用 {unallocated} 已转管理费用（凭证 {adjustment.entry_no}）",
                    amount=unallocated,
                )
            )
    else:
        checks.append(
            CloseCheck("OH-ALLOCATED", Severity.INFO, "制造费用已全部分配")
        )

    # 4. 期末在产
    wip = q_money(sum((o.wip_cost for o in run.sheet.orders), ZERO))
    checks.append(
        CloseCheck(
            "WIP-CARRIED",
            Severity.INFO,
            f"期末在产品成本 {wip}，保留在生产成本科目",
            amount=wip,
        )
    )

    # 5. 成本守恒：生产成本余额（含期初）必须等于期末在产
    cost_account = book.account_totals().get("5001", (ZERO, ZERO))
    opening_cost = ds.opening_balances.get("5001", ZERO)
    balance = q_money(opening_cost + cost_account[0] - cost_account[1])
    if balance == wip:
        checks.append(
            CloseCheck(
                "COST-CONSERVATION",
                Severity.INFO,
                f"生产成本余额 {balance}（期初 {opening_cost} + 本期）"
                f" = 期末在产 {wip}",
            )
        )
    else:
        checks.append(
            CloseCheck(
                "COST-CONSERVATION",
                Severity.CRITICAL,
                f"生产成本余额 {balance} 与期末在产 {wip} 不一致",
                blocking=True,
            )
        )

    # 6. 报废率
    thresholds = ControlThresholds.from_params(ds.params)
    completed = sum((o.qty_completed for o in run.sheet.orders), ZERO)
    scrapped = sum((o.qty_scrapped for o in run.sheet.orders), ZERO)
    scrap_rate = safe_div(scrapped, completed + scrapped)
    if scrap_rate is not None and scrap_rate > thresholds.scrap_rate_tolerance:
        checks.append(
            CloseCheck(
                "SCRAP-RATE",
                Severity.WARNING,
                f"本期综合报废率 {scrap_rate * 100:.2f}%，高于容差",
            )
        )

    # 7. 试算平衡
    rows = book.trial_balance(ds.opening_balances)
    closing_total = q_money(sum((r.closing for r in rows), ZERO))
    if closing_total == 0:
        checks.append(
            CloseCheck(
                "TRIAL-BALANCE",
                Severity.INFO,
                "试算平衡：期末余额借贷合计相等",
            )
        )
    else:
        checks.append(
            CloseCheck(
                "TRIAL-BALANCE",
                Severity.CRITICAL,
                f"试算不平衡，期末余额合计 {closing_total}",
                blocking=True,
            )
        )

    # 8. 期初结转对账：下期期初在产合计必须等于本期 5001 期末余额
    carry = build_carry(run, book)
    carry_total = carry.total_wip
    ledger_5001 = carry.balances.get("5001", ZERO)
    if carry_total == ledger_5001:
        checks.append(
            CloseCheck(
                "CARRY-RECONCILE",
                Severity.INFO,
                f"下期（{carry.period}）期初在产 {carry_total}，"
                f"共 {len(carry.wip)} 张工单，与 5001 期末余额一致",
            )
        )
    else:
        checks.append(
            CloseCheck(
                "CARRY-RECONCILE",
                Severity.CRITICAL,
                f"下期期初在产 {carry_total} 与 5001 期末余额 {ledger_5001} 不一致",
                blocking=True,
            )
        )

    return CloseReport(
        period=ds.period,
        checks=checks,
        journal=book,
        adjustment=adjustment,
        carry=carry,
    )


def transfer_unallocated(run: CostRun, book: JournalBook) -> JournalEntry | None:
    """把制造费用科目余额转入管理费用；余额为 0 时返回 None。"""
    debit, credit = book.account_totals().get("5101", (ZERO, ZERO))
    amount = max(ZERO, q_money(debit - credit))
    if amount == 0:
        return None
    return book.add(
        "CLOSING",
        "未分配制造费用转管理费用",
        [
            dr("6602", "未分配制造费用转入", amount),
            cr("5101", "未分配制造费用结转", amount),
        ],
    )


def build_carry(run: CostRun, journal: JournalBook | None = None) -> CarryForward:
    """把本期末的未完工工单与总账余额整理成下一期的期初。"""
    ds = run.dataset
    if journal is None:
        book = build_journal(run)
        transfer_unallocated(run, book)
    else:
        book = journal
    rows = book.trial_balance(ds.opening_balances)
    balances = {r.code: r.closing for r in rows if r.closing != 0}

    wip: list[OpeningWip] = []
    for cost in run.sheet.orders:
        if cost.wip_qty <= 0:
            continue
        opening = ds.wip_opening.get(cost.order_no)
        cumulative = (opening.qty_completed if opening else ZERO) + cost.qty_completed
        wip.append(
            OpeningWip(
                order_no=cost.order_no,
                product=cost.product,
                qty_planned=cost.wip_qty,
                qty_completed=cumulative,
                qty_in_wip=cost.wip_qty,
                material_completion=cost.material_completion,
                wip_progress=cost.wip_progress,
                cost_material=cost.wip_material,
                cost_labor=cost.wip_labor,
                cost_oh=cost.wip_oh,
                carried_from=ds.period,
            )
        )
    return CarryForward(
        source_period=ds.period,
        period=next_period(ds.period),
        wip=wip,
        balances=balances,
    )


def render_checks(report: CloseReport) -> str:
    lines = [f"期末结账检查 · {report.period}"]
    lines.append("=" * 40)
    for check in report.checks:
        lines.append(check.format())
    lines.append("=" * 40)
    lines.append(
        "结论：" + ("可以结账" if report.can_close else f"禁止结账（{len(report.blocking_checks)} 项阻断）")
    )
    return "\n".join(lines)
