"""数据集加载与校验：把 JSON 主数据变成强类型领域对象。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import (
    BomLine,
    CostCenter,
    Dataset,
    LaborReport,
    Material,
    MaterialIssue,
    MaterialType,
    OpeningWip,
    OrderStatus,
    OverheadActual,
    ProductionOrder,
    RoutingOp,
)
from .money import q_money


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def load_dataset(path: str | Path) -> Dataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    materials = {
        m["code"]: Material(
            code=m["code"],
            name=m["name"],
            mtype=MaterialType(m["mtype"]),
            uom=m["uom"],
            standard_price=_d(m.get("standard_price", 0)),
            spec=m.get("spec", ""),
        )
        for m in raw["materials"]
    }

    cost_centers = {
        c["code"]: CostCenter(
            code=c["code"],
            name=c["name"],
            labor_rate=_d(c["labor_rate"]),
            overhead_budget=_d(c["overhead_budget"]),
            budget_hours=_d(c["budget_hours"]),
        )
        for c in raw["cost_centers"]
    }

    bom: dict[str, list[BomLine]] = {}
    for line in raw.get("bom", []):
        bom.setdefault(line["parent"], []).append(
            BomLine(
                parent=line["parent"],
                component=line["component"],
                qty_per=_d(line["qty_per"]),
                scrap_rate=_d(line.get("scrap_rate", 0)),
                note=line.get("note", ""),
            )
        )

    routing: dict[str, list[RoutingOp]] = {}
    for op in raw.get("routing", []):
        routing.setdefault(op["product"], []).append(
            RoutingOp(
                product=op["product"],
                seq=int(op["seq"]),
                cost_center=op["cost_center"],
                std_hours=_d(op["std_hours"]),
                operation=op.get("operation", ""),
            )
        )
    for ops in routing.values():
        ops.sort(key=lambda o: o.seq)

    orders = [
        ProductionOrder(
            order_no=o["order_no"],
            product=o["product"],
            qty_planned=_d(o["qty_planned"]),
            qty_completed=_d(o.get("qty_completed", 0)),
            qty_scrapped=_d(o.get("qty_scrapped", 0)),
            wip_progress=_d(o.get("wip_progress", 0)),
            material_completion=_d(o.get("material_completion", 1)),
            status=OrderStatus(o.get("status", "RELEASED")),
            start_date=_date(o.get("start_date")),
            due_date=_date(o.get("due_date")),
        )
        for o in raw["orders"]
    ]

    issues = [
        MaterialIssue(
            doc_no=i["doc_no"],
            order_no=i["order_no"],
            material=i["material"],
            qty=_d(i["qty"]),
            unit_price=_d(i["unit_price"]),
            issue_date=_date(i.get("issue_date")),
            is_return=bool(i.get("is_return", False)),
        )
        for i in raw.get("issues", [])
    ]

    labor = [
        LaborReport(
            doc_no=r["doc_no"],
            order_no=r["order_no"],
            cost_center=r["cost_center"],
            hours=_d(r["hours"]),
            hourly_rate=_d(r["hourly_rate"]) if r.get("hourly_rate") is not None else None,
            work_date=_date(r.get("work_date")),
            operation=r.get("operation", ""),
        )
        for r in raw.get("labor", [])
    ]

    overhead = [
        OverheadActual(
            cost_center=o["cost_center"],
            expense_type=o["expense_type"],
            amount=_d(o["amount"]),
            period=o.get("period", raw.get("period", "")),
            doc_no=o.get("doc_no", ""),
        )
        for o in raw.get("overhead", [])
    ]

    params = {k: _d(v) for k, v in raw.get("params", {}).items()}

    opening: dict[str, Decimal] = {}
    opening_raw = raw.get("opening_balances", {})
    for code, amount in opening_raw.get("debit", {}).items():
        opening[code] = opening.get(code, Decimal(0)) + _d(amount)
    for code, amount in opening_raw.get("credit", {}).items():
        opening[code] = opening.get(code, Decimal(0)) - _d(amount)

    wip_opening = {
        w["order_no"]: OpeningWip(
            order_no=w["order_no"],
            product=w["product"],
            qty_planned=_d(w["qty_planned"]),
            qty_completed=_d(w.get("qty_completed", 0)),
            qty_in_wip=_d(w["qty_in_wip"]),
            material_completion=_d(w.get("material_completion", 1)),
            wip_progress=_d(w.get("wip_progress", 0)),
            cost_material=_d(w["cost_material"]),
            cost_labor=_d(w["cost_labor"]),
            cost_oh=_d(w["cost_oh"]),
            carried_from=w.get("carried_from", ""),
        )
        for w in raw.get("wip_opening", [])
    }

    ds = Dataset(
        company=raw["company"],
        period=raw["period"],
        cost_centers=cost_centers,
        materials=materials,
        bom=bom,
        routing=routing,
        orders=orders,
        issues=issues,
        labor=labor,
        overhead=overhead,
        params=params,
        opening_balances=opening,
        wip_opening=wip_opening,
    )
    validate(ds)
    return ds


def validate(ds: Dataset) -> None:
    """在计算前把脏数据挡掉，避免成本计算到一半才发现主数据缺失。"""
    from .accounts import ACCOUNTS

    for order in ds.orders:
        order.validate()
        if order.product not in ds.materials:
            raise ValueError(f"{order.order_no}: 产品 {order.product} 不存在")
        if ds.materials[order.product].mtype is MaterialType.RAW:
            raise ValueError(f"{order.order_no}: 原材料 {order.product} 不能作为产出物")

    for parent, lines in ds.bom.items():
        if parent not in ds.materials:
            raise ValueError(f"BOM 父项 {parent} 不存在")
        for line in lines:
            if line.component not in ds.materials:
                raise ValueError(f"BOM 子项 {line.component} 不存在")
            if line.qty_per <= 0:
                raise ValueError(f"BOM {parent}->{line.component}: 单位用量必须大于 0")
            if not (0 <= line.scrap_rate < 1):
                raise ValueError(f"BOM {parent}->{line.component}: 损耗率必须在 [0,1) 之间")

    order_nos = {o.order_no for o in ds.orders}
    for issue in ds.issues:
        if issue.order_no not in order_nos:
            raise ValueError(f"领料单 {issue.doc_no}: 工单 {issue.order_no} 不存在")
        if issue.material not in ds.materials:
            raise ValueError(f"领料单 {issue.doc_no}: 物料 {issue.material} 不存在")
        if issue.qty == 0:
            raise ValueError(f"领料单 {issue.doc_no}: 数量不能为 0")

    for report in ds.labor:
        if report.order_no not in order_nos:
            raise ValueError(f"报工单 {report.doc_no}: 工单 {report.order_no} 不存在")
        if report.cost_center not in ds.cost_centers:
            raise ValueError(f"报工单 {report.doc_no}: 成本中心 {report.cost_center} 不存在")
        if report.hours < 0:
            raise ValueError(f"报工单 {report.doc_no}: 工时不能为负")

    for item in ds.overhead:
        if item.cost_center not in ds.cost_centers:
            raise ValueError(f"制造费用 {item.doc_no}: 成本中心 {item.cost_center} 不存在")
        if item.amount < 0:
            raise ValueError(f"制造费用 {item.doc_no}: 金额不能为负")

    for product in ds.routing:
        if product not in ds.materials:
            raise ValueError(f"工艺路线 {product}: 物料不存在")
        for op in ds.routing[product]:
            if op.cost_center not in ds.cost_centers:
                raise ValueError(f"工艺路线 {product}: 成本中心 {op.cost_center} 不存在")

    unknown = set(ds.opening_balances) - set(ACCOUNTS)
    if unknown:
        raise ValueError(f"期初余额包含未知科目：{sorted(unknown)}")
    if sum(ds.opening_balances.values(), Decimal(0)) != 0:
        raise ValueError("期初余额借贷不平")

    by_no = {order.order_no: order for order in ds.orders}
    for code, opening in ds.wip_opening.items():
        opening.validate()
        order = by_no.get(code)
        if order is None:
            raise ValueError(f"期初在产 {code}: 本期工单不存在")
        if order.product != opening.product:
            raise ValueError(f"期初在产 {code}: 产品与工单不一致")
        if order.qty_planned != opening.qty_planned:
            raise ValueError(
                f"期初在产 {code}: 期初计划 {opening.qty_planned} 与工单计划 "
                f"{order.qty_planned} 不一致"
            )

    for order in ds.orders:
        if order.carried_from and order.order_no not in ds.wip_opening:
            raise ValueError(
                f"{order.order_no}: 跨期工单缺少期初在产记录（请先完成上期关账）"
            )

    if "5001" in ds.opening_balances:
        carried = q_money(
            sum((w.total_cost for w in ds.wip_opening.values()), Decimal(0))
        )
        if carried != ds.opening_balances["5001"]:
            raise ValueError(
                f"生产成本期初余额 {ds.opening_balances['5001']} 与期初在产合计 "
                f"{carried} 不一致"
            )
