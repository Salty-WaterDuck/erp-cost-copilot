"""导出覆盖率检查：拿到一份 ERP 导出，先回答「能做什么、缺什么」。

不假设厂商、不假设文件名。按表头识别每张表属于哪种已知单据，然后：

1. 文件级：识别类型、行数、缺失的必需列、额外列；
2. 能力级：报表体检 / 六项差异 / 费用分配 / 约当产量各自能不能做；
3. 语义级：跨表勾稽（领料金额 vs 成本单、人工 vs 成本单、废品 vs 日报），
   以及 BOM 叶子物料与领料编码是否对得上。

这一步不计算成本，只判断数据够不够、口径通不通。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .report import render_table

SCHEMAS: list[tuple[str, str, list[str]]] = [
    ("sap_cost_structure", "SAP 成本构成", ["成本组件", "2025年均值_元每件", "2026年1-6月均值_元每件", "占比_综合"]),
    ("sap_inventory", "SAP 存货明细", ["会计年度期间", "科目编码", "期初余额_万元", "本期入库_万元", "本期出库_万元", "期末余额_万元"]),
    (
        "kingdee_cost_sheet",
        "金蝶成本计算单",
        [
            "会计期间",
            "工单编号",
            "产品编码",
            "完工数量",
            "直接材料_元",
            "直接人工_元",
            "制造费用_元",
            "单位成本_元",
        ],
    ),
    ("kingdee_restoration", "金蝶成本还原", ["成本项目", "还原前金额_万元", "半成品还原金额_万元", "还原后金额_万元"]),
    ("yonyou_daily", "用友完工日报", ["会计期间", "完工数量", "净产量", "实际工时", "直接材料_元", "直接人工_元", "制造费用_元"]),
    ("yonyou_scrap", "用友废品回收", ["季度", "废品重量_kg", "回收单价_元每kg", "废品回收金额_元"]),
    ("bom", "BOM 物料清单", ["父项物料编码", "子项物料编码", "单位用量", "损耗率"]),
    ("issues", "领料明细", ["会计期间", "工单编号", "物料编码", "领用数量", "金额_元"]),
    ("labor", "报工明细", ["会计期间", "工单编号", "工序", "成本中心", "实际工时", "人工成本_元"]),
    ("materials", "物料主数据", ["物料编码", "标准价_元"]),
    ("routing", "工艺路线", ["产品编码", "工序", "标准工时_分钟每件"]),
    ("orders", "生产订单", ["工单号", "计划数量", "产品编码"]),
    ("overhead", "制造费用", ["成本中心", "费用类型", "金额_元"]),
    ("opening", "期初余额", ["科目", "借方", "贷方"]),
]

CAPABILITIES: list[tuple[str, list[str], list[str]]] = [
    ("报表级成本体检", ["sap_cost_structure", "kingdee_cost_sheet", "yonyou_daily", "yonyou_scrap"], []),
    ("成本结构趋势", ["sap_cost_structure"], []),
    ("工单异常与自洽", ["kingdee_cost_sheet"], []),
    ("跨表勾稽", ["kingdee_cost_sheet", "yonyou_daily", "yonyou_scrap"], []),
    ("BOM 结构分析", ["bom"], []),
    ("材料领用汇总", ["issues"], []),
    ("人工成本归集", ["labor"], []),
    ("材料差异（六项）", ["bom", "issues", "materials", "orders"], ["领料未覆盖半成品内部原材料"]),
    ("人工效率差异", ["labor", "routing", "orders"], ["工时定额与实际差异过大"]),
    ("制造费用分配", ["overhead", "labor"], []),
    ("约当产量/在产", ["orders"], []),
]

TOLERANCE = Decimal("0.01")


class CoverageError(ValueError):
    pass


@dataclass
class FileCoverage:
    file: str
    kind: str
    label: str
    rows: int
    missing: list[str] = field(default_factory=list)
    extra: int = 0


@dataclass
class Capability:
    name: str
    status: str  # 可用 / 部分 / 不可用
    missing: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    directory: Path
    files: list[FileCoverage]
    capabilities: list[Capability]
    checks: list[tuple[bool, str]]

    @property
    def recognized(self) -> int:
        return sum(1 for item in self.files if item.kind != "unknown")


def _d(text: str) -> Decimal:
    text = (text or "").strip().replace(",", "").replace("%", "")
    return Decimal(text) if text else Decimal(0)


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [
            {key.strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
    headers = [header.strip() for header in (reader.fieldnames or [])]
    return headers, rows


def _detect(headers: list[str]) -> tuple[str, str, list[str]] | None:
    best: tuple[str, str, list[str], int] | None = None
    for kind, label, required in SCHEMAS:
        matched = sum(1 for name in required if name in headers)
        if matched / len(required) < 0.6:
            continue
        if best is None or matched > best[3]:
            best = (kind, label, required, matched)
    if best is None:
        return None
    return best[0], best[1], best[2]


def _quarter(period: str) -> str:
    if "Q" in period:
        return period
    year, month = period.split("-")[:2]
    return f"{year}Q{(int(month) - 1) // 3 + 1}"


def check_export(directory: str | Path) -> CoverageReport:
    root = Path(directory)
    if not root.is_dir():
        raise CoverageError(f"导出目录不存在：{root}")

    files: list[FileCoverage] = []
    by_kind: dict[str, list[dict[str, str]]] = {}
    for path in sorted(root.glob("*.csv")):
        headers, rows = _read(path)
        detected = _detect(headers)
        if detected is None:
            files.append(FileCoverage(path.name, "unknown", "未识别", len(rows), headers))
            continue
        kind, label, required = detected
        missing = [name for name in required if name not in headers]
        extra = len([h for h in headers if h not in required])
        files.append(FileCoverage(path.name, kind, label, len(rows), missing, extra))
        by_kind.setdefault(kind, []).extend(rows)

    capabilities: list[Capability] = []
    for name, required_kinds, blockers in CAPABILITIES:
        missing_files = [kind for kind in required_kinds if kind not in by_kind]
        if missing_files:
            labels = [next(l for k, l, _ in SCHEMAS if k == kind) for kind in missing_files]
            missing = [f"缺 {label}" for label in labels] + blockers
            capabilities.append(Capability(name, "不可用", missing))
        elif blockers:
            capabilities.append(Capability(name, "部分", blockers))
        else:
            capabilities.append(Capability(name, "可用", []))

    checks: list[tuple[bool, str]] = []
    checks.extend(_check_totals(by_kind))
    checks.extend(_check_bom_coverage(by_kind))
    checks.extend(_check_unit_cost(by_kind))
    checks.extend(_check_hours(by_kind))
    return CoverageReport(directory=root, files=files, capabilities=capabilities, checks=checks)


def _check_totals(by_kind: dict[str, list[dict[str, str]]]) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    sheet = by_kind.get("kingdee_cost_sheet", [])
    issue_rows = by_kind.get("issues", [])
    labor_rows = by_kind.get("labor", [])
    if sheet and issue_rows:
        material = {row["工单编号"]: _d(row["直接材料_元"]) for row in sheet}
        issues: dict[str, Decimal] = {}
        for row in issue_rows:
            issues[row["工单编号"]] = issues.get(row["工单编号"], Decimal(0)) + _d(row["金额_元"])
        matched = 0
        for order, amount in issues.items():
            expected = material.get(order, Decimal(0))
            if abs(amount - expected) <= TOLERANCE:
                matched += 1
        checks.append(
            (matched == len(issues), f"领料金额 vs 成本单材料：{matched}/{len(issues)} 张工单一致")
        )
    if sheet and labor_rows:
        labor = {row["工单编号"]: _d(row["直接人工_元"]) for row in sheet}
        reports: dict[str, Decimal] = {}
        for row in labor_rows:
            reports[row["工单编号"]] = reports.get(row["工单编号"], Decimal(0)) + _d(row["人工成本_元"])
        matched = 0
        for order, amount in reports.items():
            expected = labor.get(order, Decimal(0))
            if abs(amount - expected) <= TOLERANCE:
                matched += 1
        checks.append(
            (matched == len(reports), f"人工成本 vs 成本单人工：{matched}/{len(reports)} 张工单一致")
        )
    scrap = by_kind.get("yonyou_scrap", [])
    daily = by_kind.get("yonyou_daily", [])
    if scrap and daily:
        daily_by_quarter: dict[str, Decimal] = {}
        for row in daily:
            quarter = _quarter(row["会计期间"])
            current = daily_by_quarter.get(quarter, Decimal(0))
            daily_by_quarter[quarter] = current + _d(row["废品回收金额_元"])
        matched = 0
        for row in scrap:
            quarter = row.get("季度", "")
            if abs(daily_by_quarter.get(quarter, Decimal(0)) - _d(row["废品回收金额_元"])) <= TOLERANCE:
                matched += 1
        checks.append((matched == len(scrap), f"废品回收 vs 日报：{matched}/{len(scrap)} 季度一致"))
    return checks


def _check_bom_coverage(by_kind: dict[str, list[dict[str, str]]]) -> list[tuple[bool, str]]:
    bom = by_kind.get("bom", [])
    issues = by_kind.get("issues", [])
    if not bom or not issues:
        return []
    components = {row["子项物料编码"] for row in bom}
    issued = {row["物料编码"] for row in issues}
    hit = components & issued
    missing = "、".join(sorted(components - issued)) or "—"
    return [
        (
            len(hit) == len(components),
            f"BOM 物料 vs 领料编码：{len(hit)}/{len(components)} 覆盖"
            f"（未领用：{missing}，属半成品内部用料）",
        )
    ]


def _check_unit_cost(by_kind: dict[str, list[dict[str, str]]]) -> list[tuple[bool, str]]:
    sheet = by_kind.get("kingdee_cost_sheet", [])
    if not sheet:
        return []
    matched = 0
    for row in sheet:
        total = (
            _d(row["直接材料_元"])
            + _d(row["直接人工_元"])
            + _d(row["制造费用_元"])
            + _d(row.get("废品损失_元", "0"))
        )
        completed = _d(row["完工数量"])
        computed = total / completed if completed else Decimal(0)
        if abs(computed - _d(row["单位成本_元"])) <= Decimal("0.02"):
            matched += 1
    return [
        (
            matched == len(sheet),
            f"单位成本 vs 分项合计：{matched}/{len(sheet)} 张工单一致",
        )
    ]


def _check_hours(by_kind: dict[str, list[dict[str, str]]]) -> list[tuple[bool, str]]:
    orders = by_kind.get("orders", [])
    routing = by_kind.get("routing", [])
    labor = by_kind.get("labor", [])
    if not orders or not routing or not labor:
        return []
    product = orders[0]["产品编码"]
    planned = sum((_d(row["计划数量"]) for row in orders if row["产品编码"] == product), Decimal(0))
    ops = [row for row in routing if row["产品编码"] == product]
    if not planned or not ops:
        return []
    std_per_unit = sum(
        (
            _d(row["标准工时_分钟每件"]) + _d(row["准备工时_分钟每批"]) / planned
        )
        / 60
        for row in ops
    )
    std_hours = std_per_unit * planned
    actual_hours = sum((_d(row["实际工时"]) for row in labor), Decimal(0))
    ratio = actual_hours / std_hours if std_hours else Decimal(0)
    ok = Decimal("0.8") <= ratio <= Decimal("1.2")
    return [
        (
            ok,
            f"标准工时 vs 实际工时：{std_hours:,.0f}h vs {actual_hours:,.0f}h（{ratio:.1f} 倍）",
        )
    ]


def render_coverage(report: CoverageReport) -> str:
    lines = [
        f"导出覆盖率检查 · {report.directory}",
        "=" * 56,
        f"CSV {len(report.files)} 个，识别 {report.recognized} 个，"
        f"未识别 {len(report.files) - report.recognized} 个",
        "",
        "一、文件与字段",
    ]
    rows = [
        [
            item.file,
            item.label,
            str(item.rows),
            "、".join(item.missing) if item.missing else "—",
            str(item.extra),
        ]
        for item in report.files
    ]
    lines.append(
        render_table(["文件", "类型", "行数", "缺失必需列", "额外列"], rows, ["l", "l", "r", "l", "r"])
    )

    lines.append("")
    lines.append("二、能力矩阵")
    cap_rows = [
        [item.name, item.status, "；".join(item.missing) if item.missing else "—"]
        for item in report.capabilities
    ]
    lines.append(render_table(["分析能力", "状态", "缺失 / 阻塞"], cap_rows, ["l", "l", "l"]))

    lines.append("")
    lines.append("三、语义勾稽")
    if not report.checks:
        lines.append("可勾稽的数据不足。")
    for ok, text in report.checks:
        lines.append(f"{'✓' if ok else '✗'} {text}")

    lines.append("")
    lines.append("四、结论")
    usable = [item.name for item in report.capabilities if item.status == "可用"]
    partial = [item.name for item in report.capabilities if item.status == "部分"]
    blocked = [item.name for item in report.capabilities if item.status == "不可用"]
    if usable:
        lines.append("可用：" + "、".join(usable))
    if partial:
        lines.append("部分可用：" + "、".join(partial))
    if blocked:
        lines.append("不可用：" + "、".join(blocked))
    lines.append("")
    lines.append("说明：识别只依赖表头；缺失列与勾稽结果决定能力边界，不代表数据错误。")
    return "\n".join(lines)


__all__ = [
    "Capability",
    "CoverageError",
    "CoverageReport",
    "FileCoverage",
    "check_export",
    "render_coverage",
]
