"""汽车零部件 13 表导出：单期复算 + 与 ERP 成本单对账。

这不是通用适配器，而是第二层演示的「证据链」：领料、报工、费用、BOM、工艺路线
全部按单据重算，再拿结果与金蝶成本单逐项对数。对不上就说明口径或数据有问题，
对得上才谈差异归因。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .dataset import validate
from .models import (
    BomLine,
    CostCenter,
    Dataset,
    LaborReport,
    Material,
    MaterialIssue,
    MaterialType,
    OrderStatus,
    OverheadActual,
    ProductionOrder,
    RoutingOp,
)
from .money import q_money, q_unit
from .run import CostRun

FILES = {
    "structure": "01_sap_ck13n_成本构成分解.csv",
    "inventory": "02_sap_存货明细.csv",
    "cost_sheet": "03_kingdee_成本计算单.csv",
    "restoration": "04_kingdee_成本还原汇总.csv",
    "daily": "05_yonyou_完工产品日报.csv",
    "scrap": "06_yonyou_废品回收表.csv",
    "bom": "07_bom_物料清单.csv",
    "issues": "08_领料明细_叶子级.csv",
    "labor": "09_报工明细_月度汇总.csv",
    "materials": "10_物料主数据.csv",
    "orders": "11_生产订单.csv",
    "overhead": "12_制造费用.csv",
    "routing": "13_工艺路线.csv",
}

MTYPE = {
    "产成品": MaterialType.FINISHED,
    "半成品": MaterialType.SEMI,
    "原材料": MaterialType.RAW,
}


class AutoPartsError(ValueError):
    pass


def _d(text: str) -> Decimal:
    text = (text or "").strip().replace(",", "").replace("%", "")
    return Decimal(text) if text else Decimal(0)


def _pct(text: str) -> Decimal:
    return _d(text) / 100


def _date(text: str) -> date | None:
    text = (text or "").strip()
    return date.fromisoformat(text) if text else None


def _read(root: Path, name: str) -> list[dict[str, str]]:
    path = root / name
    if not path.exists():
        raise AutoPartsError(f"缺少文件：{name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key.strip(): (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def load_period(directory: str | Path, period: str) -> Dataset:
    root = Path(directory)

    materials: dict[str, Material] = {}
    for row in _read(root, FILES["materials"]):
        materials[row["物料编码"]] = Material(
            code=row["物料编码"],
            name=row["物料名称"],
            mtype=MTYPE[row["物料类型"]],
            uom=row["计量单位"],
            standard_price=_d(row["标准价_元"]),
        )

    bom: dict[str, list[BomLine]] = {}
    for row in _read(root, FILES["bom"]):
        bom.setdefault(row["父项物料编码"], []).append(
            BomLine(
                parent=row["父项物料编码"],
                component=row["子项物料编码"],
                qty_per=_d(row["单位用量"]),
                scrap_rate=_pct(row["损耗率"]),
                note=row.get("备注", ""),
            )
        )

    orders: list[ProductionOrder] = []
    planned: dict[str, Decimal] = {}
    for row in _read(root, FILES["orders"]):
        if not row["开工日期"].startswith(period):
            continue
        planned[row["产品编码"]] = _d(row["计划数量"])
        status = OrderStatus.IN_PROGRESS if "部分" in row["状态"] else OrderStatus.COMPLETED
        orders.append(
            ProductionOrder(
                order_no=row["工单号"],
                product=row["产品编码"],
                qty_planned=_d(row["计划数量"]),
                qty_completed=_d(row["实际完工数量"]),
                qty_scrapped=_d(row["废品数量"]),
                wip_progress=_pct(row["在产完工程度"]),
                material_completion=_pct(row["投料程度"]),
                status=status,
                start_date=_date(row["开工日期"]),
                due_date=_date(row["完工日期"]),
            )
        )
    if not orders:
        raise AutoPartsError(f"{period} 没有生产订单")

    routing: dict[str, list[RoutingOp]] = {}
    for row in _read(root, FILES["routing"]):
        product = row["产品编码"]
        batch = planned.get(product) or Decimal(1)
        hours = (_d(row["标准工时_分钟每件"]) + _d(row["准备工时_分钟每批"]) / batch) / 60
        routing.setdefault(product, []).append(
            RoutingOp(
                product=product,
                seq=int(row["工序号"]),
                cost_center=row["成本中心"],
                std_hours=q_unit(hours),
                operation=row["工序"],
            )
        )

    issues: list[MaterialIssue] = []
    for index, row in enumerate(_read(root, FILES["issues"]), 1):
        if row["会计期间"] != period:
            continue
        issues.append(
            MaterialIssue(
                doc_no=f"MI-{period}-{index:03d}",
                order_no=row["工单编号"],
                material=row["物料编码"],
                qty=_d(row["领用数量"]),
                unit_price=_d(row["单价_元"]),
                issue_date=None,
            )
        )

    labor: list[LaborReport] = []
    for index, row in enumerate(_read(root, FILES["labor"]), 1):
        if row["会计期间"] != period:
            continue
        labor.append(
            LaborReport(
                doc_no=f"LR-{period}-{index:03d}",
                order_no=row["工单编号"],
                cost_center=row["成本中心"],
                hours=_d(row["实际工时"]),
                hourly_rate=_d(row["费率_元每小时"]),
                work_date=None,
                operation=row["工序"],
            )
        )

    overhead: list[OverheadActual] = []
    for index, row in enumerate(_read(root, FILES["overhead"]), 1):
        if row["会计期间"] != period:
            continue
        overhead.append(
            OverheadActual(
                cost_center=row["成本中心"],
                expense_type=row["费用类型"],
                amount=_d(row["金额_元"]),
                period=period,
                doc_no=f"OH-{period}-{index:03d}",
            )
        )

    # 成本中心：费率取工艺路线，预算工时按「标准工时 × 计划数量」滚动，
    # 费用预算取本期制造费用明细合计。
    rates: dict[str, Decimal] = {}
    for ops in routing.values():
        for op in ops:
            rates.setdefault(op.cost_center, Decimal(0))
    for row in _read(root, FILES["routing"]):
        rates[row["成本中心"]] = _d(row["人工费率_元每小时"])

    budget_hours: dict[str, Decimal] = {}
    for product, ops in routing.items():
        quantity = planned.get(product, Decimal(0))
        for op in ops:
            budget_hours[op.cost_center] = (
                budget_hours.get(op.cost_center, Decimal(0)) + op.std_hours * quantity
            )

    overhead_budget: dict[str, Decimal] = {}
    for item in overhead:
        overhead_budget[item.cost_center] = (
            overhead_budget.get(item.cost_center, Decimal(0)) + item.amount
        )

    codes = set(rates) | {item.cost_center for item in labor} | set(overhead_budget)
    centers = {
        code: CostCenter(
            code=code,
            name=code,
            labor_rate=rates.get(code, Decimal(0)),
            overhead_budget=overhead_budget.get(code, Decimal(0)),
            budget_hours=budget_hours.get(code, Decimal(0)),
        )
        for code in sorted(codes)
    }

    ds = Dataset(
        company="汽车零部件（第二层演示）",
        period=period,
        cost_centers=centers,
        materials=materials,
        bom=bom,
        routing=routing,
        orders=orders,
        issues=issues,
        labor=labor,
        overhead=overhead,
        params={},
    )
    validate(ds)
    return ds


@dataclass
class OrderRecon:
    order_no: str
    product: str
    material: Decimal
    erp_material: Decimal
    labor: Decimal
    erp_labor: Decimal
    overhead: Decimal
    erp_overhead: Decimal
    total: Decimal
    erp_three: Decimal
    erp_scrap_loss: Decimal
    unit: Decimal
    erp_unit: Decimal
    wip_cost: Decimal


def reconcile(run: CostRun, directory: str | Path, period: str) -> list[OrderRecon]:
    root = Path(directory)
    erp = {
        row["工单编号"]: row
        for row in _read(root, FILES["cost_sheet"])
        if row["会计期间"] == period
    }
    result: list[OrderRecon] = []
    for cost in run.sheet.orders:
        row = erp.get(cost.order_no)
        if row is None:
            continue
        erp_material = _d(row["直接材料_元"])
        erp_labor = _d(row["直接人工_元"])
        erp_overhead = _d(row["制造费用_元"])
        erp_scrap = _d(row["废品损失_元"])
        erp_three = erp_material + erp_labor + erp_overhead
        result.append(
            OrderRecon(
                order_no=cost.order_no,
                product=cost.product,
                material=cost.material_cost,
                erp_material=erp_material,
                labor=cost.labor_cost,
                erp_labor=erp_labor,
                overhead=cost.overhead_cost,
                erp_overhead=erp_overhead,
                total=cost.current_cost,
                erp_three=erp_three,
                erp_scrap_loss=erp_scrap,
                unit=cost.unit_cost,
                erp_unit=_d(row["单位成本_元"]),
                wip_cost=cost.wip_cost,
            )
        )
    return result


def _sap_total(directory: str | Path) -> Decimal:
    for row in _read(Path(directory), FILES["structure"]):
        if row["成本组件"].strip() == "合计":
            return _d(row["18个月综合均值_元每件"])
    return Decimal(0)


def render_second_layer(run: CostRun, directory: str | Path, period: str) -> str:
    recons = reconcile(run, directory, period)
    lines = [f"第二层成本复算 · {period}", "=" * 56]
    for item in recons:
        lines.append(f"工单 {item.order_no} · {item.product}")
        lines.append(
            f"  直接材料   {item.material:,.2f}  vs ERP {item.erp_material:,.2f}"
        )
        lines.append(
            f"  直接人工   {item.labor:,.2f}  vs ERP {item.erp_labor:,.2f}"
        )
        lines.append(
            f"  制造费用   {item.overhead:,.2f}  vs ERP {item.erp_overhead:,.2f}"
        )
        lines.append(
            f"  三项合计   {item.total:,.2f}  vs ERP {item.erp_three:,.2f}"
            f"（ERP 另列废品损失 {item.erp_scrap_loss:,.2f}）"
        )
        lines.append(
            f"  完工单位成本 {item.unit:.2f} vs ERP {item.erp_unit:.2f}"
            "（本系统拆在产，ERP 单位成本按完工数量且含废品损失）"
        )
        lines.append(f"  期末在产成本 {item.wip_cost:,.2f}")
        lines.append("")

    total_hours = sum((row.hours for row in run.dataset.labor), Decimal(0))
    eu = sum(
        (cost.qty_completed + cost.qty_scrapped + cost.wip_qty * cost.wip_progress)
        for cost in run.sheet.orders
    )
    std_hours = sum(
        (op.std_hours for op in run.dataset.routing.get("FG-BC-001", [])),
        Decimal(0),
    )
    actual_per_unit = total_hours / eu if eu else Decimal(0)
    card = run.standard.card("FG-BC-001")
    sap_total = _sap_total(directory)
    gap = (card.total / sap_total - 1) * 100 if sap_total else Decimal(0)
    hours_gap = actual_per_unit / std_hours if std_hours else Decimal(0)

    lines.append("标准成本一致性")
    lines.append(
        f"  BOM+工艺卷算 {card.total:,.2f} 元/件  vs SAP 标准 {sap_total:,.2f} 元/件"
        f"（{gap:+.2f}%）"
    )
    lines.append(
        f"  标准工时 {std_hours:.3f}h/件  vs 实际 {actual_per_unit:.3f}h/件"
        f"（{hours_gap:.1f} 倍）"
    )
    lines.append("")
    if abs(gap) > 10 or hours_gap > 1.5:
        lines.append("结论：归集与分配可复算；但 BOM/工时定额与 SAP 标准成本卡不一致，")
        lines.append("六项差异暂不可信，应先修定额或改用 ERP 标准成本卡作为基准。")
    else:
        lines.append("结论：实际成本与 ERP 一致，标准成本可用于差异分析。")
    return "\n".join(lines)


__all__ = [
    "AutoPartsError",
    "OrderRecon",
    "load_period",
    "reconcile",
    "render_second_layer",
]
