"""反证复核：财务对 AI 结论有疑问时，用反证重新评估置信度。

强归因给出的是「证据 + 反证条件」，这一步让用户把反证真正用起来：
- 反证是否命中主因（退料、报工、报废、价格……）；
- 命中则下调置信度，并说明哪条证据被削弱；
- 给出下一步动作（补什么单、找谁核实）；
- 置信度低于阈值时明确标「需补充证据」，而不是硬撑原结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .diagnosis import Diagnosis
from .llm_explainer import BusinessSense, ChatPrompt, Explanation, LLMProvider

KEYWORD_MAP: tuple[tuple[str, set[str], str], ...] = (
    ("退料", {"RETURN-MISSING", "OVERUSE"}, "退料"),
    ("退货", {"RETURN-MISSING", "OVERUSE"}, "退料"),
    ("报工", {"OVERTIME", "SKILL-SCHEDULE", "WAGE-ADJUST", "OPERATION-DETAIL"}, "报工"),
    ("工时", {"OVERTIME", "SKILL-SCHEDULE", "WAGE-ADJUST", "OPERATION-DETAIL"}, "报工"),
    ("报废", {"SCRAP"}, "报废"),
    ("废品", {"SCRAP"}, "报废"),
    ("价格", {"PURCHASE", "STD-STALE", "TRANSFER"}, "价格"),
    ("采购", {"PURCHASE", "STD-STALE", "TRANSFER"}, "价格"),
    ("提前领料", {"EARLY-ISSUE"}, "领料时点"),
    ("盘点", {"OVERUSE", "RETURN-MISSING"}, "盘点"),
)

ACTION_MAP = {
    "退料": "调取退料单与仓库红字入库记录；若确已退料未录，补单后重跑归因。",
    "报工": "调取考勤与报工原始记录，确认是否跨期、委外或漏登。",
    "报废": "调取报废单与废料入库单，核对报废数量能否覆盖超耗。",
    "价格": "调取采购订单、发票与入库单价，确认标准价是否滞后。",
    "领料时点": "核对领料日期与在产投料程度，确认是否只是时间性差异。",
    "盘点": "调取盘点表与库存台账，确认账实差异是否由盘亏造成。",
}


@dataclass
class ChallengeOutcome:
    fact_label: str
    subject: str
    original_code: str
    original_label: str
    original_score: float
    adjusted_score: float
    still_valid: bool
    matched: bool
    rationale: str
    actions: list[str]

    @property
    def original_confidence(self) -> str:
        return _confidence(self.original_score)

    @property
    def adjusted_confidence(self) -> str:
        return _confidence(self.adjusted_score)


def _confidence(score: float) -> str:
    if score >= 0.7:
        return "高"
    if score >= 0.45:
        return "中"
    return "低"


def _match(text: str, top_code: str) -> tuple[bool, str | None]:
    for keyword, codes, label in KEYWORD_MAP:
        if keyword in text and top_code in codes:
            return True, label
    return False, None


def challenge(diagnosis: Diagnosis, text: str) -> ChallengeOutcome:
    top = diagnosis.top
    if top is None:
        raise ValueError("该差异没有可复核的结论")
    matched, category = _match(text, top.code)
    drop = 0.25 if matched else 0.12
    adjusted = max(0.05, round(top.score - drop, 2))
    subject = " · ".join(
        part for part in (diagnosis.fact.material, diagnosis.fact.order_no) if part
    )
    if matched and category:
        rationale = (
            f"反证直接命中主因「{top.label}」：如果「{text}」属实，"
            f"现有证据链不完整，置信度由 {top.score:.2f} 下调到 {adjusted:.2f}。"
        )
        actions = [
            ACTION_MAP.get(category, "要求提供单据编号与日期，补录后重跑。"),
            "补证后重新运行 --diagnose，对比调整前后的置信度与主因。",
        ]
    else:
        rationale = (
            f"反证没有直接命中主因「{top.label}」，但仍提示存在未入账或未提供的证据，"
            f"置信度小幅下调到 {adjusted:.2f}。"
        )
        actions = [
            "要求反证方提供具体单据编号、日期与数量，避免口头说明。",
            "若无法补证，保留原结论但标注「待验证」，不要据此调整账务。",
        ]
    return ChallengeOutcome(
        fact_label=diagnosis.fact.label,
        subject=subject,
        original_code=top.code,
        original_label=top.label,
        original_score=top.score,
        adjusted_score=adjusted,
        still_valid=adjusted >= 0.45,
        matched=matched,
        rationale=rationale,
        actions=actions,
    )


def pick_target(diagnoses: list[Diagnosis], text: str) -> Diagnosis:
    """优先挑被反证命中的差异；没有命中时退回金额最大的一条。"""
    for diagnosis in sorted(diagnoses, key=lambda item: -abs(item.fact.amount)):
        if diagnosis.top is None:
            continue
        matched, _category = _match(text, diagnosis.top.code)
        if matched:
            return diagnosis
    return max(diagnoses, key=lambda item: abs(item.fact.amount))


def render_challenge(outcome: ChallengeOutcome) -> str:
    lines = [
        f"## 反证复核 · {outcome.fact_label} · {outcome.subject}",
        f"- 原结论：{outcome.original_label}（置信 {outcome.original_confidence} "
        f"{outcome.original_score:.2f}）",
        f"- 复核：{outcome.rationale}",
        f"- 调整后：{outcome.adjusted_confidence}（{outcome.adjusted_score:.2f}）→ "
        + ("原结论仍成立，但证据需补强。" if outcome.still_valid else "原结论暂不成立，转人工核实。"),
        "- 下一步：",
    ]
    for index, action in enumerate(outcome.actions, 1):
        lines.append(f"  {index}. {action}")
    return "\n".join(lines)


def build_challenge_prompt(
    diagnosis: Diagnosis,
    text: str,
    sense: BusinessSense,
) -> ChatPrompt:
    top = diagnosis.top
    system = (
        "你是一名成本审计师。财务对之前的归因结论提出反证，你要重新评估置信度。"
        "只能依据给定的事实、证据与反证，不得编造单据或金额；"
        "如果反证不足以推翻结论，要明确说明还需要什么证据。"
    )
    payload = {
        "差异事实": {
            "要素": diagnosis.fact.cost_element,
            "金额": str(diagnosis.fact.amount),
            "工单": diagnosis.fact.order_no,
            "物料": diagnosis.fact.material,
            "成本中心": diagnosis.fact.cost_center,
        },
        "原结论": {
            "主因": top.label if top else "无",
            "置信度": top.score if top else 0,
            "证据": top.evidence if top else [],
            "反证条件": top.falsifier if top else "",
        },
        "财务反证": text,
        "业务Sense": sense.raw,
    }
    user = f"""## 输入
```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```

## 任务
重新评估原结论的置信度（0-1），输出中文 Markdown：
1. 反证命中了哪条证据，哪条没有；
2. 调整后的置信度与理由；
3. 下一步动作（找谁、补什么单、如何验证），不超过 3 条；
4. 明确写「原结论是否仍成立」。"""
    return ChatPrompt(system=system, user=user)


def review_with_llm(
    diagnosis: Diagnosis,
    text: str,
    sense: BusinessSense,
    provider: LLMProvider | None,
) -> Explanation:
    prompt = build_challenge_prompt(diagnosis, text, sense)
    if provider is None:
        outcome = challenge(diagnosis, text)
        return Explanation(
            text=render_challenge(outcome),
            provider="offline",
            model="rule-based",
            prompt=prompt,
        )
    result = provider.complete(prompt)
    return Explanation(
        text=result.text,
        provider=result.provider,
        model=result.model,
        prompt=prompt,
    )


__all__ = [
    "ChallengeOutcome",
    "build_challenge_prompt",
    "challenge",
    "pick_target",
    "render_challenge",
    "review_with_llm",
]
