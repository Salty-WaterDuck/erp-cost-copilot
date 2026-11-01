"""报表级成本体检：只有 ERP 报表导出、没有单据明细时也能做归因。

真实的成本会计手里往往没有 BOM、领料单、报工单，只有几张从 ERP 导出的
汇总报表（SAP CK13n、金蝶成本计算单、用友完工日报……）。这一层不重算成本，
只做三件事：

1. **趋势与结构**：单位成本、材料/人工/费用占比、报废率怎么变；
2. **异常识别**：返工单、报废 spike、费用涨幅异常；
3. **跨表勾稽**：报表之间、报表内部对不对得上——先修数据，再追责任。

「强归因」的第一步不是解释差异，而是确认差异本身可信。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .money import q_money
from .report import money, render_table

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "auto_reports"

FILES = {
    "structure": "01_sap_ck13n_成本构成分解.csv",
    "inventory": "02_sap_存货明细.csv",
    "orders": "03_kingdee_成本计算单.csv",
    "restoration": "04_kingdee_成本还原汇总.csv",
    "daily": "05_yonyou_完工产品日报.csv",
    "scrap": "06_yonyou_废品回收表.csv",
}


class ReportError(ValueError):
    pass


def _d(text: str) -> Decimal:
    text = (text or "").strip().replace(",", "").replace("%", "")
    if text in ("", "-"):
        return Decimal(0)
    return Decimal(text)


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise ReportError(f"报表文件缺失：{path.name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {key.strip(): (value or "").strip() for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    name: str
    avg_2025: Decimal
    avg_2026: Decimal
    avg_18m: Decimal
    share_pct: Decimal
    note: str = ""

    @property
    def is_total(self) -> bool:
        return self.name.strip() == "合计"

    @property
    def change_pct(self) -> Decimal | None:
        if self.avg_2025 == 0:
            return None
        return (self.avg_2026 / self.avg_2025 - 1) * 100


@dataclass(frozen=True)
class InventoryRow:
    period: str
    account: str
    name: str
    opening: Decimal
    inbound: Decimal
    outbound: Decimal
    closing: Decimal
    variance: Decimal

    @property
    def balance_gap(self) -> Decimal:
        return q_money(self.opening + self.inbound - self.outbound - self.closing)


@dataclass(frozen=True)
class OrderRow:
    period: str
    order_no: str
    product: str
    name: str
    biz_type: str
    qty_completed: Decimal
    qty_scrap: Decimal
    material: Decimal
    semi_transfer: Decimal
    labor: Decimal
    overhead: Decimal
    scrap_loss: Decimal
    unit_cost: Decimal

    @property
    def total_cost(self) -> Decimal:
        return q_money(self.material + self.labor + self.overhead + self.scrap_loss)

    @property
    def computed_unit(self) -> Decimal:
        if self.qty_completed == 0:
            return Decimal(0)
        return self.total_cost / self.qty_completed

    @property
    def recon_pct(self) -> Decimal | None:
        if self.unit_cost == 0:
            return None
        return (self.computed_unit / self.unit_cost - 1) * 100

    @property
    def is_rework(self) -> bool:
        return "返工" in self.biz_type


@dataclass(frozen=True)
class RestorationRow:
    item: str
    before: Decimal
    semi: Decimal
    after: Decimal
    share_pct: Decimal


@dataclass(frozen=True)
class DailyRow:
    period: str
    cost_center: str
    product: str
    qty_completed: Decimal
    qty_scrap: Decimal
    net_output: Decimal
    scrap_recovery: Decimal
    hours: Decimal
    material: Decimal
    labor: Decimal
    overhead: Decimal
    unit_cost: Decimal

    @property
    def total_cost(self) -> Decimal:
        return q_money(self.material + self.labor + self.overhead)

    @property
    def computed_unit(self) -> Decimal:
        return self.total_cost / self.net_output if self.net_output else Decimal(0)

    @property
    def scrap_rate(self) -> Decimal:
        return self.qty_scrap / self.qty_completed * 100 if self.qty_completed else Decimal(0)


@dataclass(frozen=True)
class ScrapRow:
    period: str
    cost_center: str
    product: str
    recovery: Decimal
    net: Decimal
    weight: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    note: str = ""


@dataclass
class ReportBundle:
    structure: list[Component]
    inventory: list[InventoryRow]
    orders: list[OrderRow]
    restoration: list[RestorationRow]
    daily: list[DailyRow]
    scrap: list[ScrapRow]

    @property
    def product(self) -> str:
        for row in self.orders:
            if row.name:
                return row.name
        return "未命名产品"


@dataclass
class Finding:
    code: str
    severity: str  # 高 / 中 / 低
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""


@dataclass
class Analysis:
    bundle: ReportBundle
    findings: list[Finding]
    metrics: dict[str, Decimal]


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------


def load_bundle(directory: str | Path | None = None) -> ReportBundle:
    root = Path(directory) if directory else DEFAULT_DIR

    structure = [
        Component(
            name=row["成本组件"],
            avg_2025=_d(row["2025年均值_元每件"]),
            avg_2026=_d(row["2026年1-6月均值_元每件"]),
            avg_18m=_d(row["18个月综合均值_元每件"]),
            share_pct=_d(row["占比_综合"]),
            note=row.get("说明", ""),
        )
        for row in _rows(root / FILES["structure"])
    ]

    inventory = [
        InventoryRow(
            period=row["会计年度期间"],
            account=row["科目编码"],
            name=row["科目名称"],
            opening=_d(row["期初余额_万元"]),
            inbound=_d(row["本期入库_万元"]),
            outbound=_d(row["本期出库_万元"]),
            closing=_d(row["期末余额_万元"]),
            variance=_d(row["成本差异_万元"]),
        )
        for row in _rows(root / FILES["inventory"])
    ]

    orders = [
        OrderRow(
            period=row["会计期间"],
            order_no=row["工单编号"],
            product=row["产品编码"],
            name=row["产品名称"],
            biz_type=row["业务类型"],
            qty_completed=_d(row["完工数量"]),
            qty_scrap=_d(row["废品数量"]),
            material=_d(row["直接材料_元"]),
            semi_transfer=_d(row["其中半成品转入_元"]),
            labor=_d(row["直接人工_元"]),
            overhead=_d(row["制造费用_元"]),
            scrap_loss=_d(row["废品损失_元"]),
            unit_cost=_d(row["单位成本_元"]),
        )
        for row in _rows(root / FILES["orders"])
    ]

    restoration = [
        RestorationRow(
            item=row["成本项目"],
            before=_d(row["还原前金额_万元"]),
            semi=_d(row["半成品还原金额_万元"]),
            after=_d(row["还原后金额_万元"]),
            share_pct=_d(row["还原后占比"]),
        )
        for row in _rows(root / FILES["restoration"])
    ]

    daily = [
        DailyRow(
            period=row["会计期间"],
            cost_center=row["成本中心"],
            product=row["产品编码"],
            qty_completed=_d(row["完工数量"]),
            qty_scrap=_d(row["废品数量"]),
            net_output=_d(row["净产量"]),
            scrap_recovery=_d(row["废品回收金额_元"]),
            hours=_d(row["实际工时"]),
            material=_d(row["直接材料_元"]),
            labor=_d(row["直接人工_元"]),
            overhead=_d(row["制造费用_元"]),
            unit_cost=_d(row["单位净产量成本_元"]),
        )
        for row in _rows(root / FILES["daily"])
    ]

    scrap = [
        ScrapRow(
            period=row.get("季度") or row.get("会计期间", ""),
            cost_center=row.get("成本中心编码", ""),
            product=row.get("产品编码", ""),
            recovery=_d(row.get("废品回收金额_元", "0")),
            net=_d(row.get("冲减材料费用后净额_元", "0")),
            weight=_d(row.get("废品重量_kg", "0")),
            price=_d(row.get("回收单价_元每kg", "0")),
            note=row.get("备注", ""),
        )
        for row in _rows(root / FILES["scrap"])
    ]

    return ReportBundle(
        structure=structure,
        inventory=inventory,
        orders=orders,
        restoration=restoration,
        daily=daily,
        scrap=scrap,
    )


# --------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------


def _structure_findings(bundle: ReportBundle) -> list[Finding]:
    findings: list[Finding] = []
    total_2025 = next((c.avg_2025 for c in bundle.structure if c.is_total), Decimal(0))
    total_2026 = next((c.avg_2026 for c in bundle.structure if c.is_total), Decimal(0))
    if total_2025 and total_2026:
        change = (total_2026 / total_2025 - 1) * 100
        drivers = sorted(
            (c for c in bundle.structure if not c.is_total and c.change_pct is not None),
            key=lambda c: -(c.change_pct or Decimal(0)),
        )
        top = drivers[0] if drivers else None
        detail = (
            f"单位成本 {total_2026} 元/件，较 2025 年均值 {total_2025} 元/件变动 "
            f"{change:+.2f}%；涨幅最大的是 {top.name}（{top.change_pct:+.2f}%）。"
            if top
            else f"单位成本变动 {change:+.2f}%。"
        )
        findings.append(
            Finding(
                code="STRUCTURE-DRIFT",
                severity="中" if abs(change) >= Decimal("2") else "低",
                title="成本结构漂移",
                detail=detail,
                evidence=[
                    f"{c.name}: {c.avg_2025} → {c.avg_2026}（{c.change_pct:+.2f}%）"
                    for c in drivers[:3]
                ],
                suggestion="按涨幅最大的成本项拆到费用明细/采购价，再判断是量、价还是结构变化。",
            )
        )

    # 半成品行与备注冲突：若备注称已还原，则不应再单独计入合计
    semi = next((c for c in bundle.structure if c.name.startswith("半成品")), None)
    if semi and ("还原" in semi.name or "还原" in semi.note or "拆入" in semi.note):
        findings.append(
            Finding(
                code="SEMI-DOUBLE-COUNT",
                severity="高",
                title="SAP 成本构成疑似重复计入半成品",
                detail=(
                    f"“{semi.name}”占 {semi.share_pct}%，但备注写明“已拆入原材料/人工/制造费用”，"
                    "两者只能取其一，否则单位成本被高估。"
                ),
                evidence=[f"该行 18 个月均值 {semi.avg_18m} 元/件，占比 {semi.share_pct}%"],
                suggestion="确认该行是 memo（备查）还是参与合计；若是 memo，合计应从 100% 中剔除。",
            )
        )
    return findings


def _order_findings(bundle: ReportBundle) -> list[Finding]:
    findings: list[Finding] = []
    values = [float(order.unit_cost) for order in bundle.orders]
    if len(values) >= 5:
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = variance ** 0.5
        outliers = []
        for order in bundle.orders:
            z = (float(order.unit_cost) - mean) / std if std else 0.0
            if abs(z) >= 2:
                outliers.append((abs(z), z, order))
        if outliers:
            outliers.sort(reverse=True)
            _abs, z, worst = outliers[0]
            scrap_rate = (
                worst.qty_scrap / worst.qty_completed * 100
                if worst.qty_completed
                else Decimal(0)
            )
            findings.append(
                Finding(
                    code="ORDER-OUTLIER",
                    severity="高" if abs(z) >= 2.5 else "中",
                    title="工单单位成本异常",
                    detail=(
                        f"{worst.period} {worst.order_no}（{worst.biz_type}）单位成本 "
                        f"{worst.unit_cost}，偏离 18 期均值 {mean:.2f}（{z:+.2f}σ）；"
                        f"报废率 {scrap_rate:.2f}%，废品损失 {worst.scrap_loss}。"
                    ),
                    evidence=[
                        f"{order.period} {order.order_no}: {order.unit_cost}（{zz:+.2f}σ）"
                        for _a, zz, order in outliers[:3]
                    ],
                    suggestion="先核对当月报废/返工记录与产量波动，再判断是工艺、来料还是排产问题。",
                )
            )

    recon = []
    for order in bundle.orders:
        pct = order.recon_pct
        if pct is not None and abs(pct) > Decimal("1"):
            recon.append((abs(pct), order, pct))
    if recon:
        recon.sort(reverse=True)
        worst = recon[0]
        findings.append(
            Finding(
                code="UNIT-COST-RECON",
                severity="高",
                title="成本计算单自身不勾稽",
                detail=(
                    f"{len(recon)}/{len(bundle.orders)} 张工单的“单位成本 × 完工数量”与"
                    "分项合计不一致，最大偏差 "
                    f"{worst[2]:+.2f}%（{worst[1].order_no}）。"
                ),
                evidence=[
                    f"{order.order_no}: 分项合计/完工 = {order.computed_unit:.2f} vs 表列 {order.unit_cost}"
                    for _abs, order, _pct in recon[:3]
                ],
                suggestion="确认该列是实际单位成本还是标准/计划成本；若为实际，需先修报表口径再追差异。",
            )
        )
    return findings


def _restoration_findings(bundle: ReportBundle) -> list[Finding]:
    findings: list[Finding] = []
    before = sum((r.before for r in bundle.restoration if r.item == "合计"), Decimal(0))
    after = sum((r.after for r in bundle.restoration if r.item == "合计"), Decimal(0))
    if before and abs(after - before) > Decimal("0.5"):
        findings.append(
            Finding(
                code="RESTORATION-TOTAL",
                severity="高",
                title="成本还原前后总额不守恒",
                detail=(
                    f"还原前 {before} 万元、还原后 {after} 万元，差 {after - before} 万元"
                    f"（{(after / before - 1) * 100:+.2f}%）。成本还原应只重分类、不改变总额。"
                ),
                evidence=[
                    f"{r.item}: {r.before} → {r.after}（半成品还原 {r.semi}）"
                    for r in bundle.restoration
                    if r.item in ("半成品（综合结转）", "直接材料（原材料）")
                ],
                suggestion="检查还原表是否混入了重估/口径调整；纯还原必须满足“前=后”。",
            )
        )
    return findings


def _inventory_findings(bundle: ReportBundle) -> list[Finding]:
    broken = [row for row in bundle.inventory if abs(row.balance_gap) > Decimal("0.005")]
    if not broken:
        return []
    max_gap = max((abs(row.balance_gap) for row in broken), default=Decimal(0))
    return [
        Finding(
            code="INVENTORY-BALANCE",
            severity="低" if max_gap <= Decimal("0.5") else "中",
            title="存货明细余额不勾稽",
            detail=(
                f"{len(broken)}/{len(bundle.inventory)} 行“期初+入库-出库≠期末”，"
                f"最大差 {max_gap} 万元。"
            ),
            evidence=[
                f"{row.period} {row.name}: 期初 {row.opening} + 入 {row.inbound} "
                f"- 出 {row.outbound} ≠ 期末 {row.closing}（差 {row.balance_gap}）"
                for row in broken[:3]
            ],
            suggestion="先查是否有调账/重估未入表，再谈存货周转与成本差异。",
        )
    ]


def _scrap_findings(bundle: ReportBundle) -> list[Finding]:
    if len(bundle.scrap) < 2:
        return []
    half = len(bundle.scrap) // 2
    first = bundle.scrap[:half]
    second = bundle.scrap[half:]
    recovery_1 = sum((row.recovery for row in first), Decimal(0))
    recovery_2 = sum((row.recovery for row in second), Decimal(0))
    if recovery_1 == 0:
        return []
    change = (recovery_2 / recovery_1 - 1) * 100

    weight_1 = sum((row.weight for row in first), Decimal(0))
    weight_2 = sum((row.weight for row in second), Decimal(0))
    detail = (
        f"后半段回收 {recovery_2:.0f} 元 vs 前半段 {recovery_1:.0f} 元（{change:+.2f}%）。"
    )
    if weight_1 and weight_2:
        price_1 = recovery_1 / weight_1
        price_2 = recovery_2 / weight_2
        price_effect = (price_2 - price_1) * weight_1
        volume_effect = (weight_2 - weight_1) * price_2
        detail += (
            f"重量 {weight_1:.0f}→{weight_2:.0f}kg"
            f"（{(weight_2 / weight_1 - 1) * 100:+.2f}%），"
            f"均价 {price_1:.2f}→{price_2:.2f} 元/kg"
            f"（{(price_2 / price_1 - 1) * 100:+.2f}%）；"
            f"价格因素约 {price_effect:+.0f} 元，数量因素约 {volume_effect:+.0f} 元。"
        )
    else:
        detail += "回收额同时受废品率与废料单价影响，需拆开看。"

    return [
        Finding(
            code="SCRAP-RECOVERY-TREND",
            severity="中" if abs(change) >= Decimal("10") else "低",
            title="废品回收趋势变化",
            detail=detail,
            evidence=[
                f"{row.period}: {row.recovery} 元"
                + (f"（{row.weight:.0f}kg × {row.price:.2f}）" if row.weight else "")
                for row in bundle.scrap[-3:]
            ],
            suggestion="与报废率、废钢/废铝单价联查：量降价平、还是量价齐跌。",
        )
    ]


def _month_range(start: str, end: str) -> list[str]:
    year, month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    result: list[str] = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return result


def _gap_findings(bundle: ReportBundle) -> list[Finding]:
    months = sorted(
        {row.period for row in bundle.daily if len(row.period) == 7 and row.period[4] == "-"}
    )
    if len(months) < 2:
        return []
    missing = [m for m in _month_range(months[0], months[-1]) if m not in months]
    if not missing:
        return []
    return [
        Finding(
            code="DATA-GAPS",
            severity="中",
            title="日报期间不连续",
            detail=(
                f"用友完工日报缺 {len(missing)} 个月（{missing[0]} ~ {missing[-1]}），"
                "单位成本与报废率趋势只能看两端，不能判断中间是否发生过异常。"
            ),
            evidence=[f"已有：{', '.join(months)}"],
            suggestion="补齐缺失月份后再做趋势结论；否则“持续优化”可能只是样本缺失。",
        )
    ]


def analyze(bundle: ReportBundle) -> Analysis:
    findings: list[Finding] = []
    findings += _structure_findings(bundle)
    findings += _order_findings(bundle)
    findings += _restoration_findings(bundle)
    findings += _inventory_findings(bundle)
    findings += _scrap_findings(bundle)
    findings += _gap_findings(bundle)

    order = {"高": 0, "中": 1, "低": 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))

    metrics = {
        "components": Decimal(len(bundle.structure)),
        "orders": Decimal(len(bundle.orders)),
        "periods": Decimal(len(bundle.daily)),
        "findings_high": Decimal(sum(1 for f in findings if f.severity == "高")),
    }
    return Analysis(bundle=bundle, findings=findings, metrics=metrics)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _structure_table(bundle: ReportBundle) -> str:
    rows = []
    for component in bundle.structure:
        change = component.change_pct
        rows.append(
            [
                component.name,
                f"{component.avg_2025:.2f}",
                f"{component.avg_2026:.2f}",
                "—" if change is None else f"{change:+.2f}%",
                f"{component.share_pct:.1f}%",
            ]
        )
    return render_table(
        ["成本组件", "2025 均值", "2026H1 均值", "变动", "占比"],
        rows,
        ["l", "r", "r", "r", "r"],
    )


def _daily_table(bundle: ReportBundle) -> str:
    rows = []
    for row in bundle.daily:
        rows.append(
            [
                row.period,
                f"{row.qty_completed:.0f}",
                f"{row.qty_scrap:.0f}",
                f"{row.scrap_rate:.2f}%",
                f"{row.unit_cost:.2f}",
                f"{row.computed_unit:.2f}",
                f"{row.scrap_recovery:.0f}",
            ]
        )
    return render_table(
        ["期间", "完工", "废品", "报废率", "表列单位成本", "分项合计/净产量", "回收额"],
        rows,
        ["l", "r", "r", "r", "r", "r", "r"],
    )


def _order_table(bundle: ReportBundle) -> str:
    rows = []
    for row in bundle.orders:
        rows.append(
            [
                row.period,
                row.order_no,
                row.biz_type,
                f"{row.qty_completed:.0f}",
                f"{row.qty_scrap:.0f}",
                money(row.scrap_loss),
                f"{row.unit_cost:.1f}",
                f"{row.computed_unit:.2f}",
                "—" if row.recon_pct is None else f"{row.recon_pct:+.2f}%",
            ]
        )
    return render_table(
        ["期间", "工单", "类型", "完工", "废品", "废品损失", "表列单位", "分项合计", "偏差"],
        rows,
        ["l", "l", "l", "r", "r", "r", "r", "r", "r"],
    )


def render_analysis(analysis: Analysis) -> str:
    bundle = analysis.bundle
    lines = [
        f"成本体检报告 · {bundle.product}",
        "=" * 52,
        f"覆盖：SAP 成本构成 / SAP 存货 / 金蝶成本单 / 金蝶还原 / 用友日报 / 用友废品回收",
        f"样本：{len(bundle.orders)} 张工单、{len(bundle.daily)} 个月度日报、"
        f"{len(bundle.scrap)} 个季度回收记录",
        "",
        "一、结论与异常",
    ]
    if not analysis.findings:
        lines.append("未发现异常。")
    for index, finding in enumerate(analysis.findings, 1):
        lines.append(f"{index}. [{finding.severity}] {finding.title}")
        lines.append(f"   {finding.detail}")
        for item in finding.evidence:
            lines.append(f"   · {item}")
        if finding.suggestion:
            lines.append(f"   建议：{finding.suggestion}")
        lines.append("")

    lines.append("二、成本结构（SAP CK13n）")
    lines.append(_structure_table(bundle))

    lines.append("")
    lines.append("三、单位成本与报废趋势（用友完工日报）")
    lines.append(_daily_table(bundle))

    lines.append("")
    lines.append("四、工单成本与自洽性（金蝶成本计算单）")
    lines.append(_order_table(bundle))

    lines.append("")
    lines.append("五、成本还原（金蝶）")
    rows = [
        [r.item, f"{r.before:.1f}", f"{r.semi:+.1f}", f"{r.after:.1f}", f"{r.share_pct:.1f}%"]
        for r in bundle.restoration
    ]
    lines.append(
        render_table(
            ["成本项目", "还原前", "半成品还原", "还原后", "还原后占比"],
            rows,
            ["l", "r", "r", "r", "r"],
        )
    )

    lines.append("")
    lines.append("六、口径提示")
    lines.append(
        "- 用友日报的单位成本按“净产量”计算，金蝶成本单按“完工数量”计算，跨表比较前必须统一分母；"
    )
    lines.append("- 报表级体检只能发现“哪里不对”，不能替代单据级六项差异；要拆价差/量差仍需领料、报工、BOM 明细。")
    return "\n".join(lines)


__all__ = [
    "Analysis",
    "Component",
    "DailyRow",
    "Finding",
    "InventoryRow",
    "OrderRow",
    "ReportBundle",
    "ReportError",
    "RestorationRow",
    "ScrapRow",
    "analyze",
    "load_bundle",
    "render_analysis",
]
