"""终端报表：成本计算单、差异分解、预警清单。

中文字符占两个显示宽度，直接用 str.ljust 会错位，所以这里按 East Asian
Width 计算实际宽度再补齐 —— 纯标准库即可。
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal

from .accounting import JournalBook, TrialBalanceRow
from .models import CostSheet, OrderCost
from .money import q_money
from .run import CostRun

WIDE = ("W", "F")


def display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in WIDE else 1
    return width


def _ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _rjust(text: str, width: int) -> str:
    return " " * max(0, width - display_width(text)) + text


def render_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    def line(cells: list[str]) -> str:
        parts = [
            (_ljust(c, widths[i]) if aligns[i] == "l" else _rjust(c, widths[i]))
            for i, c in enumerate(cells)
        ]
        return "  ".join(parts).rstrip()

    head = line(headers)
    out = [head, "-" * display_width(head)]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def qty(value: Decimal) -> str:
    text = f"{value:,.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def ratio(value: Decimal | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def _section(title: str) -> str:
    return f"\n{title}\n" + "=" * max(30, display_width(title) + 4)


def render_order_detail(run: CostRun, cost: OrderCost) -> str:
    ds = run.dataset
    name = ds.material(cost.product).name
    lines: list[str] = []
    lines.append(
        f"\n▌{cost.order_no}  {cost.product} {name}   "
        f"计划 {qty(cost.qty_planned)} / 完工 {qty(cost.qty_completed)} / "
        f"报废 {qty(cost.qty_scrapped)} / 在产 {qty(cost.wip_qty)}"
    )

    rows: list[list[str]] = []
    if cost.opening_cost:
        rows.append(
            [
                "期初在产转入",
                qty(cost.qty_opening),
                "",
                money(cost.opening_cost),
                money(cost.opening_std),
                money(cost.variance_opening),
            ]
        )
    for line in cost.material_lines:
        material = ds.material(line.material)
        label = "半成品转入" if material.mtype.value == "SEMI" else "直接材料"
        rows.append(
            [
                f"{label} · {material.name}",
                qty(line.qty),
                f"{line.unit_price:.4f}",
                money(line.amount),
                money(line.std_amount),
                money(line.variance),
            ]
        )
    for line in cost.labor_lines:
        cc = ds.center(line.cost_center)
        rows.append(
            [
                f"直接人工 · {cc.name}",
                qty(line.hours) + " h",
                f"{line.rate:.4f}",
                money(line.amount),
                money(line.std_amount),
                money(line.variance),
            ]
        )
    for line in cost.overhead_lines:
        cc = ds.center(line.cost_center)
        rows.append(
            [
                f"制造费用 · {cc.name}",
                qty(line.hours) + " h",
                f"{line.rate:.4f}",
                money(line.amount),
                money(line.std_amount),
                money(line.variance),
            ]
        )
    rows.append(
        [
            "合计",
            "",
            "",
            money(cost.total_cost),
            money(cost.std_total),
            money(cost.variance_total),
        ]
    )
    lines.append(
        render_table(
            ["成本项目", "用量/工时", "单价/费率", "实际金额", "标准金额", "差异"],
            rows,
            ["l", "r", "r", "r", "r", "r"],
        )
    )

    lines.append(
        "  差异分解（本期）："
        f"材料价差 {money(cost.material_price_variance)} / "
        f"材料量差 {money(cost.material_qty_variance)} / "
        f"人工工资率 {money(cost.labor_rate_variance)} / "
        f"人工效率 {money(cost.labor_efficiency_variance)} / "
        f"费用耗费 {money(cost.overhead_spending_variance)} / "
        f"费用效率 {money(cost.overhead_efficiency_variance)}"
    )
    if cost.opening_cost:
        lines.append(
            "  差异构成："
            f"期初沉淀 {money(cost.variance_opening)} + 本期 {money(cost.variance_current)}"
            f" = 合计 {money(cost.variance_total)}"
        )
    lines.append(
        "  成本去向："
        f"完工入库 {money(cost.finished_cost)}（单位成本 {cost.unit_cost:.4f}，"
        f"其中废品损失 {money(cost.scrap_cost)} 由良品负担）/ "
        f"期末在产 {money(cost.wip_cost)}"
    )
    for note in cost.notes:
        lines.append(f"  · {note}")
    return "\n".join(lines)


def render_standard_cards(run: CostRun) -> str:
    """标准成本卡：把 BOM 逐层卷算的结果摊开给人看。"""
    ds = run.dataset
    out = ["标准成本卡（BOM 逐层卷算）"]
    products = [
        code
        for code, material in ds.materials.items()
        if material.mtype.value in ("SEMI", "FINISHED")
        and (ds.bom.get(code) or ds.routing.get(code))
    ]
    for code in sorted(products, key=lambda c: (run.standard.depth(c), c)):
        material = ds.material(code)
        card = run.standard.card(code)
        out.append(
            f"\n▌{code} {material.name}  "
            f"层级 {run.standard.depth(code)} · 标准单位成本 {card.total:.4f}"
        )
        rows: list[list[str]] = []
        for line in card.material_lines:
            component = ds.material(line.material)
            label = "半成品" if component.mtype.value == "SEMI" else "材料"
            rows.append(
                [
                    f"{label} · {component.name}",
                    qty(line.qty),
                    f"{line.price:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        for line in card.labor_lines:
            rows.append(
                [
                    f"人工 · {ds.center(line.cost_center).name}",
                    qty(line.hours) + " h",
                    f"{line.rate:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        for line in card.overhead_lines:
            rows.append(
                [
                    f"制造费用 · {ds.center(line.cost_center).name}",
                    qty(line.hours) + " h",
                    f"{line.rate:.4f}",
                    f"{line.amount:.4f}",
                ]
            )
        rows.append(["合计", "", "", f"{card.total:.4f}"])
        out.append(render_table(["项目", "单位用量/工时", "单价/费率", "单位成本"], rows, ["l", "r", "r", "r"]))
    return "\n".join(out)


def render_cost_sheet(run: CostRun, detail: bool = False) -> str:
    sheet: CostSheet = run.sheet
    out: list[str] = []
    out.append(f"{sheet.company}  {sheet.period} 成本计算单")
    out.append(
        f"工单 {len(sheet.orders)} 张 · 成本对象 {len(sheet.products)} 个 · "
        f"预警 {len(sheet.alerts)} 条"
    )

    out.append(_section("一、成本汇总"))
    summary_rows = [
        ["期初在产转入", money(sheet.opening_cost)],
        ["本期直接材料", money(sheet.material_cost)],
        ["本期直接人工", money(sheet.labor_cost)],
        ["本期制造费用", money(sheet.overhead_cost)],
        ["本期发生合计", money(sheet.current_cost)],
        ["实际总成本", money(sheet.total_cost)],
        ["标准总成本", money(sheet.std_total)],
        ["总差异", money(sheet.variance_total)],
        ["未分配制造费用", money(run.unallocated_overhead)],
    ]
    out.append(render_table(["项目", "金额（元）"], summary_rows, ["l", "r"]))

    out.append(_section("二、工单成本"))
    rows: list[list[str]] = []
    for cost in sheet.orders:
        rows.append(
            [
                cost.order_no,
                cost.product,
                f"{qty(cost.qty_completed)}/{qty(cost.qty_planned)}",
                money(cost.material_cost),
                money(cost.labor_cost),
                money(cost.overhead_cost),
                money(cost.total_cost),
                f"{cost.unit_cost:.4f}",
                money(cost.std_total),
                money(cost.variance_total),
                ratio(cost.variance_rate),
            ]
        )
    out.append(
        render_table(
            [
                "工单",
                "产品",
                "完工/计划",
                "材料",
                "人工",
                "制造费用",
                "实际合计",
                "单位成本",
                "标准",
                "差异",
                "差异率",
            ],
            rows,
            ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r", "r"],
        )
    )

    if detail:
        out.append(_section("三、成本明细"))
        for cost in sheet.orders:
            out.append(render_order_detail(run, cost))

    out.append(_section("四、产品单位成本"))
    rows = []
    for item in sheet.products:
        rows.append(
            [
                item.product,
                item.name,
                qty(item.qty_completed),
                money(item.finished_cost),
                f"{item.unit_cost:.4f}",
                f"{item.std_unit_cost:.4f}",
                money(item.variance),
                ratio(item.variance_rate),
            ]
        )
    out.append(
        render_table(
            ["产品", "名称", "完工数量", "入库成本", "实际单位", "标准单位", "差异", "差异率"],
            rows,
            ["l", "l", "r", "r", "r", "r", "r", "r"],
        )
    )

    out.append(_section("五、控制预警"))
    if not sheet.alerts:
        out.append("  本期无预警。")
    else:
        for alert in sheet.alerts:
            out.append(f"  {alert.format()}")
    return "\n".join(out)


def _balance_cell(value: Decimal) -> str:
    if value == 0:
        return "0.00"
    return f"{'借' if value > 0 else '贷'} {money(abs(value))}"


def render_journal(book: JournalBook, title: str | None = None) -> str:
    from .accounts import account

    out = [title or f"记账凭证 · {book.period}  共 {len(book.entries)} 张"]
    if not book.entries:
        return "\n".join(out + ["  本期无凭证。"])

    width = max(
        display_width(account(line.account).label)
        for entry in book.entries
        for line in entry.lines
    )
    for entry in book.entries:
        out.append("")
        out.append(f"{entry.entry_no}  {entry.entry_date}  {entry.summary}")
        for line in entry.lines:
            label = _ljust(account(line.account).label, width)
            amount = _rjust(money(line.amount), 13)
            out.append(f"  {line.side}  {label}  {amount}  {line.summary}")
        mark = "✓" if entry.is_balanced else "✗"
        out.append(
            f"     合计 借 {money(entry.debit_total)} / 贷 {money(entry.credit_total)}  {mark}"
        )
    if len(book.entries) > 1:
        out.append("")
        out.append(
            f"全部凭证合计：借 {money(book.debit_total)} / 贷 {money(book.credit_total)}"
            + (" ✓ 借贷平衡" if book.is_balanced else " ✗ 借贷不平")
        )
    return "\n".join(out)


def render_restoration(run: CostRun) -> str:
    from .restoration import CostRestoration, Restorer

    restorer = Restorer(run)
    items = restorer.all()
    out = ["成本还原：把半成品里的加工费拆回原始成本项目"]
    if not items:
        return "\n".join(out + ["  本期无完工成本。"])

    rows: list[list[str]] = []
    for item in items:
        rows.append(
            [
                item.order_no,
                item.product,
                money(item.finished_cost),
                money(item.book_material),
                money(item.book_labor),
                money(item.book_overhead),
                money(item.material),
                money(item.labor),
                money(item.overhead),
                money(item.transfer_in),
            ]
        )
    out.append(
        render_table(
            [
                "工单",
                "产品",
                "完工成本",
                "账面材料",
                "账面人工",
                "账面费用",
                "还原材料",
                "还原人工",
                "还原费用",
                "半成品转入",
            ],
            rows,
            ["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
        )
    )

    finished = [
        item
        for item in items
        if run.dataset.material(item.product).mtype.value == "FINISHED"
    ]
    if finished:
        out.append("\n成本结构变化（仅产成品，按产品汇总）：")
        groups: dict[str, list[CostRestoration]] = {}
        for item in finished:
            groups.setdefault(item.product, []).append(item)
        for product, group in sorted(groups.items()):
            total = sum((g.finished_cost for g in group), Decimal(0))
            book_m = sum((g.book_material for g in group), Decimal(0))
            book_l = sum((g.book_labor for g in group), Decimal(0))
            book_o = sum((g.book_overhead for g in group), Decimal(0))
            rest_m = sum((g.material for g in group), Decimal(0))
            rest_l = sum((g.labor for g in group), Decimal(0))
            rest_o = sum((g.overhead for g in group), Decimal(0))
            name = run.dataset.material(product).name
            out.append(
                f"  {product} {name}（{len(group)} 张工单，完工成本 {money(total)}）：材料 "
                f"{book_m / total * 100:.2f}% → {rest_m / total * 100:.2f}%，"
                f"人工 {book_l / total * 100:.2f}% → {rest_l / total * 100:.2f}%，"
                f"制造费用 {book_o / total * 100:.2f}% → {rest_o / total * 100:.2f}%"
            )
        out.append(
            "  账面「材料」被半成品转入抬高了，还原后才是真实的原材料占比；"
            "差额就是上游工序的加工费。"
        )
    return "\n".join(out)


def render_variance_trace(run: CostRun) -> str:
    from .attribution import LABELS, VarianceTracer

    tracer = VarianceTracer(run)
    traces = [t for t in tracer.all() if t.pass_through != 0]
    out = ["差异穿透：成品超支是谁造成的"]
    if not traces:
        return "\n".join(out + ["  本期没有半成品结转，全部差异都发生在工单自身。"])

    for trace in traces:
        out.append(f"\n▌{trace.order_no} {trace.product} {trace.name}")
        out.append(
            f"  差异 {money(trace.total)} = 自身 {money(trace.own_total)}"
            f" + 上游转入 {money(trace.pass_through)}"
        )
        own = " / ".join(
            f"{LABELS[key]} {money(trace.own_components.get(key, Decimal(0)))}"
            for key in trace.own_components
        )
        out.append(f"  自身：{own}")
        for item in trace.upstream:
            out.append(
                f"  ← {item.order_no} {item.product} {item.name} {qty(item.qty)} 件，"
                f"转入 {money(item.amount)}"
            )
            parts = " / ".join(
                f"{LABELS[key]} {money(item.components.get(key, Decimal(0)))}"
                for key in LABELS
            )
            out.append(f"      {parts}")
    out.append(
        "\n说明：转入金额按实际结转账与标准成本之差计算，是精确值；"
        "上游差异构成按各分项比例映射，用于回答「钱花在哪一类」。"
    )
    return "\n".join(out)


def render_trial_balance(rows: list[TrialBalanceRow]) -> str:
    body: list[list[str]] = [
        [
            row.code,
            row.name,
            _balance_cell(row.opening),
            money(row.debit),
            money(row.credit),
            _balance_cell(row.closing),
        ]
        for row in rows
    ]
    debit_total = sum((row.debit for row in rows), Decimal(0))
    credit_total = sum((row.credit for row in rows), Decimal(0))
    closing_total = sum((row.closing for row in rows), Decimal(0))
    body.append(
        [
            "合计",
            "",
            "",
            money(q_money(debit_total)),
            money(q_money(credit_total)),
            _balance_cell(q_money(closing_total)),
        ]
    )
    text = render_table(
        ["科目", "名称", "期初余额", "本期借方", "本期贷方", "期末余额"],
        body,
        ["l", "l", "r", "r", "r", "r"],
    )
    note = (
        "\n试算平衡：本期借贷相等，期末余额合计为 0。"
        if q_money(debit_total) == q_money(credit_total) and q_money(closing_total) == 0
        else "\n试算不平衡，请检查凭证与期初余额。"
    )
    return text + note
