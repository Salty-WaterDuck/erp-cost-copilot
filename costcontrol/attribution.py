"""差异穿透：成品超支的 1 块钱，到底是谁造成的。

成本计算单能告诉你「成品工单超支 1,117.68」，但答不了「其中多少是总装
自己的、多少是壶身组件工单传下来的」。差异穿透沿着半成品领用关系往回追：

* 本期实际结转账与标准成本之差，就是上游工单传下来的差异（金额精确）；
* 再按上游工单的差异构成比例，把金额映射成「材料价差 / 量差 / 人工 / 费用」；
* 成品工单自身的材料、人工、费用差异单独列出。

这样一张表，回答的就是成本控制里最常被追问的那句：
**这笔超支该找谁？**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import MaterialType, OrderCost, VarianceKind
from .money import ZERO, q_money
from .run import CostRun

COMPONENTS: tuple[VarianceKind, ...] = (
    VarianceKind.MATERIAL_PRICE,
    VarianceKind.MATERIAL_QTY,
    VarianceKind.LABOR_RATE,
    VarianceKind.LABOR_EFFICIENCY,
    VarianceKind.OVERHEAD_SPENDING,
    VarianceKind.OVERHEAD_EFFICIENCY,
)

LABELS: dict[VarianceKind, str] = {
    VarianceKind.MATERIAL_PRICE: "材料价差",
    VarianceKind.MATERIAL_QTY: "材料量差",
    VarianceKind.LABOR_RATE: "人工工资率",
    VarianceKind.LABOR_EFFICIENCY: "人工效率",
    VarianceKind.OVERHEAD_SPENDING: "费用耗费",
    VarianceKind.OVERHEAD_EFFICIENCY: "费用效率",
}


def variance_components(cost: OrderCost) -> dict[VarianceKind, Decimal]:
    """一张工单的六项差异（含半成品价差，即上游传进来的部分）。"""
    return {
        VarianceKind.MATERIAL_PRICE: cost.material_price_variance,
        VarianceKind.MATERIAL_QTY: cost.material_qty_variance,
        VarianceKind.LABOR_RATE: cost.labor_rate_variance,
        VarianceKind.LABOR_EFFICIENCY: cost.labor_efficiency_variance,
        VarianceKind.OVERHEAD_SPENDING: cost.overhead_spending_variance,
        VarianceKind.OVERHEAD_EFFICIENCY: cost.overhead_efficiency_variance,
    }


def _scale(components: dict[VarianceKind, Decimal], target: Decimal) -> dict[VarianceKind, Decimal]:
    """按比例缩放差异构成，使合计恰好等于 target（尾差给绝对值最大的分项）。"""
    total = sum(components.values(), ZERO)
    if total == 0:
        return {key: ZERO for key in COMPONENTS}
    raw = {key: value * target / total for key, value in components.items()}
    biggest = max(raw, key=lambda key: abs(raw[key]))
    scaled = {key: q_money(value) for key, value in raw.items()}
    scaled[biggest] = q_money(
        target - sum((value for key, value in scaled.items() if key != biggest), ZERO)
    )
    return scaled


@dataclass
class UpstreamShare:
    order_no: str
    product: str
    name: str
    qty: Decimal
    amount: Decimal  # 传导到下游的差异金额
    components: dict[VarianceKind, Decimal] = field(default_factory=dict)


@dataclass
class VarianceTrace:
    order_no: str
    product: str
    name: str
    total: Decimal        # 工单总差异
    pass_through: Decimal  # 其中上游转入
    own_components: dict[VarianceKind, Decimal] = field(default_factory=dict)
    upstream: list[UpstreamShare] = field(default_factory=list)

    @property
    def own_total(self) -> Decimal:
        return q_money(sum(self.own_components.values(), ZERO))


class VarianceTracer:
    def __init__(self, run: CostRun) -> None:
        self.run = run
        self.ds = run.dataset
        self.producers: dict[str, list[OrderCost]] = {}
        for cost in run.sheet.orders:
            if cost.qty_completed > 0:
                self.producers.setdefault(cost.product, []).append(cost)

    def trace(self, order_no: str) -> VarianceTrace:
        cost = self.run.sheet.order(order_no)
        own = variance_components(cost)
        pass_through = ZERO
        upstream: dict[str, UpstreamShare] = {}

        for line in cost.material_lines:
            if self.ds.material(line.material).mtype is not MaterialType.SEMI:
                continue
            price_variance = line.price_variance
            if price_variance == 0:
                continue
            producers = self.producers.get(line.material, [])
            if not producers:
                continue
            pass_through += price_variance
            own[VarianceKind.MATERIAL_PRICE] -= price_variance

            total_qty = sum((p.qty_completed for p in producers), ZERO)
            for producer in producers:
                share = producer.qty_completed / total_qty if total_qty else ZERO
                amount = q_money(price_variance * share)
                producer_components = variance_components(producer)
                key = producer.order_no
                item = upstream.get(key)
                if item is None:
                    item = UpstreamShare(
                        order_no=producer.order_no,
                        product=producer.product,
                        name=self.ds.material(producer.product).name,
                        qty=ZERO,
                        amount=ZERO,
                    )
                    upstream[key] = item
                item.qty += line.qty * share
                item.amount = q_money(item.amount + amount)
                scaled = _scale(producer_components, amount)
                for component, value in scaled.items():
                    item.components[component] = q_money(
                        item.components.get(component, ZERO) + value
                    )

        return VarianceTrace(
            order_no=cost.order_no,
            product=cost.product,
            name=self.ds.material(cost.product).name,
            total=cost.variance_total,
            pass_through=q_money(pass_through),
            own_components=own,
            upstream=sorted(upstream.values(), key=lambda s: -abs(s.amount)),
        )

    def all(self) -> list[VarianceTrace]:
        return [self.trace(cost.order_no) for cost in self.run.sheet.orders]


__all__ = [
    "COMPONENTS",
    "LABELS",
    "UpstreamShare",
    "VarianceTrace",
    "VarianceTracer",
    "variance_components",
]
