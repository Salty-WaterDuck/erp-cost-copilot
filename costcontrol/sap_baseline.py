"""SAP 标准成本卡基准：BOM/工艺路线不可信时，用 ERP 标准卡做差异基准。

第二层复算已经证明：材料、人工、费用三项与 ERP 成本单分毫不差，但 BOM+工艺
卷算出来的标准成本（286.26）与 SAP 标准卡（456.93）差 37%。这时继续用 BOM
做基准，会把「标准本身不对」误判成「车间超支/节约」。

这个模块改用 SAP CK13n 的标准成本作为基准，只做四件事：

1. 按成本要素列实际 vs 标准；
2. 算差异额与差异率；
3. 判断是否为「系统性同向偏差」（提示标准版本/口径问题，而不是绩效）；
4. 明确说清哪些结论能下、哪些不能下。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .report import render_table
from .run import CostRun

FILES = {"structure": "01_sap_ck13n_成本构成分解.csv"}

ELEMENTS = (
    ("material", "材料"),
    ("labor", "人工"),
    ("overhead", "费用"),
    ("outsourcing", "外协"),
)


class BaselineError(ValueError):
    pass


def _d(text: str) -> Decimal:
    text = (text or "").strip().replace(",", "").replace("%", "")
    return Decimal(text) if text else Decimal(0)


@dataclass(frozen=True)
class SapCard:
    material: Decimal
    labor: Decimal
    overhead: Decimal
    outsourcing: Decimal
    total: Decimal
    basis: str = "18个月综合均值"

    def amount(self, key: str) -> Decimal:
        return {
            "material": self.material,
            "labor": self.labor,
            "overhead": self.overhead,
            "outsourcing": self.outsourcing,
        }[key]


@dataclass
class ElementVariance:
    key: str
    label: str
    actual: Decimal
    standard: Decimal

    @property
    def variance(self) -> Decimal:
        return self.actual - self.standard

    @property
    def rate(self) -> Decimal | None:
        if self.standard == 0:
            return None
        return self.variance / self.standard * 100


@dataclass
class BaselineResult:
    period: str
    quantity: Decimal
    elements: list[ElementVariance]
    unit_actual: Decimal
    unit_standard: Decimal

    @property
    def actual_total(self) -> Decimal:
        return sum((item.actual for item in self.elements), Decimal(0))

    @property
    def standard_total(self) -> Decimal:
        return sum((item.standard for item in self.elements), Decimal(0))

    @property
    def variance_total(self) -> Decimal:
        return self.actual_total - self.standard_total

    @property
    def rate_total(self) -> Decimal | None:
        if self.standard_total == 0:
            return None
        return self.variance_total / self.standard_total * 100

    @property
    def systemic(self) -> bool:
        rates = [item.rate for item in self.elements if item.rate is not None]
        if len(rates) < 2:
            return False
        return all(rate <= -10 for rate in rates) or all(rate >= 10 for rate in rates)


def load_sap_card(directory: str | Path) -> SapCard:
    path = Path(directory) / FILES["structure"]
    if not path.exists():
        raise BaselineError(f"缺少文件：{FILES['structure']}")
    values = {
        "material": Decimal(0),
        "labor": Decimal(0),
        "overhead": Decimal(0),
        "outsourcing": Decimal(0),
    }
    total = Decimal(0)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row = {key.strip(): (value or "").strip() for key, value in row.items()}
            name = row["成本组件"]
            amount = _d(row["18个月综合均值_元每件"])
            if name.strip() == "合计":
                total = amount
            elif name.startswith("原材料"):
                values["material"] = amount
            elif name.startswith("直接人工"):
                values["labor"] = amount
            elif name.startswith("制造费用"):
                values["overhead"] = amount
            elif name.startswith("外协"):
                values["outsourcing"] = amount
    if total == 0:
        raise BaselineError("SAP 成本构成缺少合计行")
    # 分项四舍五入后与合计可能差 0.01，把尾差并入材料，保证表内合计一致。
    values["material"] += total - sum(values.values(), Decimal(0))
    return SapCard(
        material=values["material"],
        labor=values["labor"],
        overhead=values["overhead"],
        outsourcing=values["outsourcing"],
        total=total,
    )


def analyze(run: CostRun, directory: str | Path, period: str) -> BaselineResult:
    card = load_sap_card(directory)
    quantity = sum(
        (cost.qty_completed + cost.qty_scrapped for cost in run.sheet.orders),
        Decimal(0),
    )
    if quantity == 0:
        raise BaselineError(f"{period} 没有完工/废品数量")

    actual = {
        "material": sum((cost.material_cost for cost in run.sheet.orders), Decimal(0)),
        "labor": sum((cost.labor_cost for cost in run.sheet.orders), Decimal(0)),
        "overhead": sum((cost.overhead_cost for cost in run.sheet.orders), Decimal(0)),
        "outsourcing": Decimal(0),
    }
    elements = [
        ElementVariance(
            key=key,
            label=label,
            actual=actual[key],
            standard=card.amount(key) * quantity,
        )
        for key, label in ELEMENTS
    ]
    unit_actual = sum((actual.values()), Decimal(0)) / quantity
    return BaselineResult(
        period=period,
        quantity=quantity,
        elements=elements,
        unit_actual=unit_actual,
        unit_standard=card.total,
    )


def _fmt_rate(rate: Decimal | None) -> str:
    return "—" if rate is None else f"{rate:+.2f}%"


def render_baseline(result: BaselineResult) -> str:
    lines = [
        f"SAP 标准卡基准 · {result.period}",
        "=" * 60,
        f"基准：SAP CK13n 18 个月均值 {result.unit_standard:,.2f} 元/件 × "
        f"{result.quantity:,.0f} 件（完工+废品，与 ERP 单位成本同口径）",
        "",
    ]
    rows = [
        [
            item.label,
            f"{item.actual:,.2f}",
            f"{item.standard:,.2f}",
            f"{item.variance:,.2f}",
            _fmt_rate(item.rate),
        ]
        for item in result.elements
    ]
    rows.append(
        [
            "合计",
            f"{result.actual_total:,.2f}",
            f"{result.standard_total:,.2f}",
            f"{result.variance_total:,.2f}",
            _fmt_rate(result.rate_total),
        ]
    )
    rows.append(
        [
            "单位成本",
            f"{result.unit_actual:,.2f}",
            f"{result.unit_standard:,.2f}",
            f"{result.unit_actual - result.unit_standard:,.2f}",
            _fmt_rate((result.unit_actual / result.unit_standard - 1) * 100),
        ]
    )
    lines.append(
        render_table(
            ["项目", "实际", "标准", "差异", "差异率"],
            rows,
            ["l", "r", "r", "r", "r"],
        )
    )
    lines.append(
        "单位成本口径：三项合计 /（完工+废品），用于与标准卡同口径比较；"
        "ERP 的 344.70 含废品损失且按完工数量。"
    )
    lines.append("")

    material = next(item for item in result.elements if item.key == "material")
    outsourcing = next(item for item in result.elements if item.key == "outsourcing")
    combined_actual = material.actual + outsourcing.actual
    combined_standard = material.standard + outsourcing.standard
    combined_rate = (
        (combined_actual / combined_standard - 1) * 100 if combined_standard else None
    )

    lines.append("")
    lines.append("判读")
    if result.systemic:
        lines.append(
            "- 四项实际全部同向偏离标准，且降幅都在 10% 以上 → 系统性偏差，"
            "优先怀疑标准卡版本/口径，而不是「全面节约」。"
        )
    else:
        lines.append("- 各要素有升有降，可以按要素分别追差异。")
    lines.append(
        f"- 材料+外协合并看：{combined_actual:,.2f} vs {combined_standard:,.2f}"
        f"（{_fmt_rate(combined_rate)}）。外协在本次导出中没有单独实际数，"
        "若半成品结转价已含外协，应与材料合并比较。"
    )
    lines.append(
        "- SAP 标准是 2025-2026H1 均值，且在产/外协归集范围可能与本期不同；"
        "在标准与本期口径对齐前，不建议把差额归责到采购或车间。"
    )
    lines.append(
        f"- 可确认的事实：本期实际单位成本 {result.unit_actual:,.2f}，"
        f"比标准 {result.unit_standard:,.2f} 低 "
        f"{(1 - result.unit_actual / result.unit_standard) * 100:,.2f}%。"
    )
    return "\n".join(lines)


__all__ = [
    "BaselineError",
    "BaselineResult",
    "ElementVariance",
    "SapCard",
    "analyze",
    "load_sap_card",
    "render_baseline",
]
