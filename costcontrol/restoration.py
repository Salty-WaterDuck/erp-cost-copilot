"""成本还原：把逐步结转后的「综合成本」拆回原始成本项目。

逐步结转分步法下，产成品的「直接材料」里混着半成品的加工费：

    借 5001 生产成本   19,971.46
      贷 1406 自制半成品  17,338.96   ← 里面是壶身/底座的料 + 人工 + 费用
      贷 1403 原材料       2,632.50

财务报表要求按原始成本项目列示（材料多少、人工多少、制造费用多少），
而成本控制需要知道「成品成本里有多少是上游工序的加工费」。这两件事都要
把半成品成本一层层拆开，就是成本还原。

还原是精确的：每个上游工单的完工成本按「被领用数量 / 完工数量」的比例
分给下游，逐层递归，还原后的三项之和严格等于完工成本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import MaterialType, OrderCost
from .money import ZERO, q_money
from .run import CostRun


def finished_elements(cost: OrderCost) -> tuple[Decimal, Decimal, Decimal]:
    """工单完工成本中的（材料, 人工, 制造费用）三段，按约当量折算到完工部分。

    完工成本 = 单位完全成本 ×（完工 + 报废），而单位成本是用约当量算的，
    所以各成本项目要按 output/约当量 的比例从本期发生额里切出来。
    """
    output = cost.qty_completed + cost.qty_scrapped
    eu_material = output + cost.wip_qty * cost.material_completion
    eu_conversion = output + cost.wip_qty * cost.wip_progress

    material = cost.material_cost * output / eu_material if eu_material else ZERO
    labor = cost.labor_cost * output / eu_conversion if eu_conversion else ZERO
    overhead = cost.overhead_cost * output / eu_conversion if eu_conversion else ZERO

    # 三段之和与完工成本的尾差归入制造费用，保证严格相等。
    overhead += cost.finished_cost - (material + labor + overhead)
    return material, labor, overhead


@dataclass
class CostRestoration:
    order_no: str
    product: str
    name: str
    qty_completed: Decimal
    finished_cost: Decimal

    # 还原前（账面口径：材料里含半成品转入）
    book_material: Decimal
    book_labor: Decimal
    book_overhead: Decimal
    transfer_in: Decimal  # 其中半成品转入金额

    # 还原后（原始成本项目）
    material: Decimal
    labor: Decimal
    overhead: Decimal
    material_detail: dict[str, Decimal] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return q_money(self.material + self.labor + self.overhead)

    @property
    def material_rate(self) -> Decimal | None:
        return None if self.finished_cost == 0 else self.material / self.finished_cost


@dataclass
class _Buckets:
    materials: dict[str, Decimal] = field(default_factory=dict)
    labor: Decimal = ZERO
    overhead: Decimal = ZERO


class Restorer:
    def __init__(self, run: CostRun) -> None:
        self.run = run
        self.ds = run.dataset
        self.orders: dict[str, OrderCost] = {o.order_no: o for o in run.sheet.orders}
        self.producers: dict[str, list[OrderCost]] = {}
        for cost in run.sheet.orders:
            if cost.qty_completed > 0:
                self.producers.setdefault(cost.product, []).append(cost)

    def restore(self, order_no: str) -> CostRestoration:
        cost = self.orders[order_no]
        buckets = _Buckets()
        self._decompose(cost, cost.finished_cost, buckets, ())

        material = q_money(sum(buckets.materials.values(), ZERO))
        details = {k: q_money(v) for k, v in sorted(buckets.materials.items())}
        if details:
            biggest = max(details, key=lambda key: abs(details[key]))
            details[biggest] += material - q_money(sum(details.values(), ZERO))

        labor = q_money(buckets.labor)
        # 逐层取整的尾差归入制造费用，保证三项之和 = 完工成本
        overhead = q_money(cost.finished_cost - material - labor)

        book_material, book_labor, book_overhead = finished_elements(cost)
        output = cost.qty_completed + cost.qty_scrapped
        eu_material = output + cost.wip_qty * cost.material_completion
        transfer_in = q_money(
            sum(
                (
                    line.amount
                    for line in cost.material_lines
                    if self.ds.material(line.material).mtype is MaterialType.SEMI
                ),
                ZERO,
            )
            * (output / eu_material if eu_material else ZERO)
        )

        return CostRestoration(
            order_no=cost.order_no,
            product=cost.product,
            name=self.ds.material(cost.product).name,
            qty_completed=cost.qty_completed,
            finished_cost=cost.finished_cost,
            book_material=q_money(book_material),
            book_labor=q_money(book_labor),
            book_overhead=q_money(book_overhead),
            transfer_in=transfer_in,
            material=material,
            labor=labor,
            overhead=overhead,
            material_detail=details,
        )

    def all(self) -> list[CostRestoration]:
        return [
            self.restore(cost.order_no)
            for cost in self.run.sheet.orders
            if cost.finished_cost > 0
        ]

    # ------------------------------------------------------------------
    def _decompose(
        self,
        cost: OrderCost,
        amount: Decimal,
        buckets: _Buckets,
        visited: tuple[str, ...],
    ) -> None:
        if amount == 0:
            return
        if cost.finished_cost == 0:
            buckets.labor += amount
            return

        ratio = amount / cost.finished_cost
        _material_fin, labor_fin, overhead_fin = finished_elements(cost)
        output = cost.qty_completed + cost.qty_scrapped
        eu_material = output + cost.wip_qty * cost.material_completion

        produced = ZERO
        for line in cost.material_lines:
            line_fin = line.amount * (output / eu_material if eu_material else ZERO) * ratio
            producers = self.producers.get(line.material, [])
            is_semi = self.ds.material(line.material).mtype is MaterialType.SEMI
            if is_semi and producers and cost.order_no not in visited:
                total_qty = sum((p.qty_completed for p in producers), ZERO)
                for producer in producers:
                    share = producer.qty_completed / total_qty if total_qty else ZERO
                    part = line_fin * share
                    self._decompose(
                        producer, part, buckets, (*visited, cost.order_no)
                    )
                    produced += part
            else:
                buckets.materials[line.material] = (
                    buckets.materials.get(line.material, ZERO) + line_fin
                )
                produced += line_fin

        buckets.labor += labor_fin * ratio
        buckets.overhead += overhead_fin * ratio
        produced += (labor_fin + overhead_fin) * ratio

        # 尾差归入制造费用，保证每次拆分后合计不变
        buckets.overhead += amount - produced


__all__ = [
    "CostRestoration",
    "Restorer",
    "finished_elements",
]
