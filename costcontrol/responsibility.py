"""责任归因层：差异事实 × 责任映射 → 责任主体、证据与置信度。

这一层回答的不是「差多少」，而是「该找谁」。规则全部来自业务 Sense 的
``attribution.rules``：按成本要素匹配，责任人可以用 ``{{ cost_center }}``
这类占位符从事实与组织映射里解析；解析不到就退回人工复核，不瞎猜。

置信度只反映**证据完整度**，不代表金额准确性：
- 高：规则命中，责任人已解析，规则要求的证据齐全；
- 中：规则命中，但缺少部分证据（例如供应商未维护）；
- 低：规则未命中或责任人解析失败，转人工。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .facts import VarianceFact
from .llm_explainer import BusinessSense
from .money import ZERO, q_money
from .report import money, render_table

TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


@dataclass(frozen=True)
class AttributionRule:
    conditions: dict[str, str]
    owner: str
    secondary: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def cost_element(self) -> str:
        return self.conditions.get("cost_element", "")


@dataclass
class Attribution:
    fact: VarianceFact
    owner: str
    secondary: str
    confidence: str
    missing_evidence: list[str] = field(default_factory=list)
    matched: bool = True


def load_rules(sense: BusinessSense) -> tuple[list[AttributionRule], str]:
    config = sense.raw.get("attribution", {}) or {}
    rules: list[AttributionRule] = []
    for item in config.get("rules", []) or []:
        when = item.get("when", {}) or {}
        conditions = {str(key): str(value) for key, value in when.items()}
        if "cost_element" not in conditions:
            continue
        rules.append(
            AttributionRule(
                conditions=conditions,
                owner=str(item.get("owner", "") or ""),
                secondary=str(item.get("secondary", "") or ""),
                evidence=tuple(item.get("evidence", []) or ()),
            )
        )
    fallback = str(config.get("fallback", "成本会计复核") or "成本会计复核")
    return rules, fallback


def _rule_matches(rule: AttributionRule, fact: VarianceFact) -> bool:
    dimensions = fact.dimensions
    return all(
        str(dimensions.get(key, "")) == value
        for key, value in rule.conditions.items()
    )


def _field_value(fact: VarianceFact, org: dict[str, Any], name: str) -> str:
    if name == "cost_center":
        code = fact.cost_center
        return str((org.get("cost_centers", {}) or {}).get(code, code) or "")
    if name == "work_center":
        return fact.work_center
    if name == "supplier":
        return fact.supplier
    return str(fact.dimensions.get(name, "") or "")


def render_template(template: str, fact: VarianceFact, org: dict[str, Any]) -> str:
    """解析 {{ field }}；任一占位符为空则整体返回空串（交给 fallback）。"""
    if not template:
        return ""
    values: dict[str, str] = {}
    for match in TEMPLATE.finditer(template):
        name = match.group(1)
        value = _field_value(fact, org, name)
        if not value:
            return ""
        values[name] = value
    return TEMPLATE.sub(lambda m: values[m.group(1)], template)


def attribute(
    facts: list[VarianceFact],
    sense: BusinessSense,
) -> list[Attribution]:
    org = sense.raw.get("org", {}) or {}
    rules, fallback = load_rules(sense)

    result: list[Attribution] = []
    for fact in facts:
        candidates = [rule for rule in rules if _rule_matches(rule, fact)]
        rule = max(candidates, key=lambda r: len(r.conditions), default=None)
        if rule is None:
            result.append(
                Attribution(
                    fact=fact,
                    owner=fallback,
                    secondary="",
                    confidence="低",
                    missing_evidence=[],
                    matched=False,
                )
            )
            continue

        owner = render_template(rule.owner, fact, org)
        secondary = render_template(rule.secondary, fact, org) if rule.secondary else ""
        missing = [key for key in rule.evidence if not fact.evidence.get(key)]
        if not owner:
            owner = fallback
            confidence = "低"
        elif missing:
            confidence = "中"
        else:
            confidence = "高"
        result.append(
            Attribution(
                fact=fact,
                owner=owner,
                secondary=secondary,
                confidence=confidence,
                missing_evidence=missing,
                matched=bool(owner and owner != fallback),
            )
        )
    return result


def _detail_line(index: int, item: Attribution) -> list[str]:
    fact = item.fact
    subject = " · ".join(
        part for part in (fact.material, fact.order_no) if part
    ) or fact.order_no
    note = f"（置信度：{item.confidence}"
    if item.missing_evidence:
        note += "，缺 " + "、".join(item.missing_evidence)
    note += "）"
    lines = [f"{index}. [{fact.label}] {subject}  {money(fact.amount)} → {item.owner}{note}"]
    dims = []
    if fact.product:
        dims.append(f"产品 {fact.product}")
    if fact.cost_center:
        dims.append(f"成本中心 {fact.cost_center}")
    if fact.work_center:
        dims.append(f"工作中心 {fact.work_center}")
    if fact.supplier:
        dims.append(f"供应商 {fact.supplier}")
    if dims:
        lines.append("   " + " / ".join(dims))
    if item.secondary:
        lines.append(f"   并挂责任：{item.secondary}")
    if fact.evidence:
        evidence = "；".join(f"{k}={v}" for k, v in sorted(fact.evidence.items()))
        lines.append(f"   证据：{evidence}")
    return lines


def render_report(attributions: list[Attribution], sense: BusinessSense) -> str:
    if not attributions:
        return "差异归因报告：本期没有非零差异事实。"

    period = attributions[0].fact.period
    total = q_money(sum((a.fact.amount for a in attributions), ZERO))
    by_owner: dict[str, list[Attribution]] = {}
    for item in attributions:
        by_owner.setdefault(item.owner, []).append(item)

    auto = [a for a in attributions if a.matched]
    manual = [a for a in attributions if not a.matched]
    high = sum(1 for a in attributions if a.confidence == "高")

    lines = [
        f"差异归因报告 · {period}",
        "=" * 48,
        f"差异事实 {len(attributions)} 条，合计 {money(total)}；"
        f"自动归因 {len(auto)} 条（高置信 {high} 条），转人工 {len(manual)} 条。",
        "",
        "一、按责任主体汇总",
    ]
    rows = []
    for owner, items in sorted(
        by_owner.items(), key=lambda kv: -abs(sum((a.fact.amount for a in kv[1]), ZERO))
    ):
        amount = q_money(sum((a.fact.amount for a in items), ZERO))
        rows.append([owner, str(len(items)), money(amount)])
    lines.append(render_table(["责任主体", "条数", "差异金额"], rows, ["l", "r", "r"]))

    lines.append("")
    lines.append("二、逐条归因（按金额降序）")
    ordered = sorted(attributions, key=lambda a: (-abs(a.fact.amount), a.fact.fact_id))
    for index, item in enumerate(ordered, 1):
        lines.extend(_detail_line(index, item))

    if manual:
        lines.append("")
        lines.append("三、未能自动归因（转人工复核）")
        for item in manual:
            lines.append(
                f"- [{item.fact.label}] {item.fact.order_no} {money(item.fact.amount)}："
                f"未命中责任规则，附事实包转成本会计"
            )

    lines.append("")
    lines.append(
        "口径：金额来自确定性成本引擎（逐条取整，合计与工单总差异可能相差 1 分）；"
        "责任映射来自业务 Sense，置信度只代表证据完整度。报告不替代审批。"
    )
    return "\n".join(lines)


__all__ = [
    "Attribution",
    "AttributionRule",
    "attribute",
    "load_rules",
    "render_report",
    "render_template",
]
