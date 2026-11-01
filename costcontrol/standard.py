"""标准成本：BOM 逐层卷算、工艺路线标准工时、成本卡。

组装型制造业的 BOM 是多层的（成品 ← 半成品 ← 原材料），成本必须自底向上
逐层卷算。这里用带记忆化的递归实现，并做环检测 —— 现实中 BOM 维护错误导致
的循环引用并不罕见，必须在计算前报错而不是栈溢出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Dataset, MaterialType
from .money import ZERO, q_unit


@dataclass(frozen=True)
class StdMaterialLine:
    material: str
    qty: Decimal
    price: Decimal

    @property
    def amount(self) -> Decimal:
        # 标准成本卡是「单位成本」，保留 4 位小数；逐行取整到分会让
        # 16.492 变成 16.49，误差会沿着 BOM 一层层放大。
        return q_unit(self.qty * self.price)


@dataclass(frozen=True)
class StdConversionLine:
    """人工 / 制造费用标准：按成本中心归集标准工时与费率。"""

    cost_center: str
    hours: Decimal
    rate: Decimal

    @property
    def amount(self) -> Decimal:
        return q_unit(self.hours * self.rate)


@dataclass
class StandardCostCard:
    product: str
    material_lines: list[StdMaterialLine] = field(default_factory=list)
    labor_lines: list[StdConversionLine] = field(default_factory=list)
    overhead_lines: list[StdConversionLine] = field(default_factory=list)

    @property
    def material_cost(self) -> Decimal:
        return q_unit(sum((l.amount for l in self.material_lines), ZERO))

    @property
    def labor_cost(self) -> Decimal:
        return q_unit(sum((l.amount for l in self.labor_lines), ZERO))

    @property
    def overhead_cost(self) -> Decimal:
        return q_unit(sum((l.amount for l in self.overhead_lines), ZERO))

    @property
    def total(self) -> Decimal:
        return q_unit(self.material_cost + self.labor_cost + self.overhead_cost)


class StandardCosts:
    def __init__(self, ds: Dataset, prefer_master_price: bool = False) -> None:
        self.ds = ds
        # 自制件若维护了标准价（价格控制 S），优先用标准价而不是 BOM 卷算。
        # 适用于 BOM 只维护到关键件、标准成本由 ERP 成本估算维护的场景。
        self.prefer_master_price = prefer_master_price
        self.warnings: list[str] = []
        self._unit_cost: dict[str, Decimal] = {}
        self._depth: dict[str, int] = {}

    # ------------------------------------------------------------------
    # BOM 结构
    # ------------------------------------------------------------------
    def depth(self, code: str) -> int:
        """物料在 BOM 中的层级：外购件 0，半成品 1，成品 2……"""
        if code in self._depth:
            return self._depth[code]
        lines = self.ds.bom.get(code)
        if not lines:
            self._depth[code] = 0
            return 0
        value = 1 + max(self.depth(line.component) for line in lines)
        self._depth[code] = value
        return value

    def unit_cost(self, code: str) -> Decimal:
        """单位标准成本：外购件取标准价，自制件按 BOM 逐层卷算。"""
        return self._unit_cost_of(code, ())

    def _unit_cost_of(self, code: str, stack: tuple[str, ...]) -> Decimal:
        if code in self._unit_cost:
            return self._unit_cost[code]
        if code in stack:
            chain = " -> ".join((*stack, code))
            raise ValueError(f"BOM 存在循环引用：{chain}")

        lines = self.ds.bom.get(code)
        material = self.ds.material(code)
        if (
            self.prefer_master_price
            and material.mtype is not MaterialType.RAW
            and material.standard_price > 0
        ):
            cost = q_unit(material.standard_price)
        elif not lines:
            cost = q_unit(material.standard_price)
            if material.mtype is not MaterialType.RAW and cost == 0:
                self.warnings.append(
                    f"{code} 是自制件但既无 BOM 也无标准价，标准成本按 0 处理"
                )
        else:
            # 半成品按「完全成本」逐层滚入上层：材料 + 本工序人工 + 本工序制造费用。
            # 这样上层产成品的标准材料成本与「实际领用半成品的结转价」口径一致，
            # 差异分析才不会被口径差污染。
            material_cost = sum(
                (
                    line.effective_qty * self._unit_cost_of(line.component, (*stack, code))
                    for line in lines
                ),
                ZERO,
            )
            conversion_cost = sum(
                (
                    op.std_hours
                    * (
                        self.ds.center(op.cost_center).labor_rate
                        + self.ds.center(op.cost_center).overhead_rate
                    )
                    for op in self.ds.routing.get(code, [])
                ),
                ZERO,
            )
            cost = q_unit(material_cost + conversion_cost)
        self._unit_cost[code] = cost
        return cost

    # ------------------------------------------------------------------
    # 单位标准用量 / 工时
    # ------------------------------------------------------------------
    def material_requirements(self, product: str) -> dict[str, Decimal]:
        """单位产品的直接材料标准用量（含损耗），按料号汇总。"""
        result: dict[str, Decimal] = {}
        for line in self.ds.bom.get(product, []):
            result[line.component] = result.get(line.component, ZERO) + line.effective_qty
        return {k: v for k, v in result.items()}

    def hours(self, product: str) -> dict[str, Decimal]:
        """单位产品在各成本中心的标准工时。"""
        result: dict[str, Decimal] = {}
        for op in self.ds.routing.get(product, []):
            result[op.cost_center] = result.get(op.cost_center, ZERO) + op.std_hours
        return result

    # ------------------------------------------------------------------
    # 标准成本卡
    # ------------------------------------------------------------------
    def card(self, product: str) -> StandardCostCard:
        card = StandardCostCard(product=product)
        for line in self.ds.bom.get(product, []):
            card.material_lines.append(
                StdMaterialLine(
                    material=line.component,
                    qty=line.effective_qty,
                    price=self.unit_cost(line.component),
                )
            )
        for center, hours in self.hours(product).items():
            cc = self.ds.center(center)
            card.labor_lines.append(
                StdConversionLine(cost_center=center, hours=hours, rate=cc.labor_rate)
            )
            card.overhead_lines.append(
                StdConversionLine(cost_center=center, hours=hours, rate=cc.overhead_rate)
            )
        return card
