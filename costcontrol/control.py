"""控制规则引擎：把「算出来的成本」变成「该被关注的成本」。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .allocation import OverheadAllocation
from .models import Alert, Dataset, OrderCost, Severity
from .money import ZERO, q_money, safe_div


@dataclass(frozen=True)
class ControlThresholds:
    material_qty_tolerance: Decimal = Decimal("0.02")
    order_variance_tolerance: Decimal = Decimal("0.03")
    scrap_rate_tolerance: Decimal = Decimal("0.02")
    overhead_budget_tolerance: Decimal = Decimal("0.00")

    @classmethod
    def from_params(cls, params: dict[str, Decimal]) -> "ControlThresholds":
        default = cls()
        return cls(
            material_qty_tolerance=params.get(
                "material_qty_tolerance", default.material_qty_tolerance
            ),
            order_variance_tolerance=params.get(
                "order_variance_tolerance", default.order_variance_tolerance
            ),
            scrap_rate_tolerance=params.get(
                "scrap_rate_tolerance", default.scrap_rate_tolerance
            ),
            overhead_budget_tolerance=params.get(
                "overhead_budget_tolerance", default.overhead_budget_tolerance
            ),
        )


def evaluate(
    ds: Dataset,
    orders: list[OrderCost],
    overhead: OverheadAllocation,
    thresholds: ControlThresholds,
) -> list[Alert]:
    alerts: list[Alert] = []

    for cost in orders:
        # 1. 材料超耗：实际用量超过「标准用量 ×（1 + 容差）」
        for line in cost.material_lines:
            if line.std_qty <= 0 or line.qty <= 0:
                continue
            if line.qty <= line.std_qty * (1 + thresholds.material_qty_tolerance):
                continue
            alerts.append(
                Alert(
                    rule="MAT-OVERUSE",
                    severity=Severity.WARNING,
                    message=(
                        f"{cost.order_no} 领用 {line.material} {line.qty}，"
                        f"标准允许 {line.std_qty}，超耗 {line.qty - line.std_qty}"
                    ),
                    order_no=cost.order_no,
                    material=line.material,
                    amount=line.qty_variance,
                )
            )

        # 2. 工单成本超支
        if cost.std_total > 0:
            rate = safe_div(cost.variance_total, cost.std_total)
            if rate is not None and rate > thresholds.order_variance_tolerance:
                severity = (
                    Severity.CRITICAL
                    if rate > thresholds.order_variance_tolerance * 2
                    else Severity.WARNING
                )
                alerts.append(
                    Alert(
                        rule="ORDER-OVERSPEND",
                        severity=severity,
                        message=(
                            f"{cost.order_no} 实际成本 {cost.total_cost}，"
                            f"标准 {cost.std_total}，超支 {cost.variance_total}"
                            f"（{rate * 100:.2f}%）"
                        ),
                        order_no=cost.order_no,
                        amount=cost.variance_total,
                    )
                )

        # 3. 废品率
        output = cost.qty_completed + cost.qty_scrapped
        if output > 0 and cost.qty_scrapped > 0:
            scrap_rate = safe_div(cost.qty_scrapped, output) or ZERO
            if scrap_rate > thresholds.scrap_rate_tolerance:
                alerts.append(
                    Alert(
                        rule="SCRAP-RATE",
                        severity=Severity.WARNING,
                        message=(
                            f"{cost.order_no} 废品 {cost.qty_scrapped} / 产出 {output}"
                            f"，报废率 {scrap_rate * 100:.2f}%，"
                            f"废品损失 {cost.scrap_cost}"
                        ),
                        order_no=cost.order_no,
                        amount=cost.scrap_cost,
                    )
                )

        # 4. 半成品按标准成本结转
        if cost.used_standard_transfer:
            alerts.append(
                Alert(
                    rule="TRANSFER-STD",
                    severity=Severity.INFO,
                    message=f"{cost.order_no} 存在无本期实际成本的半成品，已按标准价结转",
                    order_no=cost.order_no,
                )
            )

        # 5. 完全没有标准成本，差异分析失去基准
        if cost.std_total == 0 and cost.total_cost > 0:
            alerts.append(
                Alert(
                    rule="NO-STANDARD",
                    severity=Severity.WARNING,
                    message=f"{cost.order_no} 无标准成本（BOM/工艺路线缺失），无法做差异分析",
                    order_no=cost.order_no,
                    amount=cost.total_cost,
                )
            )

    # 6. 制造费用未分配：有费用池但没有动因消耗
    for center, amount in overhead.unallocated.items():
        if amount > 0:
            alerts.append(
                Alert(
                    rule="OH-UNALLOCATED",
                    severity=Severity.CRITICAL,
                    message=(
                        f"{center} 发生制造费用 {amount}，但本期无工单消耗其动因，"
                        f"费用挂账未分配"
                    ),
                    cost_center=center,
                    amount=amount,
                )
            )

    # 7. 制造费用超预算
    for center, pool in overhead.pools.items():
        cc = ds.center(center)
        if pool == 0:
            continue
        limit = cc.overhead_budget * (1 + thresholds.overhead_budget_tolerance)
        if pool > limit:
            alerts.append(
                Alert(
                    rule="OH-OVERBUDGET",
                    severity=Severity.WARNING,
                    message=(
                        f"{center}（{cc.name}）实际费用 {pool} 超预算 {cc.overhead_budget}"
                        f"，超支 {q_money(pool - cc.overhead_budget)}"
                    ),
                    cost_center=center,
                    amount=q_money(pool - cc.overhead_budget),
                )
            )

    order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    alerts.sort(key=lambda a: (order[a.severity], -a.amount))
    return alerts
