"""反证复核：财务对 AI 结论有疑问时，用反证重新评估置信度。

强归因给出的是「证据 + 反证条件」，这一步让用户把反证真正用起来：
- 反证是否命中主因（退料、报工、报废、价格……）；
- 命中则下调置信度，并说明哪条证据被削弱；
- 给出下一步动作（补什么单、找谁核实）；
- 置信度低于阈值时明确标「需补充证据」，而不是硬撑原结论。

结构化反证
----------
模型通过 Function Calling 调用 ``update_confidence``，参数走严格校验：

- ``hit_evidence`` 只能引用 ``E1..En``，不能自造证据；
- ``adjusted_score`` 有绝对边界与相对原结论的跳变上限；
- ``verdict`` 必须与分数、命中证据自洽；
- 任何一项不通过，直接回退到离线规则 ``challenge()``，并记录原因。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .diagnosis import Diagnosis
from .llm_explainer import BusinessSense, ChatPrompt, LLMError, LLMProvider, ToolCall

TOOL_NAME = "update_confidence"

VERDICT_LABELS = {
    "upheld": "维持原结论",
    "weakened": "证据被削弱",
    "overturned": "原结论被推翻",
    "needs_evidence": "证据不足，需补证",
}

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


class ChallengeValidationError(ValueError):
    """工具参数不满足业务语义约束。"""


@dataclass(frozen=True)
class ChallengeAction:
    owner: str
    action: str
    evidence_needed: str


@dataclass(frozen=True)
class ChallengeDecision:
    verdict: str
    hit_evidence: tuple[str, ...]
    adjusted_score: float
    rationale: str
    actions: tuple[ChallengeAction, ...]


@dataclass
class ChallengeReview:
    text: str
    provider: str
    model: str
    prompt: ChatPrompt
    decision: ChallengeDecision | None = None
    fallback_reason: str = ""


TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "根据财务反证重新评估归因结论的置信度。"
            "只能引用给定的证据 ID，不得编造单据、金额或证据。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": list(VERDICT_LABELS),
                },
                "hit_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "被反证命中的证据 ID，例如 E1、E2；不能自造。",
                },
                "adjusted_score": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "rationale": {"type": "string"},
                "actions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "owner": {"type": "string"},
                            "action": {"type": "string"},
                            "evidence_needed": {"type": "string"},
                        },
                        "required": ["owner", "action", "evidence_needed"],
                    },
                },
            },
            "required": [
                "verdict",
                "hit_evidence",
                "adjusted_score",
                "rationale",
                "actions",
            ],
        },
    },
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


def evidence_items(diagnosis: Diagnosis) -> dict[str, str]:
    top = diagnosis.top
    if top is None:
        return {}
    return {f"E{index}": text for index, text in enumerate(top.evidence, 1)}


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


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChallengeValidationError(f"{field_name} 必须是非空字符串")
    if len(value) > 500:
        raise ChallengeValidationError(f"{field_name} 超过 500 字")
    return value.strip()


def decision_from_arguments(arguments: dict[str, Any]) -> ChallengeDecision:
    if not isinstance(arguments, dict):
        raise ChallengeValidationError("工具参数必须是对象")
    expected = {"verdict", "hit_evidence", "adjusted_score", "rationale", "actions"}
    extra = set(arguments) - expected
    missing = expected - set(arguments)
    if extra:
        raise ChallengeValidationError(f"工具参数包含未知字段：{sorted(extra)}")
    if missing:
        raise ChallengeValidationError(f"工具参数缺少字段：{sorted(missing)}")

    verdict = arguments["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICT_LABELS:
        raise ChallengeValidationError(f"未知 verdict：{verdict}")

    raw_hit = arguments["hit_evidence"]
    if not isinstance(raw_hit, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in raw_hit
    ):
        raise ChallengeValidationError("hit_evidence 必须是非空字符串数组")
    hit = tuple(item.strip() for item in raw_hit)
    if len(hit) > 8:
        raise ChallengeValidationError("hit_evidence 最多 8 条")

    score = arguments["adjusted_score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ChallengeValidationError("adjusted_score 必须是数字")
    score = float(score)
    if not math.isfinite(score):
        raise ChallengeValidationError("adjusted_score 必须是有限数字")

    rationale = _require_text(arguments["rationale"], "rationale")

    raw_actions = arguments["actions"]
    if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 3:
        raise ChallengeValidationError("actions 必须是 1–3 条的数组")
    actions = []
    for index, item in enumerate(raw_actions, 1):
        if not isinstance(item, dict):
            raise ChallengeValidationError(f"actions[{index}] 必须是对象")
        keys = {"owner", "action", "evidence_needed"}
        if set(item) != keys:
            raise ChallengeValidationError(
                f"actions[{index}] 字段必须恰好是 {sorted(keys)}"
            )
        actions.append(
            ChallengeAction(
                owner=_require_text(item["owner"], f"actions[{index}].owner"),
                action=_require_text(item["action"], f"actions[{index}].action"),
                evidence_needed=_require_text(
                    item["evidence_needed"], f"actions[{index}].evidence_needed"
                ),
            )
        )

    return ChallengeDecision(
        verdict=verdict,
        hit_evidence=hit,
        adjusted_score=score,
        rationale=rationale,
        actions=tuple(actions),
    )


def validate_decision(decision: ChallengeDecision, diagnosis: Diagnosis) -> None:
    top = diagnosis.top
    if top is None:
        raise ChallengeValidationError("该差异没有可复核的结论")
    if decision.verdict not in VERDICT_LABELS:
        raise ChallengeValidationError(f"未知 verdict：{decision.verdict}")

    original = float(top.score)
    score = decision.adjusted_score
    if not 0.0 <= score <= 1.0:
        raise ChallengeValidationError(f"adjusted_score 超出 0–1：{score}")
    if score - original > 0.10:
        raise ChallengeValidationError(
            f"没有新证据却上调超过 0.10：{original:.2f} → {score:.2f}"
        )
    if original - score > 0.50:
        raise ChallengeValidationError(
            f"下调超过 0.50：{original:.2f} → {score:.2f}"
        )

    ids = set(evidence_items(diagnosis))
    unknown = set(decision.hit_evidence) - ids
    if unknown:
        raise ChallengeValidationError(f"引用了不存在的证据 ID：{sorted(unknown)}")

    if decision.verdict == "upheld":
        if decision.hit_evidence:
            raise ChallengeValidationError("verdict=upheld 时 hit_evidence 必须为空")
        if abs(score - original) > 0.05:
            raise ChallengeValidationError("verdict=upheld 时置信度变化不能超过 0.05")
    elif decision.verdict == "weakened":
        if not decision.hit_evidence:
            raise ChallengeValidationError("verdict=weakened 时必须命中至少一条证据")
        drop = original - score
        if not 0.05 <= drop <= 0.45:
            raise ChallengeValidationError(
                f"verdict=weakened 时下调幅度应在 0.05–0.45，实际 {drop:.2f}"
            )
    elif decision.verdict == "overturned":
        if not decision.hit_evidence:
            raise ChallengeValidationError("verdict=overturned 时必须命中至少一条证据")
        if score > 0.30:
            raise ChallengeValidationError("verdict=overturned 时置信度应降到 0.30 以下")
    elif decision.verdict == "needs_evidence":
        if score > original:
            raise ChallengeValidationError("verdict=needs_evidence 时置信度不能上调")
        if original - score > 0.15:
            raise ChallengeValidationError("证据不足时下调不应超过 0.15")

    for action in decision.actions:
        if len(action.owner) > 50 or len(action.action) > 200:
            raise ChallengeValidationError("action 文本过长")


def render_decision(decision: ChallengeDecision, diagnosis: Diagnosis) -> str:
    top = diagnosis.top
    subject = " · ".join(
        part for part in (diagnosis.fact.material, diagnosis.fact.order_no) if part
    )
    evidence = evidence_items(diagnosis)
    hit = (
        "、".join(f"{item}（{evidence[item]}）" for item in decision.hit_evidence)
        if decision.hit_evidence
        else "无（反证未命中既有证据）"
    )
    lines = [
        f"## 反证复核 · {diagnosis.fact.label} · {subject}",
        f"- 原结论：{top.label if top else '无'}（{top.score:.2f}，{_confidence(top.score)}）",
        f"- 裁决：{VERDICT_LABELS[decision.verdict]}（{decision.verdict}）",
        f"- 命中证据：{hit}",
        f"- 调整后：{decision.adjusted_score:.2f}（{_confidence(decision.adjusted_score)}）",
        f"- 理由：{decision.rationale}",
        "- 下一步：",
    ]
    for index, action in enumerate(decision.actions, 1):
        lines.append(
            f"  {index}. {action.owner}：{action.action}"
            f"（需补：{action.evidence_needed}）"
        )
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
            "证据": [
                {"id": evidence_id, "text": evidence_text}
                for evidence_id, evidence_text in evidence_items(diagnosis).items()
            ],
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
调用 `{TOOL_NAME}` 工具，返回结构化裁决。

## 约束
- hit_evidence 只能引用上面列出的证据 ID（E1、E2……），不能自造；
- verdict 与 adjusted_score 必须一致：
  - upheld：未命中，分数变化不超过 ±0.05；
  - weakened：命中原证据，下调 0.05–0.45；
  - overturned：命中核心证据，分数 ≤ 0.30；
  - needs_evidence：无法判断，分数不上调，并给出补证动作；
- 没有新证据时，分数不得上调超过 0.10；
- actions 1–3 条，每条写清 owner、action、evidence_needed；
- 如果你认为反证不足以推翻结论，也要明确还需要什么证据。"""
    return ChatPrompt(system=system, user=user)


def _offline_review(
    diagnosis: Diagnosis,
    text: str,
    prompt: ChatPrompt,
    reason: str = "",
) -> ChallengeReview:
    outcome = challenge(diagnosis, text)
    body = render_challenge(outcome)
    if reason:
        body = (
            f"> 结构化输出未通过校验（{reason}），已回退离线规则。\n\n" + body
        )
    return ChallengeReview(
        text=body,
        provider="fallback" if reason else "offline",
        model="rule-based",
        prompt=prompt,
        decision=None,
        fallback_reason=reason,
    )


def review_with_llm(
    diagnosis: Diagnosis,
    text: str,
    sense: BusinessSense,
    provider: LLMProvider | None,
) -> ChallengeReview:
    prompt = build_challenge_prompt(diagnosis, text, sense)
    if provider is None:
        return _offline_review(diagnosis, text, prompt)

    complete_tool = getattr(provider, "complete_tool", None)
    if not callable(complete_tool):
        return _offline_review(
            diagnosis, text, prompt, "provider 不支持工具调用"
        )

    try:
        call = complete_tool(prompt, TOOL_SCHEMA)
        if not isinstance(call, ToolCall):
            raise ChallengeValidationError("provider 返回的不是 ToolCall")
        if call.name != TOOL_NAME:
            raise ChallengeValidationError(f"模型调用了未知函数：{call.name}")
        decision = decision_from_arguments(call.arguments)
        validate_decision(decision, diagnosis)
    except (
        LLMError,
        ChallengeValidationError,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return _offline_review(diagnosis, text, prompt, str(exc))

    return ChallengeReview(
        text=render_decision(decision, diagnosis),
        provider=provider.name,
        model=provider.model,
        prompt=prompt,
        decision=decision,
    )


def record_review(
    review: ChallengeReview,
    diagnosis: Diagnosis,
    path: str | Path,
) -> dict[str, Any] | None:
    """把结构化裁决追加到 JSONL，供审计与后续学习。"""
    if review.decision is None:
        return None
    top = diagnosis.top
    record = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fact_id": getattr(diagnosis.fact, "fact_id", ""),
        "cost_element": diagnosis.fact.cost_element,
        "order_no": diagnosis.fact.order_no,
        "material": diagnosis.fact.material,
        "original_label": top.label if top else "",
        "original_score": top.score if top else 0,
        "verdict": review.decision.verdict,
        "hit_evidence": list(review.decision.hit_evidence),
        "adjusted_score": review.decision.adjusted_score,
        "rationale": review.decision.rationale,
        "actions": [
            {
                "owner": action.owner,
                "action": action.action,
                "evidence_needed": action.evidence_needed,
            }
            for action in review.decision.actions
        ],
        "provider": review.provider,
        "model": review.model,
        "fallback_reason": review.fallback_reason,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


__all__ = [
    "ChallengeAction",
    "ChallengeDecision",
    "ChallengeOutcome",
    "ChallengeReview",
    "ChallengeValidationError",
    "TOOL_NAME",
    "TOOL_SCHEMA",
    "build_challenge_prompt",
    "challenge",
    "decision_from_arguments",
    "evidence_items",
    "pick_target",
    "record_review",
    "render_challenge",
    "render_decision",
    "review_with_llm",
    "validate_decision",
]
