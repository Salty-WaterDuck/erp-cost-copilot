"""强归因诊断：从「该找谁」走到「为什么、凭什么、什么能推翻它」。

责任映射（responsibility.py）回答的是组织归属，这一层回答因果：

    差异事实 → 候选原因 → 证据链 → 反证条件 → 置信度

每个候选原因都必须给出：
- **证据**：来自哪张单、哪个数字（可追溯到 `VarianceFact.evidence` 与原始单据）；
- **反证条件**：什么情况下这个判断不成立（让人能证伪，而不是盲信）；
- **缺失证据**：还需要什么数据才能升级置信度。

原则：宁可标「证据不足」，也不给一个看起来很像的高置信结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .facts import VarianceFact
from .money import q_money
from .run import CostRun


@dataclass
class Hypothesis:
    code: str
    label: str
    score: float
    evidence: list[str] = field(default_factory=list)
    falsifier: str = ""

    @property
    def confidence(self) -> str:
        if self.score >= 0.7:
            return "高"
        if self.score >= 0.45:
            return "中"
        return "低"


@dataclass
class Diagnosis:
    fact: VarianceFact
    candidates: list[Hypothesis]

    @property
    def top(self) -> Hypothesis | None:
        return self.candidates[0] if self.candidates else None

    @property
    def confidence(self) -> str:
        return self.top.confidence if self.top else "低"


def _money(value: Decimal | float | str) -> str:
    return f"{Decimal(str(value)):,.2f}"


def _qty(value: Decimal | float | str) -> str:
    text = f"{Decimal(str(value)):,.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _ratio(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def _material_context(fact: VarianceFact, run: CostRun):
    cost = run.sheet.order(fact.order_no)
    line = next(
        (l for l in cost.material_lines if l.material == fact.material),
        None,
    )
    if line is None:
        return None, None, []
    returns = [
        issue
        for issue in run.dataset.issues
        if issue.order_no == fact.order_no
        and issue.material == fact.material
        and issue.is_return
    ]
    return cost, line, returns


def _diagnose_material(fact: VarianceFact, run: CostRun) -> list[Hypothesis]:
    cost, line, returns = _material_context(fact, run)
    if line is None:
        return [Hypothesis("NO-DATA", "缺少该物料明细", 0.0, ["成本单中无此物料行"], "需补导领料明细")]

    material = run.dataset.material(fact.material)
    over_qty = line.qty - line.std_qty
    ratio = (over_qty / line.std_qty) if line.std_qty else Decimal(0)
    result: list[Hypothesis] = []

    if line.std_qty == 0:
        result.append(
            Hypothesis(
                "SUBSTITUTE",
                "替代料 / 串料",
                0.85,
                [
                    f"标准用量为 0，但实际领用 {_qty(line.qty)}",
                    "该物料不在当前 BOM 中",
                ],
                "若该物料已纳入 BOM 或工单临时用料审批，则不是串料",
            )
        )
        return result

    if over_qty <= 0:
        result.append(
            Hypothesis(
                "SAVING",
                "实际单耗低于定额",
                min(0.8, 0.5 + float(abs(ratio))),
                [
                    f"领用 {_qty(line.qty)} vs 标准 {_qty(line.std_qty)}，节约 {_qty(-over_qty)}",
                    f"实际单价 {_money(line.unit_price)} vs 标准 {_money(line.std_price)}",
                ],
                "若存在退料或替代料未入账，节约可能是账面现象",
            )
        )
        return result

    if cost.wip_qty > 0 and cost.material_completion < 1:
        result.append(
            Hypothesis(
                "EARLY-ISSUE",
                "提前领料（时间性差异）",
                0.65,
                [
                    f"期末在产 {_qty(cost.wip_qty)}，投料程度 {_ratio(cost.material_completion)}",
                    "材料已领但尚未形成产出，可能只是领料早于消耗",
                ],
                "若在产投料程度为 100%（材料已全部投入），则不是时间性差异",
            )
        )

    returned = sum((issue.qty for issue in returns), Decimal(0))
    if not returns:
        result.append(
            Hypothesis(
                "RETURN-MISSING",
                "退料未入库 / 超耗",
                min(0.9, 0.55 + float(min(abs(ratio), 0.2))),
                [
                    f"超领 {_qty(over_qty)}（{_ratio(ratio)}）",
                    "本期该工单该物料无退料单",
                ],
                "若存在未入账退料单，或超耗已被报废单覆盖，则结论不成立",
            )
        )
    else:
        result.append(
            Hypothesis(
                "OVERUSE",
                "净领用超耗（已扣退料）",
                min(0.85, 0.5 + float(min(abs(ratio), 0.25))),
                [
                    f"超领 {_qty(over_qty)}（{_ratio(ratio)}）",
                    f"已退料 {_qty(abs(returned))}，净领仍高于标准",
                ],
                "若存在未入账退料，或报废单能覆盖差额，则不是真实超耗",
            )
        )

    if cost.qty_scrapped > 0:
        result.append(
            Hypothesis(
                "SCRAP",
                "报废未足额上报",
                0.2,
                [
                    f"工单已报报废 {_qty(cost.qty_scrapped)}",
                    "报废已计入约当量，若实际报废更多则会表现为超耗",
                ],
                "若报废单已覆盖全部损耗，则不是未报报废",
            )
        )

    if abs(ratio) <= Decimal("0.02"):
        result.append(
            Hypothesis(
                "TOLERANCE",
                "容差内波动",
                0.4,
                [f"超耗比例 {_ratio(ratio)}，在 2% 容差内"],
                "若连续多期同向超耗，需要检查定额而不是归为波动",
            )
        )

    result.append(
        Hypothesis(
            "HISTORY",
            "定额偏紧（需历史验证）",
            0.15,
            ["需要该物料 ≥3 期的单耗序列才能判断"],
            "若历史单耗稳定且接近定额，则排除定额问题",
        )
    )
    return result


def _diagnose_labor(fact: VarianceFact, run: CostRun) -> list[Hypothesis]:
    cost = run.sheet.order(fact.order_no)
    line = next(
        (l for l in cost.labor_lines if l.cost_center == fact.cost_center),
        None,
    )
    if line is None:
        return [Hypothesis("NO-DATA", "缺少人工明细", 0.0, [], "需补导报工单")]

    result: list[Hypothesis] = []
    hours_up = line.hours > line.std_hours
    rate_up = line.rate > line.std_rate

    if rate_up and hours_up:
        result.append(
            Hypothesis(
                "OVERTIME",
                "加班 / 高费率工时集中",
                0.75,
                [
                    f"实际 {_qty(line.hours)}h vs 标准 {_qty(line.std_hours)}h",
                    f"实际费率 {_money(line.rate)} vs 标准 {_money(line.std_rate)}",
                ],
                "若工时包含换线、待料或返工，需工序级工时拆分后再判断",
            )
        )
    elif hours_up:
        result.append(
            Hypothesis(
                "SKILL-SCHEDULE",
                "技能不足 / 排产不顺",
                0.55,
                [
                    f"实际 {_qty(line.hours)}h vs 标准 {_qty(line.std_hours)}h",
                    "费率未超标准，效率损失更可能来自熟练度或换线",
                ],
                "若工序级工时显示准备工时占比高，则应归因排产而非技能",
            )
        )
    elif rate_up:
        result.append(
            Hypothesis(
                "WAGE-ADJUST",
                "工资率调整",
                0.6,
                [
                    f"实际费率 {_money(line.rate)} vs 标准 {_money(line.std_rate)}",
                    "工时未超标准",
                ],
                "若费率差异只出现在个别月份，可能是加班补贴而非普调",
            )
        )

    result.append(
        Hypothesis(
            "OPERATION-DETAIL",
            "需工序级工时拆分",
            0.2,
            ["当前只汇总到成本中心，无法区分换线/待料/返工"],
            "补齐工序级报工后，该假设可被验证或排除",
        )
    )
    return result


def _diagnose_overhead(fact: VarianceFact, run: CostRun) -> list[Hypothesis]:
    cost = run.sheet.order(fact.order_no)
    line = next(
        (l for l in cost.overhead_lines if l.cost_center == fact.cost_center),
        None,
    )
    if line is None:
        return [Hypothesis("NO-DATA", "缺少费用明细", 0.0, [], "需补导费用单")]

    center = run.dataset.center(fact.cost_center)
    items = [o for o in run.dataset.overhead if o.cost_center == fact.cost_center]
    total = sum((o.amount for o in items), Decimal(0))
    top_item = max(items, key=lambda o: o.amount, default=None)
    result: list[Hypothesis] = []

    volume_down = line.hours < line.std_hours
    if volume_down and line.spending_variance > 0:
        result.append(
            Hypothesis(
                "VOLUME",
                "产能利用不足（固定费用未吸收）",
                0.65,
                [
                    f"实际工时 {_qty(line.hours)}h 低于标准 {_qty(line.std_hours)}h",
                    f"费用 {_money(line.amount)} 高于按实际工时应有的 {_money(line.budgeted_at_actual)}",
                ],
                "若费用本身也同比下降，则不是产能问题而是费用管控",
            )
        )

    if line.amount > line.budgeted_at_actual:
        result.append(
            Hypothesis(
                "SPENDING",
                "费用本身超支",
                0.6,
                [
                    f"实际 {_money(line.amount)} vs 按实际工时应发生 {_money(line.budgeted_at_actual)}",
                    f"成本中心预算 {_money(center.overhead_budget)}",
                ],
                "若超支集中在一次性项目（维修/外委），应单独列示而非视为常态",
            )
        )

    if top_item is not None and total > 0:
        share = top_item.amount / total
        if share >= Decimal("0.4"):
            result.append(
                Hypothesis(
                    "CONCENTRATION",
                    f"单项费用集中：{top_item.expense_type}",
                    0.55,
                    [
                        f"{top_item.expense_type} {_money(top_item.amount)}，占该中心 {_ratio(share)}",
                        f"共 {len(items)} 项费用，合计 {_money(total)}",
                    ],
                    "若该项是固定折旧/薪酬，属于结构性问题，不能按一次性超支处理",
                )
            )

    if line.hours > line.std_hours:
        result.append(
            Hypothesis(
                "EFFICIENCY",
                "工时超标准带动费用",
                0.5,
                [
                    f"实际 {_qty(line.hours)}h vs 标准 {_qty(line.std_hours)}h",
                    "费用按工时分摊，工时超支会同步放大费用",
                ],
                "若费用分配率本身下降，则工时不是主因",
            )
        )
    return result


def _diagnose_price(fact: VarianceFact, run: CostRun) -> list[Hypothesis]:
    material = run.dataset.material(fact.material)
    if material.mtype.value == "SEMI":
        producers = [
            o
            for o in run.sheet.orders
            if o.product == fact.material and o.qty_completed > 0
        ]
        upstream = "、".join(o.order_no for o in producers) or "未知"
        variance = q_money(sum((o.variance_total for o in producers), Decimal(0)))
        return [
            Hypothesis(
                "TRANSFER",
                "上游工单成本转入",
                0.8,
                [
                    f"半成品 {fact.material} 本期由 {upstream} 生产",
                    f"上游工单差异合计 {_money(variance)}（同向传导）",
                ],
                "若上游工单按标准价结转或差异方向相反，则不是上游问题",
            )
        ]

    direction = "上涨" if fact.amount > 0 else "下降"
    supplier = fact.supplier or "供应商未维护"
    actual_price = _money(fact.evidence.get("unit_price", "0"))
    std_price = _money(fact.evidence.get("std_price", "0"))
    return [
        Hypothesis(
            "PURCHASE",
            f"采购价{direction}",
            0.6 if fact.supplier else 0.45,
            [
                f"单价 {actual_price} vs 标准 {std_price}",
                f"供应商：{supplier}",
            ],
            "若同期多家供应商报价未变，可能是标准价未更新而非采购涨价",
        ),
        Hypothesis(
            "STD-STALE",
            "标准价未更新",
            0.25,
            ["需要连续期间的价格序列才能判断标准价是否滞后"],
            "若标准价近期刚维护过，则排除",
        ),
    ]


def diagnose_facts(facts: list[VarianceFact], run: CostRun) -> list[Diagnosis]:
    handlers = {
        "MAT-QTY-VAR": _diagnose_material,
        "MAT-PRICE-VAR": _diagnose_price,
        "LABOR-EFF-VAR": _diagnose_labor,
        "LABOR-RATE-VAR": _diagnose_labor,
        "OH-SPEND-VAR": _diagnose_overhead,
        "OH-EFF-VAR": _diagnose_overhead,
    }
    result: list[Diagnosis] = []
    for fact in facts:
        handler = handlers.get(fact.cost_element)
        if handler is None:
            continue
        candidates = handler(fact, run)
        candidates.sort(key=lambda h: (-h.score, h.code))
        result.append(Diagnosis(fact=fact, candidates=candidates[:3]))
    return result


def render_diagnosis(diagnoses: list[Diagnosis], limit: int = 12) -> str:
    if not diagnoses:
        return "差异诊断：本期没有可诊断的差异事实。"

    period = diagnoses[0].fact.period
    ordered = sorted(diagnoses, key=lambda d: -abs(d.fact.amount))
    total = q_money(sum((d.fact.amount for d in diagnoses), Decimal(0)))
    lines = [
        f"差异诊断 · {period}",
        "=" * 48,
        f"诊断 {len(diagnoses)} 条差异，合计 {_money(total)}；"
        f"高置信 {sum(1 for d in diagnoses if d.confidence == '高')} 条，"
        f"中 {sum(1 for d in diagnoses if d.confidence == '中')} 条，"
        f"低 {sum(1 for d in diagnoses if d.confidence == '低')} 条。",
        "",
    ]
    for index, diagnosis in enumerate(ordered[:limit], 1):
        fact = diagnosis.fact
        top = diagnosis.top
        subject = " · ".join(p for p in (fact.material, fact.order_no) if p)
        lines.append(f"{index}. [{fact.label}] {subject}  {_money(fact.amount)}")
        if top is None:
            lines.append("   证据不足，建议转人工")
            lines.append("")
            continue
        lines.append(f"   主因：{top.label}（置信 {top.confidence} {top.score:.2f}）")
        for item in top.evidence:
            lines.append(f"   · {item}")
        if top.falsifier:
            lines.append(f"   反证：{top.falsifier}")
        alternatives = [c for c in diagnosis.candidates[1:] if c.score >= 0.2]
        if alternatives:
            lines.append(
                "   备选："
                + " / ".join(f"{c.label}（{c.score:.2f}）" for c in alternatives)
            )
        lines.append("")

    if len(diagnoses) > limit:
        lines.append(f"（另有 {len(diagnoses) - limit} 条未展开）")
    lines.append("说明：置信度是可解释的规则打分，不是概率；低置信项应转人工。")
    return "\n".join(lines)


__all__ = ["Diagnosis", "Hypothesis", "diagnose_facts", "render_diagnosis"]
