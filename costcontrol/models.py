"""领域模型：主数据、业务单据、成本计算结果。

设计原则
--------
1. 主数据（物料 / BOM / 工艺路线 / 成本中心）与业务单据（工单 / 领料 /
   报工 / 实际制造费用）分离，前者是「标准」，后者是「实际」。
2. 成本计算结果是只读快照，重新计算不会改动原始单据 —— 这是 ERP 成本
   模块可追溯、可重算的前提。
3. 金额一律 Decimal，精度约定见 ``money`` 模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from .money import ZERO, q_money, q_qty, q_unit, safe_div


class MaterialType(str, Enum):
    RAW = "RAW"          # 外购原材料
    SEMI = "SEMI"        # 自制半成品
    FINISHED = "FINISHED"  # 产成品


class OrderStatus(str, Enum):
    DRAFT = "DRAFT"
    RELEASED = "RELEASED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class VarianceKind(str, Enum):
    MATERIAL_PRICE = "MATERIAL_PRICE"        # 材料价差
    MATERIAL_QTY = "MATERIAL_QTY"            # 材料量差
    LABOR_RATE = "LABOR_RATE"                # 人工工资率差异
    LABOR_EFFICIENCY = "LABOR_EFFICIENCY"    # 人工效率差异
    OVERHEAD_SPENDING = "OVERHEAD_SPENDING"  # 制造费用耗费差异
    OVERHEAD_EFFICIENCY = "OVERHEAD_EFFICIENCY"  # 制造费用效率差异


# --------------------------------------------------------------------------
# 主数据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Material:
    code: str
    name: str
    mtype: MaterialType
    uom: str
    standard_price: Decimal = ZERO  # 外购件标准价；自制件由 BOM 卷算
    spec: str = ""

    @property
    def is_purchased(self) -> bool:
        return self.mtype is MaterialType.RAW


@dataclass(frozen=True)
class BomLine:
    """一行 BOM：parent 由 component 组装而成。

    ``scrap_rate`` 是损耗率，表示「为产出 1 单位 parent，除净用量外还要多投
    的比例」，因此标准用量 = ``qty_per * (1 + scrap_rate)``。
    """

    parent: str
    component: str
    qty_per: Decimal
    scrap_rate: Decimal = ZERO
    note: str = ""

    @property
    def effective_qty(self) -> Decimal:
        return q_qty(self.qty_per * (1 + self.scrap_rate))


@dataclass(frozen=True)
class RoutingOp:
    """工艺路线一行：某产品在某个成本中心的标准工时。"""

    product: str
    seq: int
    cost_center: str
    std_hours: Decimal  # 单位标准工时（小时/件）
    operation: str = ""


@dataclass(frozen=True)
class CostCenter:
    code: str
    name: str
    labor_rate: Decimal      # 标准人工费率 元/小时
    overhead_budget: Decimal  # 期间制造费用预算
    budget_hours: Decimal     # 预算工时（用于标准费用分配率）

    @property
    def overhead_rate(self) -> Decimal:
        """标准制造费用分配率 = 预算费用 / 预算工时。"""
        ratio = safe_div(self.overhead_budget, self.budget_hours)
        return q_unit(ratio) if ratio is not None else ZERO


# --------------------------------------------------------------------------
# 业务单据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionOrder:
    order_no: str
    product: str
    qty_planned: Decimal
    qty_completed: Decimal = ZERO   # 良品完工入库
    qty_scrapped: Decimal = ZERO    # 报废（已消耗材料）
    wip_progress: Decimal = ZERO    # 在产完工程度（用于人工/费用约当量）
    material_completion: Decimal = Decimal("1")  # 在产投料程度
    carried_from: str = ""  # 跨期工单：来源期间，如 "2026-10"
    status: OrderStatus = OrderStatus.RELEASED
    start_date: date | None = None
    due_date: date | None = None

    @property
    def wip_qty(self) -> Decimal:
        return q_qty(self.qty_planned - self.qty_completed - self.qty_scrapped)

    @property
    def output_qty(self) -> Decimal:
        """本期实际产出的约当实物量（良品 + 废品）。"""
        return q_qty(self.qty_completed + self.qty_scrapped)

    def validate(self) -> None:
        if self.qty_planned <= 0:
            raise ValueError(f"{self.order_no}: 计划数量必须大于 0")
        if self.qty_completed < 0 or self.qty_scrapped < 0:
            raise ValueError(f"{self.order_no}: 完工/报废数量不能为负")
        if self.wip_qty < 0:
            raise ValueError(
                f"{self.order_no}: 完工 {self.qty_completed} + 报废 {self.qty_scrapped} "
                f"超过计划 {self.qty_planned}"
            )
        if not (0 <= self.wip_progress <= 1):
            raise ValueError(f"{self.order_no}: 在产完工程度必须在 0~1 之间")
        if not (0 <= self.material_completion <= 1):
            raise ValueError(f"{self.order_no}: 投料程度必须在 0~1 之间")


@dataclass(frozen=True)
class OpeningWip:
    """跨期工单的期初在产：上期末按成本项目沉淀下来的成本与进度。

    下期该工单的 ``qty_planned`` 等于这里的剩余量；本期归集与期初成本合并
    后按加权平均计算，期初数量**不再重复计入约当量分母**。
    """

    order_no: str
    product: str
    qty_planned: Decimal       # 本期计划（= 上期剩余量）
    qty_completed: Decimal     # 截至上期累计完工（追溯用）
    qty_in_wip: Decimal
    material_completion: Decimal
    wip_progress: Decimal
    cost_material: Decimal
    cost_labor: Decimal
    cost_oh: Decimal
    carried_from: str = ""

    @property
    def total_cost(self) -> Decimal:
        return q_money(self.cost_material + self.cost_labor + self.cost_oh)

    def validate(self) -> None:
        if self.qty_in_wip < 0:
            raise ValueError(f"{self.order_no}: 期初在产数量不能为负")
        if not (0 <= self.material_completion <= 1):
            raise ValueError(f"{self.order_no}: 期初投料程度必须在 0~1 之间")
        if not (0 <= self.wip_progress <= 1):
            raise ValueError(f"{self.order_no}: 期初完工程度必须在 0~1 之间")
        for value in (self.cost_material, self.cost_labor, self.cost_oh):
            if value < 0:
                raise ValueError(f"{self.order_no}: 期初在产成本不能为负")


@dataclass(frozen=True)
class MaterialIssue:
    """领料单。退料用负数数量，金额同步为负。"""

    doc_no: str
    order_no: str
    material: str
    qty: Decimal
    unit_price: Decimal
    issue_date: date | None = None
    is_return: bool = False

    @property
    def amount(self) -> Decimal:
        return q_money(self.qty * self.unit_price)


@dataclass(frozen=True)
class LaborReport:
    """报工单。``hourly_rate`` 为空时使用成本中心标准费率。"""

    doc_no: str
    order_no: str
    cost_center: str
    hours: Decimal
    hourly_rate: Decimal | None = None
    work_date: date | None = None
    operation: str = ""


@dataclass(frozen=True)
class OverheadActual:
    """实际制造费用发生额（期间费用池）。"""

    cost_center: str
    expense_type: str
    amount: Decimal
    period: str = ""
    doc_no: str = ""


# --------------------------------------------------------------------------
# 数据集
# --------------------------------------------------------------------------


@dataclass
class Dataset:
    company: str
    period: str
    cost_centers: dict[str, CostCenter]
    materials: dict[str, Material]
    bom: dict[str, list[BomLine]]
    routing: dict[str, list[RoutingOp]]
    orders: list[ProductionOrder]
    issues: list[MaterialIssue]
    labor: list[LaborReport]
    overhead: list[OverheadActual]
    params: dict[str, Decimal] = field(default_factory=dict)
    # 期初余额：借方为正、贷方为负，便于直接做试算平衡
    opening_balances: dict[str, Decimal] = field(default_factory=dict)
    # 期初在产：跨期工单在上期末沉淀的成本（按成本项目）
    wip_opening: dict[str, OpeningWip] = field(default_factory=dict)

    def material(self, code: str) -> Material:
        try:
            return self.materials[code]
        except KeyError:
            raise KeyError(f"物料 {code} 不存在") from None

    def center(self, code: str) -> CostCenter:
        try:
            return self.cost_centers[code]
        except KeyError:
            raise KeyError(f"成本中心 {code} 不存在") from None


# --------------------------------------------------------------------------
# 成本计算结果
# --------------------------------------------------------------------------


@dataclass
class MaterialCostLine:
    material: str
    qty: Decimal
    amount: Decimal
    std_qty: Decimal
    std_price: Decimal

    @property
    def unit_price(self) -> Decimal:
        return q_unit(safe_div(self.amount, self.qty) or ZERO)

    @property
    def std_amount(self) -> Decimal:
        return q_money(self.std_qty * self.std_price)

    @property
    def price_variance(self) -> Decimal:
        """价差 =（实际单价 - 标准单价）× 实际用量。"""
        return q_money((self.unit_price - self.std_price) * self.qty)

    @property
    def qty_variance(self) -> Decimal:
        """量差 =（实际用量 - 标准用量）× 标准单价。"""
        return q_money((self.qty - self.std_qty) * self.std_price)

    @property
    def variance(self) -> Decimal:
        return q_money(self.amount - self.std_amount)


@dataclass
class LaborCostLine:
    cost_center: str
    hours: Decimal
    amount: Decimal
    std_hours: Decimal
    std_rate: Decimal

    @property
    def rate(self) -> Decimal:
        return q_unit(safe_div(self.amount, self.hours) or ZERO)

    @property
    def std_amount(self) -> Decimal:
        return q_money(self.std_hours * self.std_rate)

    @property
    def rate_variance(self) -> Decimal:
        return q_money((self.rate - self.std_rate) * self.hours)

    @property
    def efficiency_variance(self) -> Decimal:
        return q_money((self.hours - self.std_hours) * self.std_rate)

    @property
    def variance(self) -> Decimal:
        return q_money(self.amount - self.std_amount)


@dataclass
class OverheadCostLine:
    cost_center: str
    hours: Decimal
    amount: Decimal
    std_hours: Decimal
    std_rate: Decimal

    @property
    def rate(self) -> Decimal:
        """实际分配率 = 实际费用 / 实际工时。"""
        return q_unit(safe_div(self.amount, self.hours) or ZERO)

    @property
    def std_amount(self) -> Decimal:
        return q_money(self.std_hours * self.std_rate)

    @property
    def spending_variance(self) -> Decimal:
        """耗费差异 = 实际费用 - 实际工时 × 标准费率（弹性预算下的应有费用）。"""
        return q_money(self.amount - self.budgeted_at_actual)

    @property
    def budgeted_at_actual(self) -> Decimal:
        """按实际工时 × 标准费率得到的「应有费用」。"""
        return q_money(self.std_rate * self.hours)

    @property
    def efficiency_variance(self) -> Decimal:
        return q_money((self.hours - self.std_hours) * self.std_rate)

    @property
    def variance(self) -> Decimal:
        return q_money(self.amount - self.std_amount)


@dataclass
class OrderCost:
    """一张工单的成本计算单。"""

    order_no: str
    product: str
    qty_planned: Decimal
    qty_completed: Decimal
    qty_scrapped: Decimal
    wip_qty: Decimal
    wip_progress: Decimal
    material_completion: Decimal = Decimal("1")

    material_lines: list[MaterialCostLine] = field(default_factory=list)
    labor_lines: list[LaborCostLine] = field(default_factory=list)
    overhead_lines: list[OverheadCostLine] = field(default_factory=list)

    material_cost: Decimal = ZERO
    labor_cost: Decimal = ZERO
    overhead_cost: Decimal = ZERO

    # 期初在产（按成本项目），非跨期工单为 0
    opening_material: Decimal = ZERO
    opening_labor: Decimal = ZERO
    opening_overhead: Decimal = ZERO
    qty_opening: Decimal = ZERO

    finished_cost: Decimal = ZERO   # 完工入库成本（含由良品负担的废品损失）
    scrap_cost: Decimal = ZERO      # 废品损失（供分析考核，已包含在完工成本内）
    wip_cost: Decimal = ZERO        # 期末在产品成本
    wip_material: Decimal = ZERO    # 期末在产中的材料 / 人工 / 费用
    wip_labor: Decimal = ZERO
    wip_oh: Decimal = ZERO
    unit_cost: Decimal = ZERO       # 完工产品单位成本

    std_total: Decimal = ZERO       # 期初标准 + 本期标准
    std_current: Decimal = ZERO     # 本期增量口径的标准（六项差异的基准）
    opening_std: Decimal = ZERO     # 期初在产对应的标准成本
    used_standard_transfer: bool = False  # 半成品按标准价结转（无本期实际成本）
    notes: list[str] = field(default_factory=list)

    @property
    def opening_cost(self) -> Decimal:
        return q_money(self.opening_material + self.opening_labor + self.opening_overhead)

    @property
    def current_cost(self) -> Decimal:
        """本期发生额（不含期初）。"""
        return q_money(self.material_cost + self.labor_cost + self.overhead_cost)

    @property
    def total_cost(self) -> Decimal:
        """工单总成本 = 期初在产 + 本期发生。"""
        return q_money(self.opening_cost + self.current_cost)

    @property
    def variance_total(self) -> Decimal:
        return q_money(self.total_cost - self.std_total)

    @property
    def variance_current(self) -> Decimal:
        """本期增量差异 = 本期发生 - 本期增量标准。"""
        return q_money(self.current_cost - self.std_current)

    @property
    def variance_opening(self) -> Decimal:
        """上期沉淀下来的差异 = 期初在产 - 期初标准。"""
        return q_money(self.opening_cost - self.opening_std)

    @property
    def variance_rate(self) -> Decimal | None:
        return safe_div(self.variance_total, self.std_total)

    @property
    def std_material_cost(self) -> Decimal:
        return q_money(sum((l.std_amount for l in self.material_lines), ZERO))

    @property
    def std_labor_cost(self) -> Decimal:
        return q_money(sum((l.std_amount for l in self.labor_lines), ZERO))

    @property
    def std_overhead_cost(self) -> Decimal:
        return q_money(sum((l.std_amount for l in self.overhead_lines), ZERO))

    @property
    def material_price_variance(self) -> Decimal:
        return q_money(sum((l.price_variance for l in self.material_lines), ZERO))

    @property
    def material_qty_variance(self) -> Decimal:
        return q_money(sum((l.qty_variance for l in self.material_lines), ZERO))

    @property
    def labor_rate_variance(self) -> Decimal:
        return q_money(sum((l.rate_variance for l in self.labor_lines), ZERO))

    @property
    def labor_efficiency_variance(self) -> Decimal:
        return q_money(sum((l.efficiency_variance for l in self.labor_lines), ZERO))

    @property
    def overhead_spending_variance(self) -> Decimal:
        return q_money(sum((l.spending_variance for l in self.overhead_lines), ZERO))

    @property
    def overhead_efficiency_variance(self) -> Decimal:
        return q_money(sum((l.efficiency_variance for l in self.overhead_lines), ZERO))


@dataclass
class Alert:
    rule: str
    severity: Severity
    message: str
    order_no: str = ""
    material: str = ""
    cost_center: str = ""
    amount: Decimal = ZERO

    def format(self) -> str:
        tag = {"INFO": "提示", "WARNING": "警告", "CRITICAL": "严重"}[self.severity.value]
        return f"[{tag}] {self.rule}: {self.message}"


@dataclass
class ProductSummary:
    product: str
    name: str
    qty_completed: Decimal
    qty_scrapped: Decimal
    finished_cost: Decimal
    std_unit_cost: Decimal
    unit_cost: Decimal
    variance: Decimal

    @property
    def variance_rate(self) -> Decimal | None:
        return safe_div(self.variance, self.std_unit_cost * self.qty_completed)


@dataclass
class CostSheet:
    company: str
    period: str
    orders: list[OrderCost]
    products: list[ProductSummary]
    alerts: list[Alert]

    def order(self, order_no: str) -> OrderCost:
        for o in self.orders:
            if o.order_no == order_no:
                return o
        raise KeyError(f"工单 {order_no} 不在本期成本计算单中")

    @property
    def material_cost(self) -> Decimal:
        return q_money(sum((o.material_cost for o in self.orders), ZERO))

    @property
    def labor_cost(self) -> Decimal:
        return q_money(sum((o.labor_cost for o in self.orders), ZERO))

    @property
    def overhead_cost(self) -> Decimal:
        return q_money(sum((o.overhead_cost for o in self.orders), ZERO))

    @property
    def opening_cost(self) -> Decimal:
        return q_money(sum((o.opening_cost for o in self.orders), ZERO))

    @property
    def current_cost(self) -> Decimal:
        return q_money(self.material_cost + self.labor_cost + self.overhead_cost)

    @property
    def total_cost(self) -> Decimal:
        return q_money(self.opening_cost + self.current_cost)

    @property
    def std_total(self) -> Decimal:
        return q_money(sum((o.std_total for o in self.orders), ZERO))

    @property
    def variance_total(self) -> Decimal:
        return q_money(self.total_cost - self.std_total)
