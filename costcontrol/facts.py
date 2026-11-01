"""规范事实（canonical facts）：ERP 导出与归因层之间的唯一契约。

ERP 厂商的字段名各不相同，但差异归因只关心一组稳定的维度与证据。接入层
（adapter）负责把 SAP / 用友 / 金蝶的导出翻译成 ``VarianceFact``，后面的
责任映射、AI 解释、报告都只认这个结构——换 ERP 只换 adapter。

每条事实强制带维度标签（缺失的维度留空并在报告里标注），并携带证据键值，
供责任映射规则声明「这条判断需要哪些证据」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .money import q_money
from .models import MaterialType
from .run import CostRun

COST_ELEMENTS: dict[str, str] = {
    "MAT-PRICE-VAR": "材料价差",
    "MAT-QTY-VAR": "材料量差",
    "LABOR-RATE-VAR": "人工工资率差异",
    "LABOR-EFF-VAR": "人工效率差异",
    "OH-SPEND-VAR": "制造费用耗费差异",
    "OH-EFF-VAR": "制造费用效率差异",
}


@dataclass(frozen=True)
class VarianceFact:
    """一条可归因的差异事实。金额为正表示超支，为负表示节约。"""

    fact_id: str
    period: str
    cost_element: str
    amount: Decimal
    order_no: str
    product: str
    material: str = ""
    material_type: str = ""
    cost_center: str = ""
    work_center: str = ""
    operation: str = ""
    supplier: str = ""
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return COST_ELEMENTS.get(self.cost_element, self.cost_element)

    @property
    def dimensions(self) -> dict[str, str]:
        return {
            "period": self.period,
            "cost_element": self.cost_element,
            "order_no": self.order_no,
            "product": self.product,
            "material": self.material,
            "material_type": self.material_type,
            "cost_center": self.cost_center,
            "work_center": self.work_center,
            "operation": self.operation,
            "supplier": self.supplier,
        }

    @property
    def missing_dimensions(self) -> list[str]:
        required = ["order_no", "product"]
        if self.cost_element.startswith("MAT-"):
            required += ["material"]
        if self.cost_element.startswith(("LABOR-", "OH-")):
            required += ["cost_center"]
        return [name for name in required if not self.dimensions.get(name)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "period": self.period,
            "cost_element": self.cost_element,
            "label": self.label,
            "amount": str(self.amount),
            **self.dimensions,
            "evidence": dict(self.evidence),
        }


def _org_value(org: dict[str, Any], group: str, key: str) -> str:
    return str((org.get(group, {}) or {}).get(key, "") or "")


def _primary_center(cost: Any) -> str:
    """工单的主要责任成本中心：工时最多的那个；没有工时则取费用中心。"""
    if cost.labor_lines:
        return max(cost.labor_lines, key=lambda line: line.hours).cost_center
    if cost.overhead_lines:
        return max(cost.overhead_lines, key=lambda line: line.amount).cost_center
    return ""


def build_variance_facts(
    run: CostRun,
    org: dict[str, Any] | None = None,
) -> list[VarianceFact]:
    """从成本计算结果生成规范差异事实；不改变任何金额。"""
    org = org or {}
    period = run.dataset.period
    facts: list[VarianceFact] = []
    counter = 0

    def add(
        cost_element: str,
        amount: Decimal,
        cost: Any,
        *,
        material: str = "",
        material_type: str = "",
        cost_center: str = "",
        evidence: dict[str, str] | None = None,
    ) -> None:
        nonlocal counter
        amount = q_money(amount)
        if amount == 0:
            return
        counter += 1
        supplier = _org_value(org, "suppliers", material) if material else ""
        work_center = _org_value(org, "work_centers", cost_center) if cost_center else ""
        facts.append(
            VarianceFact(
                fact_id=f"VF-{period}-{counter:03d}",
                period=period,
                cost_element=cost_element,
                amount=amount,
                order_no=cost.order_no,
                product=cost.product,
                material=material,
                material_type=material_type,
                cost_center=cost_center,
                work_center=work_center,
                supplier=supplier,
                evidence=evidence or {},
            )
        )

    for cost in run.sheet.orders:
        primary_center = _primary_center(cost)
        for line in cost.material_lines:
            material = run.dataset.material(line.material)
            upstream = ""
            if material.mtype is MaterialType.SEMI:
                upstream = "、".join(
                    order.order_no
                    for order in run.sheet.orders
                    if order.product == line.material and order.qty_completed > 0
                )
            base = {
                "material": line.material,
                "material_name": material.name,
                "material_type": material.mtype.value,
                "qty": str(line.qty),
                "std_qty": str(line.std_qty),
                "unit_price": str(line.unit_price),
                "std_price": str(line.std_price),
                "supplier": _org_value(org, "suppliers", line.material),
                "upstream_orders": upstream,
            }
            add(
                "MAT-PRICE-VAR",
                line.price_variance,
                cost,
                material=line.material,
                material_type=material.mtype.value,
                cost_center=primary_center,
                evidence=base,
            )
            add(
                "MAT-QTY-VAR",
                line.qty_variance,
                cost,
                material=line.material,
                material_type=material.mtype.value,
                cost_center=primary_center,
                evidence=base,
            )
        for line in cost.labor_lines:
            base = {
                "hours": str(line.hours),
                "std_hours": str(line.std_hours),
                "rate": str(line.rate),
                "std_rate": str(line.std_rate),
            }
            add(
                "LABOR-RATE-VAR",
                line.rate_variance,
                cost,
                cost_center=line.cost_center,
                evidence=base,
            )
            add(
                "LABOR-EFF-VAR",
                line.efficiency_variance,
                cost,
                cost_center=line.cost_center,
                evidence=base,
            )
        for line in cost.overhead_lines:
            base = {
                "hours": str(line.hours),
                "std_hours": str(line.std_hours),
                "amount": str(line.amount),
                "budgeted_at_actual": str(line.budgeted_at_actual),
            }
            add(
                "OH-SPEND-VAR",
                line.spending_variance,
                cost,
                cost_center=line.cost_center,
                evidence=base,
            )
            add(
                "OH-EFF-VAR",
                line.efficiency_variance,
                cost,
                cost_center=line.cost_center,
                evidence=base,
            )

    return facts


def facts_to_dicts(facts: list[VarianceFact]) -> list[dict[str, Any]]:
    return [fact.to_dict() for fact in facts]


__all__ = [
    "COST_ELEMENTS",
    "VarianceFact",
    "build_variance_facts",
    "facts_to_dicts",
]
