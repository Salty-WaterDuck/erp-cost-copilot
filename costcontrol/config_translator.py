"""ERP 配置翻译：把分摊说明 / Costing Sheet 翻译成大白话，并指出会踩的坑。

输入是一段人写的配置文档（不是结构化数据），输出是给财务经理看的解释：
- 这笔费用按什么动因、分到哪些车间、比例多少；
- 哪里可能不准（动因未更新、比例不闭合、动因与系统不一致）；
- 建议谁去补什么。

解析是确定性的（关键词 + 比例 + 中心名），大模型只负责把结构化结果讲清楚，
不允许编造配置里没有的规则或数字。离线时有模板兜底。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .llm_explainer import BusinessSense, ChatPrompt, Explanation, LLMProvider
from .models import Dataset
from .money import q_money

POOL_KEYWORDS = (
    "电费",
    "水费",
    "蒸汽",
    "燃气",
    "租金",
    "折旧",
    "维修费",
    "管理薪酬",
    "制造费用",
    "辅料",
    "检测费",
)
DRIVER_KEYWORDS = (
    "机器工时",
    "人工工时",
    "工时",
    "产量",
    "面积",
    "人数",
    "用电量",
    "重量",
    "维修工单",
)
CENTER_PATTERN = re.compile(r"(CC-[A-Z0-9-]+|[A-Za-z\u4e00-\u9fa5]{2,8}(?:车间|中心|产线|维护组|组))")
PREFIXED_CENTER_PATTERN = re.compile(
    r"((?:总装|喷涂|质检|机加|冲压|装配|注塑|焊装|涂装|包装|设备|维修|仓储|动力|生产)"
    r"[\u4e00-\u9fa5]{0,4}?(?:车间|中心|产线|维护组|组))"
)
PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")
STALE_WORDS = ("未更新", "尚未", "待补", "未提供", "下月补")
NON_CENTER_WORDS = (
    "按",
    "到",
    "入",
    "给",
    "至",
    "在",
    "由",
    "为",
    "计入",
    "分摊",
    "全部",
    "各",
    "该",
    "其",
    "和",
    "与",
    "工单",
)


@dataclass(frozen=True)
class AllocationRule:
    pool: str
    driver: str
    centers: tuple[str, ...]
    shares: tuple[Decimal, ...]
    source: str

    @property
    def share_total(self) -> Decimal:
        return sum(self.shares, Decimal(0))


@dataclass
class ConfigReview:
    rules: list[AllocationRule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


def _first(text: str, words: tuple[str, ...]) -> str | None:
    for word in words:
        if word in text:
            return word
    return None


def _centers(line: str, ds: Dataset | None) -> tuple[str, ...]:
    found: dict[str, int] = {}
    if ds is not None:
        for center in ds.cost_centers.values():
            index = line.find(center.name)
            if index >= 0:
                found[center.name] = index
    for candidate in PREFIXED_CENTER_PATTERN.findall(line):
        found.setdefault(candidate, line.find(candidate))
    for candidate in CENTER_PATTERN.findall(line):
        if any(word in candidate for word in NON_CENTER_WORDS):
            continue
        found.setdefault(candidate, line.find(candidate))
    return tuple(name for name, _index in sorted(found.items(), key=lambda item: item[1]))


def parse_config(text: str, ds: Dataset | None = None) -> ConfigReview:
    review = ConfigReview(raw_text=text)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(word in line for word in STALE_WORDS):
            review.warnings.append(
                f"原文提示数据可能未就绪：「{line}」——分摊基数可能缺失或滞后。"
            )
            continue
        if "不参与分摊" in line or "直接计入" in line:
            review.notes.append(f"直接计入、不参与分摊：{line}")
            continue
        pool = _first(line, POOL_KEYWORDS)
        driver = _first(line, DRIVER_KEYWORDS)
        if not pool or not driver:
            continue
        centers = _centers(line, ds)
        shares = tuple(Decimal(value) for value in PERCENT_PATTERN.findall(line))
        if not centers:
            review.notes.append(f"「{line}」只写了动因，没有指明分摊对象。")
            continue
        rule = AllocationRule(
            pool=pool,
            driver=driver,
            centers=centers,
            shares=shares,
            source=line,
        )
        review.rules.append(rule)
        if shares and sum(shares, Decimal(0)) != Decimal(100):
            review.warnings.append(
                f"{pool} 的分摊比例合计 {sum(shares, Decimal(0))}%，不等于 100%。"
            )

    if not review.rules:
        review.warnings.append("没有识别出任何分摊规则，请确认文档里是否包含「费用/动因/车间」。")

    if ds is not None:
        machine_warned = False
        warned_centers: set[str] = set()
        for rule in review.rules:
            if rule.driver == "机器工时" and not machine_warned:
                review.warnings.append(
                    "配置中多条规则使用机器工时，但系统当前按人工工时归集；"
                    "机器工时需要在动因配置中显式启用。"
                )
                machine_warned = True
            for center_name in rule.centers:
                center = _match_center(ds, center_name)
                if center is None or center in warned_centers:
                    continue
                if ds.labor and not any(r.cost_center == center for r in ds.labor):
                    review.warnings.append(
                        f"{center_name}（{center}）有分摊规则，但本期没有任何报工记录，"
                        "分摊动因为 0，相关费用可能整笔挂账。"
                    )
                    warned_centers.add(center)
    return review


def _match_center(ds: Dataset, name: str) -> str | None:
    for center in ds.cost_centers.values():
        if name in (center.code, center.name) or center.name in name:
            return center.code
    return None


def build_translate_prompt(review: ConfigReview, sense: BusinessSense) -> ChatPrompt:
    rules = [
        {
            "费用": rule.pool,
            "动因": rule.driver,
            "对象": list(rule.centers),
            "比例": [str(value) for value in rule.shares],
        }
        for rule in review.rules
    ]
    system = (
        "你是一名 ERP 成本顾问，擅长把分摊配置翻译成财务经理能听懂的话。"
        "你只能依据用户给的配置原文与结构化解析，不得编造费用项目、车间、比例或金额；"
        "不确定的地方要明确写「需确认」。"
    )
    user = f"""## 任务
把下面这段 ERP 分摊配置翻译成大白话，并指出可能踩的坑。

## 配置原文
```text
{review.raw_text}
```

## 结构化解析（只依据原文）
```json
{json.dumps(rules, ensure_ascii=False, indent=2)}
```

## 已发现的疑点
{json.dumps(review.warnings, ensure_ascii=False, indent=2)}

## 行业业务 Sense
```json
{json.dumps(sense.raw, ensure_ascii=False, indent=2)}
```

## 输出要求
中文 Markdown，严格三段：
1. **这条配置在说什么**：逐条用一句话说清「哪笔费用、按什么动因、分给谁、比例多少」；
2. **可能踩的坑**：只讲有依据的风险（动因未更新、比例不闭合、动因与系统不一致、固定费用按工时分摊等）；
3. **建议动作**：谁去补什么、补完后怎么验证；不超过 4 条。

数字只能来自原文或结构化解析；不要给会计处理建议，不要自动改配置。"""
    return ChatPrompt(system=system, user=user)


def render_offline_translation(review: ConfigReview) -> str:
    lines: list[str] = ["## 这条配置在说什么"]
    if not review.rules:
        lines.append("没有从文档里识别出明确的分摊规则，需要人工确认。")
    for rule in review.rules:
        shares = (
            "、".join(
                f"{center} {share}%"
                for center, share in zip(rule.centers, rule.shares)
            )
            if rule.shares
            else "、".join(rule.centers)
        )
        lines.append(f"- {rule.pool}：按{rule.driver}，分给 {shares}。")
    for note in review.notes:
        lines.append(f"- {note}")
    lines.append("")

    lines.append("## 可能踩的坑")
    if not review.warnings:
        lines.append("- 未发现明显结构性问题；仍需确认动因数据是否为本期。")
    for warning in review.warnings:
        lines.append(f"- {warning}")
    lines.append("")

    lines.append("## 建议动作")
    actions = []
    if any("未更新" in w or "未就绪" in w for w in review.warnings):
        actions.append("让数据提供方补齐本期动因（机器工时/产量），再跑分摊。")
    if any("不等于 100" in w for w in review.warnings):
        actions.append("核对分摊比例，确保合计 100%。")
    if any("机器工时" in w for w in review.warnings):
        actions.append("确认系统动因是否支持机器工时；若仍用人工工时，需在 Sense 中说明差异。")
    if any("没有任何报工" in w for w in review.warnings):
        actions.append("补录对应车间的报工，或确认该笔费用是否应改由其他中心承担。")
    actions.append("分摊后抽查一张工单，核对费用池合计是否等于分出去的金额。")
    for index, action in enumerate(actions[:4], 1):
        lines.append(f"{index}. {action}")
    lines.append("")
    lines.append("> 本解释只基于配置原文与已发现疑点，不替代配置评审。")
    return "\n".join(lines)


def translate_config(
    text: str,
    sense: BusinessSense,
    provider: LLMProvider | None = None,
    ds: Dataset | None = None,
) -> Explanation:
    review = parse_config(text, ds)
    prompt = build_translate_prompt(review, sense)
    if provider is None:
        return Explanation(
            text=render_offline_translation(review),
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


def read_config(path: str | Path) -> str:
    if str(path) == "-":
        import sys

        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


__all__ = [
    "AllocationRule",
    "ConfigReview",
    "build_translate_prompt",
    "parse_config",
    "read_config",
    "render_offline_translation",
    "translate_config",
]
