"""CSV 适配器：把 ERP 导出翻译成规范数据，也能反向生成演示导出。

为什么需要它：真实 ERP（SAP / 用友 / 金蝶）的导出列名各不相同，但归因层只认
规范字段。适配器把「列名映射」抽到 YAML，换 ERP 只改配置，不改代码。

没有真实导出数据时，可以先用 ``write_export`` 把内置演示数据写成「ERP 导出
形态」的 CSV，再用 ``read_export`` 读回来——开发与测试不依赖任何厂商数据。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .dataset import validate
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
from .yaml_lite import load_file

DEFAULT_MAPPING = (
    Path(__file__).resolve().parent.parent
    / "config_templates"
    / "adapters"
    / "yonyou_u8.yaml"
)


class AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class ExportMapping:
    vendor: str
    label: str
    files: dict[str, str]
    columns: dict[str, dict[str, str]]
    values: dict[str, dict[str, str]]

    def header(self, entity: str, field_name: str) -> str:
        try:
            return self.columns[entity][field_name]
        except KeyError:
            raise AdapterError(f"映射缺少 {entity}.{field_name}") from None

    def reverse_values(self, group: str) -> dict[str, str]:
        return {
            canonical: label
            for label, canonical in (self.values.get(group, {}) or {}).items()
        }


def load_mapping(path: str | Path | None = None) -> ExportMapping:
    target = Path(path) if path else DEFAULT_MAPPING
    raw = load_file(target)
    return ExportMapping(
        vendor=str(raw.get("vendor", "unknown")),
        label=str(raw.get("label", "")),
        files={str(k): str(v) for k, v in (raw.get("files", {}) or {}).items()},
        columns={
            str(entity): {str(field): str(header) for field, header in cols.items()}
            for entity, cols in (raw.get("columns", {}) or {}).items()
        },
        values={
            str(group): {str(label): str(canonical) for label, canonical in items.items()}
            for group, items in (raw.get("values", {}) or {}).items()
        },
    )


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _write_table(
    path: Path,
    columns: dict[str, str],
    rows: list[dict[str, Any]],
    values: dict[str, dict[str, str]] | None = None,
) -> None:
    headers = list(columns.values())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            line = []
            for field in columns:
                value = row.get(field)
                if values and field in values and isinstance(value, str):
                    value = values[field].get(value, value)
                line.append(_fmt(value))
            writer.writerow(line)


def write_export(
    ds: Dataset,
    out_dir: str | Path,
    mapping: ExportMapping | None = None,
) -> Path:
    """把数据集写成 ERP 导出形态的 CSV 目录（用于演示与适配器测试）。"""
    mapping = mapping or load_mapping()
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    files = mapping.files

    def path_for(entity: str) -> Path:
        name = files.get(entity)
        if not name:
            raise AdapterError(f"映射缺少文件 {entity}")
        return target / name

    meta = [
        {"key": "公司", "value": ds.company},
        {"key": "期间", "value": ds.period},
        {"key": "货币", "value": "CNY"},
    ]
    _write_table(path_for("meta"), mapping.columns["meta"], meta)

    materials = [
        {
            "code": m.code,
            "name": m.name,
            "mtype": m.mtype.value,
            "uom": m.uom,
            "standard_price": m.standard_price,
            "spec": m.spec,
        }
        for m in ds.materials.values()
    ]
    _write_table(
        path_for("materials"),
        mapping.columns["materials"],
        materials,
        {"mtype": mapping.reverse_values("mtype")},
    )

    bom = [
        {
            "parent": line.parent,
            "component": line.component,
            "qty_per": line.qty_per,
            "scrap_rate": line.scrap_rate,
            "note": line.note,
        }
        for lines in ds.bom.values()
        for line in lines
    ]
    _write_table(path_for("bom"), mapping.columns["bom"], bom)

    routing = [
        {
            "product": op.product,
            "seq": op.seq,
            "cost_center": op.cost_center,
            "std_hours": op.std_hours,
            "operation": op.operation,
        }
        for ops in ds.routing.values()
        for op in ops
    ]
    _write_table(path_for("routing"), mapping.columns["routing"], routing)

    centers = [
        {
            "code": c.code,
            "name": c.name,
            "labor_rate": c.labor_rate,
            "overhead_budget": c.overhead_budget,
            "budget_hours": c.budget_hours,
        }
        for c in ds.cost_centers.values()
    ]
    _write_table(path_for("cost_centers"), mapping.columns["cost_centers"], centers)

    orders = [
        {
            "order_no": o.order_no,
            "product": o.product,
            "qty_planned": o.qty_planned,
            "qty_completed": o.qty_completed,
            "qty_scrapped": o.qty_scrapped,
            "wip_progress": o.wip_progress,
            "material_completion": o.material_completion,
            "status": o.status.value,
            "start_date": o.start_date,
            "due_date": o.due_date,
            "carried_from": o.carried_from,
        }
        for o in ds.orders
    ]
    _write_table(
        path_for("orders"),
        mapping.columns["orders"],
        orders,
        {"status": mapping.reverse_values("status")},
    )

    issues = [
        {
            "doc_no": i.doc_no,
            "order_no": i.order_no,
            "material": i.material,
            "qty": i.qty,
            "unit_price": i.unit_price,
            "issue_date": i.issue_date,
            "is_return": i.is_return,
        }
        for i in ds.issues
    ]
    _write_table(path_for("issues"), mapping.columns["issues"], issues)

    labor = [
        {
            "doc_no": r.doc_no,
            "order_no": r.order_no,
            "cost_center": r.cost_center,
            "hours": r.hours,
            "hourly_rate": r.hourly_rate,
            "work_date": r.work_date,
            "operation": r.operation,
        }
        for r in ds.labor
    ]
    _write_table(path_for("labor"), mapping.columns["labor"], labor)

    overhead = [
        {
            "doc_no": o.doc_no,
            "cost_center": o.cost_center,
            "expense_type": o.expense_type,
            "amount": o.amount,
        }
        for o in ds.overhead
    ]
    _write_table(path_for("overhead"), mapping.columns["overhead"], overhead)

    opening = [
        {
            "account": code,
            "debit": amount if amount > 0 else Decimal("0"),
            "credit": -amount if amount < 0 else Decimal("0"),
        }
        for code, amount in sorted(ds.opening_balances.items())
    ]
    _write_table(path_for("opening"), mapping.columns["opening"], opening)

    wip = [
        {
            "order_no": w.order_no,
            "product": w.product,
            "qty_planned": w.qty_planned,
            "qty_completed": w.qty_completed,
            "qty_in_wip": w.qty_in_wip,
            "material_completion": w.material_completion,
            "wip_progress": w.wip_progress,
            "cost_material": w.cost_material,
            "cost_labor": w.cost_labor,
            "cost_oh": w.cost_oh,
            "carried_from": w.carried_from,
        }
        for w in ds.wip_opening.values()
    ]
    _write_table(path_for("wip"), mapping.columns["wip"], wip)

    (target / "说明.txt").write_text(
        "本目录为 ERP 导出形态的示例数据（由 costcontrol 生成），用于适配器演示。\n"
        f"厂商模板：{mapping.label}（{mapping.vendor}）\n"
        "列名与字段的对应关系见 config_templates/adapters/。\n",
        encoding="utf-8",
    )
    return target


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------


def _decimal(text: str, default: str = "0") -> Decimal:
    text = (text or "").strip()
    return Decimal(text) if text else Decimal(default)


def _date(text: str) -> date | None:
    text = (text or "").strip()
    return date.fromisoformat(text) if text else None


def _read_table(path: Path, columns: dict[str, str]) -> list[dict[str, str]]:
    if not path.exists():
        raise AdapterError(f"导出文件缺失：{path.name}")
    reverse = {header: field for field, header in columns.items()}
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(
                {
                    field: (raw.get(header) or "").strip()
                    for header, field in reverse.items()
                }
            )
    return rows


def read_export(
    directory: str | Path,
    mapping: ExportMapping | None = None,
) -> Dataset:
    """读取 ERP 导出目录，返回可直接计算的 Dataset。"""
    mapping = mapping or load_mapping()
    root = Path(directory)
    if not root.is_dir():
        raise AdapterError(f"导出目录不存在：{root}")

    def path_for(entity: str) -> Path:
        name = mapping.files.get(entity)
        if not name:
            raise AdapterError(f"映射缺少文件 {entity}")
        return root / name

    meta_rows = _read_table(path_for("meta"), mapping.columns["meta"])
    meta = {row["key"]: row["value"] for row in meta_rows}

    value_maps = mapping.values

    materials = {}
    for row in _read_table(path_for("materials"), mapping.columns["materials"]):
        mtype = value_maps.get("mtype", {}).get(row["mtype"], row["mtype"])
        materials[row["code"]] = Material(
            code=row["code"],
            name=row["name"],
            mtype=MaterialType(mtype),
            uom=row["uom"],
            standard_price=_decimal(row["standard_price"]),
            spec=row["spec"],
        )

    bom: dict[str, list[BomLine]] = {}
    for row in _read_table(path_for("bom"), mapping.columns["bom"]):
        bom.setdefault(row["parent"], []).append(
            BomLine(
                parent=row["parent"],
                component=row["component"],
                qty_per=_decimal(row["qty_per"]),
                scrap_rate=_decimal(row["scrap_rate"]),
                note=row["note"],
            )
        )

    routing: dict[str, list[RoutingOp]] = {}
    for row in _read_table(path_for("routing"), mapping.columns["routing"]):
        routing.setdefault(row["product"], []).append(
            RoutingOp(
                product=row["product"],
                seq=int(row["seq"]),
                cost_center=row["cost_center"],
                std_hours=_decimal(row["std_hours"]),
                operation=row["operation"],
            )
        )

    centers = {
        row["code"]: CostCenter(
            code=row["code"],
            name=row["name"],
            labor_rate=_decimal(row["labor_rate"]),
            overhead_budget=_decimal(row["overhead_budget"]),
            budget_hours=_decimal(row["budget_hours"]),
        )
        for row in _read_table(path_for("cost_centers"), mapping.columns["cost_centers"])
    }

    orders = []
    for row in _read_table(path_for("orders"), mapping.columns["orders"]):
        status = value_maps.get("status", {}).get(row["status"], row["status"])
        orders.append(
            ProductionOrder(
                order_no=row["order_no"],
                product=row["product"],
                qty_planned=_decimal(row["qty_planned"]),
                qty_completed=_decimal(row["qty_completed"]),
                qty_scrapped=_decimal(row["qty_scrapped"]),
                wip_progress=_decimal(row["wip_progress"], "0"),
                material_completion=_decimal(row["material_completion"], "1"),
                status=OrderStatus(status or "RELEASED"),
                start_date=_date(row["start_date"]),
                due_date=_date(row["due_date"]),
                carried_from=row["carried_from"],
            )
        )

    issues = [
        MaterialIssue(
            doc_no=row["doc_no"],
            order_no=row["order_no"],
            material=row["material"],
            qty=_decimal(row["qty"]),
            unit_price=_decimal(row["unit_price"]),
            issue_date=_date(row["issue_date"]),
            is_return=row["is_return"].strip() == "是",
        )
        for row in _read_table(path_for("issues"), mapping.columns["issues"])
    ]

    labor = [
        LaborReport(
            doc_no=row["doc_no"],
            order_no=row["order_no"],
            cost_center=row["cost_center"],
            hours=_decimal(row["hours"]),
            hourly_rate=(
                _decimal(row["hourly_rate"]) if row["hourly_rate"].strip() else None
            ),
            work_date=_date(row["work_date"]),
            operation=row["operation"],
        )
        for row in _read_table(path_for("labor"), mapping.columns["labor"])
    ]

    overhead = [
        OverheadActual(
            cost_center=row["cost_center"],
            expense_type=row["expense_type"],
            amount=_decimal(row["amount"]),
            period=str(meta.get("期间", "")),
            doc_no=row["doc_no"],
        )
        for row in _read_table(path_for("overhead"), mapping.columns["overhead"])
    ]

    opening: dict[str, Decimal] = {}
    for row in _read_table(path_for("opening"), mapping.columns["opening"]):
        amount = _decimal(row["debit"]) - _decimal(row["credit"])
        if amount != 0:
            opening[row["account"]] = amount

    wip_opening = {
        row["order_no"]: OpeningWip(
            order_no=row["order_no"],
            product=row["product"],
            qty_planned=_decimal(row["qty_planned"]),
            qty_completed=_decimal(row["qty_completed"]),
            qty_in_wip=_decimal(row["qty_in_wip"]),
            material_completion=_decimal(row["material_completion"], "1"),
            wip_progress=_decimal(row["wip_progress"], "0"),
            cost_material=_decimal(row["cost_material"]),
            cost_labor=_decimal(row["cost_labor"]),
            cost_oh=_decimal(row["cost_oh"]),
            carried_from=row["carried_from"],
        )
        for row in _read_table(path_for("wip"), mapping.columns["wip"])
    }

    ds = Dataset(
        company=str(meta.get("公司", "")),
        period=str(meta.get("期间", "")),
        cost_centers=centers,
        materials=materials,
        bom=bom,
        routing=routing,
        orders=orders,
        issues=issues,
        labor=labor,
        overhead=overhead,
        params={},
        opening_balances=opening,
        wip_opening=wip_opening,
    )
    validate(ds)
    return ds


__all__ = [
    "AdapterError",
    "DEFAULT_MAPPING",
    "ExportMapping",
    "load_mapping",
    "read_export",
    "write_export",
]
