"""成本计算核心：归集 → 半成品逐级结转 → 约当产量 → 差异。

组装型制造业的成本难点集中在三处，本模块逐一处理：

1. **归集**：材料、人工、制造费用三条线各自回到工单。
2. **逐级结转**：成品领用半成品时，用的不是采购价，而是本期半成品工单的
   实际单位成本（全月一次加权平均）。因此必须按 BOM 层级自底向上计算，
   否则成品的材料成本会失真。
3. **完工与在产之间分配**：用约当产量法。装配型企业的材料通常在开工时
   一次性投入（投料程度 100%），人工和制造费用按完工程度折算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .allocation import OverheadAllocation
from .models import (
    Dataset,
    LaborCostLine,
    MaterialCostLine,
    MaterialType,
    OrderCost,
    OverheadCostLine,
    ProductSummary,
    ProductionOrder,
)
from .money import ZERO, q_money, q_qty, q_unit, safe_div
from .standard import StandardCosts


@dataclass
class _TransferPool:
    """本期某自制品的完工成本累计，用于计算加权平均结转单价。"""

    cost: Decimal = ZERO
    qty: Decimal = ZERO

    def add(self, cost: Decimal, qty: Decimal) -> None:
        self.cost += cost
        self.qty += qty

    @property
    def unit_cost(self) -> Decimal | None:
        if self.qty == 0:
            return None
        return q_unit(self.cost / self.qty)


@dataclass
class CostingResult:
    orders: list[OrderCost]
    transfer_pools: dict[str, _TransferPool] = field(default_factory=dict)


def _group_issues(ds: Dataset) -> dict[str, dict[str, tuple[Decimal, Decimal]]]:
    """工单 -> 物料 -> (净数量, 净金额)。退料为负，自动冲减。"""
    grouped: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    for issue in ds.issues:
        by_material = grouped.setdefault(issue.order_no, {})
        qty, amount = by_material.get(issue.material, (ZERO, ZERO))
        by_material[issue.material] = (qty + issue.qty, amount + issue.amount)
    return grouped


def _group_labor(ds: Dataset) -> dict[str, dict[str, tuple[Decimal, Decimal]]]:
    """工单 -> 成本中心 -> (工时, 人工金额)。报工单价缺省取成本中心标准费率。"""
    grouped: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    for report in ds.labor:
        rate = report.hourly_rate
        if rate is None:
            rate = ds.center(report.cost_center).labor_rate
        amount = q_money(report.hours * rate)
        by_center = grouped.setdefault(report.order_no, {})
        hours, total = by_center.get(report.cost_center, (ZERO, ZERO))
        by_center[report.cost_center] = (hours + report.hours, total + amount)
    return grouped


def equivalent_units(order: ProductionOrder) -> tuple[Decimal, Decimal]:
    """约当产量：(材料约当量, 加工约当量)。

    材料按投料程度折算（装配型通常开工一次性投料 = 100%），人工与制造费用
    按完工程度折算。标准用量也必须按约当量计算，否则「在产越多、差异越难看」。
    """
    output = order.output_qty
    material = output + order.wip_qty * order.material_completion
    conversion = output + order.wip_qty * order.wip_progress
    return q_qty(material), q_qty(conversion)


def _material_lines(
    order: ProductionOrder,
    ds: Dataset,
    std: StandardCosts,
    issues: dict[str, tuple[Decimal, Decimal]],
    transfer_pools: dict[str, _TransferPool],
    notes: list[str],
    eu_material: Decimal,
) -> tuple[list[MaterialCostLine], bool]:
    requirements = std.material_requirements(order.product)
    lines: list[MaterialCostLine] = []
    used_standard_transfer = False

    for code, (qty, amount) in sorted(issues.items()):
        qty = q_qty(qty)
        if qty == 0:
            continue
        material = ds.material(code)
        std_price = std.unit_cost(code)
        std_qty = q_qty(requirements.get(code, ZERO) * eu_material)

        if material.mtype is MaterialType.SEMI:
            pool = transfer_pools.get(code)
            actual = pool.unit_cost if pool else None
            if actual is None:
                # 本期没有该半成品的完工入库成本，只能按标准成本结转；
                # 同时用标准价冲平金额，避免凭空产生一笔「价差」。
                used_standard_transfer = True
                amount = q_money(qty * std_price)
                notes.append(f"{code} 本期无完工入库成本，按标准成本 {std_price} 结转")
            else:
                amount = q_money(qty * actual)

        lines.append(
            MaterialCostLine(
                material=code,
                qty=qty,
                amount=q_money(amount),
                std_qty=std_qty,
                std_price=std_price,
            )
        )

    # 标准允许用量里有、但本期没领的物料：说明漏领或用替代料，标准成本里仍要
    # 体现这部分缺口，差异才能反映「少领 = 有利」或「成本不完整」。
    for code, per_unit in requirements.items():
        if code in issues:
            continue
        std_qty = q_qty(per_unit * eu_material)
        if std_qty == 0:
            continue
        lines.append(
            MaterialCostLine(
                material=code,
                qty=ZERO,
                amount=ZERO,
                std_qty=std_qty,
                std_price=std.unit_cost(code),
            )
        )
        notes.append(f"{code} 有标准用量但本期未领料")

    return lines, used_standard_transfer


def _labor_lines(
    order: ProductionOrder,
    ds: Dataset,
    std: StandardCosts,
    labor: dict[str, tuple[Decimal, Decimal]],
    eu_conversion: Decimal,
) -> list[LaborCostLine]:
    std_hours_by_center = std.hours(order.product)
    centers = sorted(set(labor) | set(std_hours_by_center))
    lines: list[LaborCostLine] = []
    for center in centers:
        hours, amount = labor.get(center, (ZERO, ZERO))
        cc = ds.center(center)
        lines.append(
            LaborCostLine(
                cost_center=center,
                hours=q_qty(hours),
                amount=q_money(amount),
                std_hours=q_qty(std_hours_by_center.get(center, ZERO) * eu_conversion),
                std_rate=cc.labor_rate,
            )
        )
    return lines


def _overhead_lines(
    order: ProductionOrder,
    ds: Dataset,
    std: StandardCosts,
    overhead: OverheadAllocation,
    eu_conversion: Decimal,
) -> list[OverheadCostLine]:
    allocated = overhead.for_order(order.order_no)
    std_hours_by_center = std.hours(order.product)
    actual_hours: dict[str, Decimal] = {}
    for report in ds.labor:
        if report.order_no == order.order_no:
            actual_hours[report.cost_center] = (
                actual_hours.get(report.cost_center, ZERO) + report.hours
            )

    centers = sorted(set(allocated) | set(std_hours_by_center))
    lines: list[OverheadCostLine] = []
    for center in centers:
        cc = ds.center(center)
        lines.append(
            OverheadCostLine(
                cost_center=center,
                hours=q_qty(actual_hours.get(center, ZERO)),
                amount=q_money(allocated.get(center, ZERO)),
                std_hours=q_qty(std_hours_by_center.get(center, ZERO) * eu_conversion),
                std_rate=cc.overhead_rate,
            )
        )
    return lines


def _split_completed_and_wip(
    order: ProductionOrder,
    material_pool: Decimal,
    labor_pool: Decimal,
    overhead_pool: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """约当产量法把总成本拆成「完工入库 / 废品损失 / 期末在产」。

    成本池 = 期初在产 + 本期归集；约当量只按本期完工与期末在产折算，
    期初数量已包含在两者之中，不再重复计入分母。

    废品按已完工处理，其成本**由合格品负担**，完工入库成本包含废品损失。

    返回 (完工, 废品, 期末在产, 单位成本, 在产材料, 在产人工, 在产费用)。
    """
    output = order.output_qty
    eu_material, eu_conversion = equivalent_units(order)

    unit_material = safe_div(material_pool, eu_material) or ZERO
    unit_conversion = safe_div(labor_pool + overhead_pool, eu_conversion) or ZERO
    unit_total = unit_material + unit_conversion

    total = q_money(material_pool + labor_pool + overhead_pool)
    finished_cost = q_money(unit_total * output)
    wip_cost = q_money(total - finished_cost)
    scrap_cost = q_money(unit_total * order.qty_scrapped)
    unit_cost = q_unit(safe_div(finished_cost, order.qty_completed) or ZERO)

    # 期末在产按成本项目拆开，供下期期初结转
    wip_material = q_money(material_pool - unit_material * output)
    conversion_pool = labor_pool + overhead_pool
    conversion_finished = unit_conversion * output
    wip_conversion = q_money(conversion_pool - conversion_finished)
    wip_labor = (
        q_money(wip_conversion * labor_pool / conversion_pool)
        if conversion_pool
        else ZERO
    )
    wip_overhead = q_money(wip_conversion - wip_labor)
    wip_overhead += wip_cost - (wip_material + wip_labor + wip_overhead)

    return (
        finished_cost,
        scrap_cost,
        wip_cost,
        unit_cost,
        wip_material,
        wip_labor,
        wip_overhead,
    )


def cost_orders(ds: Dataset, std: StandardCosts, overhead: OverheadAllocation) -> CostingResult:
    issues = _group_issues(ds)
    labor = _group_labor(ds)

    # 按 BOM 层级从深到浅（叶子优先）计算，保证半成品成本先于成品成本确定。
    ordered = sorted(ds.orders, key=lambda o: (std.depth(o.product), o.order_no))

    pools: dict[str, _TransferPool] = {}
    results: list[OrderCost] = []

    for order in ordered:
        notes: list[str] = []
        opening = ds.wip_opening.get(order.order_no)
        eu_material, eu_conversion = equivalent_units(order)
        opening_material_qty = (
            opening.qty_in_wip * opening.material_completion if opening else ZERO
        )
        opening_conversion_qty = (
            opening.qty_in_wip * opening.wip_progress if opening else ZERO
        )
        # 本期增量约当量：差异分析只考核本期行为，期初已经发生的约当要扣掉
        eu_material_current = max(ZERO, eu_material - opening_material_qty)
        eu_conversion_current = max(ZERO, eu_conversion - opening_conversion_qty)

        material_lines, used_standard = _material_lines(
            order,
            ds,
            std,
            issues.get(order.order_no, {}),
            pools,
            notes,
            eu_material_current,
        )
        labor_lines = _labor_lines(
            order, ds, std, labor.get(order.order_no, {}), eu_conversion_current
        )
        overhead_lines = _overhead_lines(
            order, ds, std, overhead, eu_conversion_current
        )

        material_cost = q_money(sum((l.amount for l in material_lines), ZERO))
        labor_cost = q_money(sum((l.amount for l in labor_lines), ZERO))
        overhead_cost = q_money(sum((l.amount for l in overhead_lines), ZERO))

        open_material = opening.cost_material if opening else ZERO
        open_labor = opening.cost_labor if opening else ZERO
        open_overhead = opening.cost_oh if opening else ZERO

        (
            finished,
            scrap,
            wip,
            unit,
            wip_material,
            wip_labor,
            wip_overhead,
        ) = _split_completed_and_wip(
            order,
            material_cost + open_material,
            labor_cost + open_labor,
            overhead_cost + open_overhead,
        )

        std_current = q_money(
            sum((l.std_amount for l in material_lines), ZERO)
            + sum((l.std_amount for l in labor_lines), ZERO)
            + sum((l.std_amount for l in overhead_lines), ZERO)
        )
        opening_std = ZERO
        if opening is not None:
            card = std.card(order.product)
            opening_std = q_money(
                opening.qty_in_wip
                * opening.material_completion
                * card.material_cost
                + opening.qty_in_wip
                * opening.wip_progress
                * (card.labor_cost + card.overhead_cost)
            )
        std_total = q_money(opening_std + std_current)

        cost = OrderCost(
            order_no=order.order_no,
            product=order.product,
            qty_planned=order.qty_planned,
            qty_completed=order.qty_completed,
            qty_scrapped=order.qty_scrapped,
            wip_qty=order.wip_qty,
            wip_progress=order.wip_progress,
            material_completion=order.material_completion,
            material_lines=material_lines,
            labor_lines=labor_lines,
            overhead_lines=overhead_lines,
            material_cost=material_cost,
            labor_cost=labor_cost,
            overhead_cost=overhead_cost,
            opening_material=open_material,
            opening_labor=open_labor,
            opening_overhead=open_overhead,
            qty_opening=opening.qty_in_wip if opening else ZERO,
            finished_cost=finished,
            scrap_cost=scrap,
            wip_cost=wip,
            wip_material=wip_material,
            wip_labor=wip_labor,
            wip_oh=wip_overhead,
            unit_cost=unit,
            std_total=std_total,
            std_current=std_current,
            opening_std=opening_std,
            used_standard_transfer=used_standard,
            notes=notes,
        )
        results.append(cost)

        # 登记完工成本，供下游工单按加权平均实际成本结转（含期初沉淀成本）。
        if order.qty_completed > 0:
            pool = pools.setdefault(order.product, _TransferPool())
            pool.add(finished, order.qty_completed)

    return CostingResult(orders=results, transfer_pools=pools)


def summarize_products(
    ds: Dataset, orders: list[OrderCost], std: StandardCosts
) -> list[ProductSummary]:
    by_product: dict[str, list[OrderCost]] = {}
    for cost in orders:
        if cost.qty_completed > 0:
            by_product.setdefault(cost.product, []).append(cost)

    summaries: list[ProductSummary] = []
    for code in sorted(by_product):
        group = by_product[code]
        qty = sum((o.qty_completed for o in group), ZERO)
        finished = q_money(sum((o.finished_cost for o in group), ZERO))
        std_unit = std.card(code).total
        summaries.append(
            ProductSummary(
                product=code,
                name=ds.material(code).name,
                qty_completed=qty,
                qty_scrapped=q_qty(sum((o.qty_scrapped for o in group), ZERO)),
                finished_cost=finished,
                std_unit_cost=std_unit,
                unit_cost=q_unit(safe_div(finished, qty) or ZERO),
                variance=q_money(finished - std_unit * qty),
            )
        )
    return summaries


__all__ = [
    "CostingResult",
    "cost_orders",
    "summarize_products",
    "equivalent_units",
]
