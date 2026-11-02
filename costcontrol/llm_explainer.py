"""AI 解释层：把确定性的成本结果翻译成「人话 + 排雷建议」。

边界
----
本模块**只读**成本计算结果，不参与任何金额计算，也不修改凭证。它做三件事：

1. 把 ``CostRun`` 里的差异、预警、在产、未分配费用整理成结构化事实；
2. 叠加 YAML 业务 Sense（行业特征、分摊基准、异常特征），让归因有行业上下文；
3. 调用大模型（DeepSeek / 本地 Ollama），或退化为离线规则，生成结账前排查清单。

没有配置模型时，``provider_from_env()`` 返回 ``None``，由 ``render_offline_checklist``
生成一份确定性的兜底报告——这样项目在断网、无密钥的环境里依然能演示。

提示词安全
----------
事实与 Sense 都以 JSON 注入，并明确要求「数字只能来自事实、不得编造」。模型输出
只作为建议，不自动落账；CLI 与 README 都强调这一点。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from .audit import detect_anomalies, render_anomalies
from .models import Alert, Severity
from .money import ZERO
from .run import CostRun
from .yaml_lite import load_file

DEFAULT_SENSE_PATH = (
    Path(__file__).resolve().parent.parent
    / "config_templates"
    / "discrete_manufacturing.yaml"
)

SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
SEVERITY_LABEL = {Severity.CRITICAL: "严重", Severity.WARNING: "警告", Severity.INFO: "提示"}

VARIANCE_LABELS = {
    "material_price_variance": "材料价差",
    "material_qty_variance": "材料量差",
    "labor_rate_variance": "人工工资率",
    "labor_efficiency_variance": "人工效率",
    "overhead_spending_variance": "费用耗费",
    "overhead_efficiency_variance": "费用效率",
}


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 业务 Sense
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessSense:
    industry: str
    label: str
    description: str
    raw: dict[str, Any]

    @property
    def anomalies(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("anomalies", {}) or {}

    @property
    def rule_map(self) -> dict[str, str]:
        return self.raw.get("rule_map", {}) or {}

    @property
    def prompt_config(self) -> dict[str, Any]:
        return self.raw.get("prompt", {}) or {}

    @property
    def max_items(self) -> int:
        value = self.prompt_config.get("max_items", 6)
        return int(value) if value else 6

    def anomaly_for(self, rule: str) -> dict[str, Any] | None:
        key = self.rule_map.get(rule)
        if not key:
            return None
        return self.anomalies.get(key)


def load_sense(path: str | Path | None = None) -> BusinessSense:
    target = Path(path) if path else DEFAULT_SENSE_PATH
    try:
        raw = load_file(target)
    except OSError as exc:
        raise LLMError(f"读取业务 Sense 失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise LLMError(f"业务 Sense 配置不是映射结构：{target}")
    return BusinessSense(
        industry=str(raw.get("industry", "unknown")),
        label=str(raw.get("label", "未命名行业")),
        description=str(raw.get("description", "")),
        raw=raw,
    )


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatPrompt:
    system: str
    user: str

    def as_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


@dataclass(frozen=True)
class ChatResult:
    text: str
    provider: str
    model: str


@dataclass(frozen=True)
class ToolCall:
    """模型返回的一次工具调用：函数名 + 已解析的参数。"""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Explanation:
    text: str
    provider: str
    model: str
    prompt: ChatPrompt
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def _money(value: Decimal | int | str) -> str:
    return f"{Decimal(str(value)):,.2f}"


def _qty(value: Decimal | int | str) -> str:
    text = f"{Decimal(str(value)):,.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def build_facts(run: CostRun) -> dict[str, Any]:
    """把成本结果整理成模型可用的事实（不含任何结论性判断）。"""
    sheet = run.sheet
    rate = (
        None
        if sheet.std_total == 0
        else f"{sheet.variance_total / sheet.std_total * 100:.2f}%"
    )
    alerts = [
        {
            "rule": alert.rule,
            "severity": SEVERITY_LABEL[alert.severity],
            "message": alert.message,
            "order_no": alert.order_no,
            "material": alert.material,
            "cost_center": alert.cost_center,
            "amount": str(alert.amount),
        }
        for alert in sheet.alerts
    ]
    orders = []
    for cost in sheet.orders:
        orders.append(
            {
                "order_no": cost.order_no,
                "product": cost.product,
                "qty_completed": str(cost.qty_completed),
                "unit_cost": str(cost.unit_cost),
                "total_cost": str(cost.total_cost),
                "std_total": str(cost.std_total),
                "variance": str(cost.variance_total),
                "variance_rate": (
                    None
                    if cost.std_total == 0
                    else f"{cost.variance_total / cost.std_total * 100:.2f}%"
                ),
                "variances": {
                    VARIANCE_LABELS[name]: str(getattr(cost, name))
                    for name in VARIANCE_LABELS
                },
                "materials": [
                    {
                        "material": line.material,
                        "qty": str(line.qty),
                        "unit_price": str(line.unit_price),
                        "std_price": str(line.std_price),
                        "std_qty": str(line.std_qty),
                        "price_variance": str(line.price_variance),
                        "qty_variance": str(line.qty_variance),
                    }
                    for line in cost.material_lines
                ],
                "wip_cost": str(cost.wip_cost),
                "opening_cost": str(cost.opening_cost),
            }
        )
    return {
        "company": sheet.company,
        "period": sheet.period,
        "totals": {
            "total_cost": str(sheet.total_cost),
            "std_total": str(sheet.std_total),
            "variance": str(sheet.variance_total),
            "variance_rate": rate,
            "opening_cost": str(sheet.opening_cost),
            "current_cost": str(sheet.current_cost),
            "unallocated_overhead": str(run.unallocated_overhead),
        },
        "data_anomalies": [
            {
                "code": item.code,
                "severity": item.severity,
                "title": item.title,
                "detail": item.detail,
                "evidence": list(item.evidence),
                "action": item.action,
                "owner": item.owner,
            }
            for item in detect_anomalies(run.dataset, run)
        ],
        "alerts": alerts,
        "orders": orders,
    }


def build_prompt(
    run: CostRun,
    sense: BusinessSense,
    audience: str | None = None,
) -> ChatPrompt:
    facts = build_facts(run)
    config = sense.prompt_config
    audience = audience or str(config.get("audience", "财务经理与成本会计"))
    max_items = sense.max_items

    system = (
        "你是一名资深成本会计与 ERP 成本顾问，擅长在月结前做成本排雷。"
        "你只依据用户提供的事实与行业业务 Sense 作判断；所有数字必须与事实完全一致，"
        "不得编造工单、单据、供应商或金额。你输出的是分析建议，不是会计分录，"
        "任何调整都必须提示人工复核。"
    )
    user = f"""## 任务
为 {facts['company']} 的 {facts['period']} 月结生成一份「结账前排查清单」，读者是{audience}。

## 事实（JSON，唯一可信数字来源）
```json
{json.dumps(facts, ensure_ascii=False, indent=2)}
```

## 行业业务 Sense（JSON）
```json
{json.dumps(sense.raw, ensure_ascii=False, indent=2)}
```

## 输出要求
- 中文 Markdown，先给一句总体结论，再给不超过 {max_items} 条排查项；
- **先处理 facts.data_anomalies 里的单据异常**：完工未报工、负数领用无退料、动因为 0 等，
  它们会让成本失真，必须排在差异项之前；
- 每条包含：**问题标题（含金额/工单号）**、依据（只能引用事实）、建议动作、责任岗位；
- 先列会阻断结账的严重项（未分配费用、严重超支），同一严重级别内按金额从大到小排序；
- 结合 Sense 里的分摊基准与异常特征解释「为什么值得查」，不要泛泛而谈；
- 严禁编造事实中不存在的数字或单据；信息不足时明确写「需补充数据」；
- 结尾给出一条「下一步」建议，并注明本报告不替代人工审批。

## 输出格式
```
## 总体结论
...

## 排查清单
1. **...**
   - 依据：...
   - 动作：...
   - 责任：...

## 下一步
...
```"""
    return ChatPrompt(system=system, user=user)


# --------------------------------------------------------------------------
# 离线兜底
# --------------------------------------------------------------------------


def _top_variances(cost: Any, limit: int = 2) -> str:
    """挑出金额最大的几项差异，给离线报告补充归因线索。"""
    items = []
    for attr, label in VARIANCE_LABELS.items():
        value = getattr(cost, attr)
        if value:
            items.append((abs(value), label, value))
    items.sort(reverse=True)
    return "、".join(f"{label} {_money(value)}" for _abs, label, value in items[:limit])


def render_offline_checklist(
    run: CostRun,
    sense: BusinessSense,
    max_items: int | None = None,
) -> str:
    """无模型时用规则生成排查清单，保证项目离线可用、结果可复现。"""
    sheet = run.sheet
    limit = max_items or sense.max_items
    anomalies = detect_anomalies(run.dataset, run)
    rate = (
        "—"
        if sheet.std_total == 0
        else f"{sheet.variance_total / sheet.std_total * 100:.2f}%"
    )

    severity_order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
    alerts = sorted(
        sheet.alerts,
        key=lambda a: (severity_order[a.severity], -abs(a.amount)),
    )
    lines: list[str] = []
    lines.append("## 总体结论")
    lines.append(
        f"{sheet.period} 实际成本 {_money(sheet.total_cost)}，标准 {_money(sheet.std_total)}，"
        f"总差异 {_money(sheet.variance_total)}（{rate}）；"
        f"未分配制造费用 {_money(run.unallocated_overhead)}，预警 {len(sheet.alerts)} 条，"
        f"数据异常 {len(anomalies)} 条。"
    )
    if run.unallocated_overhead > 0:
        lines.append(
            "存在未分配制造费用，属于会阻断结账的项，建议优先处理。"
        )
    if anomalies:
        lines.append(
            "先修数据异常，再看差异；否则「节约/超支」可能只是单据缺失造成的假象。"
        )
    lines.append("")
    lines.append(render_anomalies(anomalies))
    lines.append("## 差异排查")
    if not alerts:
        lines.append("本期没有触发预警。建议抽查金额最大的两张工单，确认标准成本与领料口径。")
    for index, alert in enumerate(alerts[:limit], 1):
        anomaly = sense.anomaly_for(alert.rule) or {}
        label = anomaly.get("label", alert.rule)
        owner = anomaly.get("owner", "成本会计")
        suggestion = anomaly.get("suggestion", "核对原始单据与标准成本口径")
        signal = anomaly.get("signal", "")
        subject = alert.order_no or alert.cost_center
        title = f"{label} · {subject}｜{_money(alert.amount)}" if subject else label
        lines.append(f"{index}. **{title}**")
        lines.append(f"   - 事实：{alert.message}")
        if alert.order_no and alert.rule == "ORDER-OVERSPEND":
            cost = run.sheet.order(alert.order_no)
            lines.append(f"   - 归因：本期差异主要来自 {_top_variances(cost)}")
        if alert.order_no and alert.rule == "MAT-OVERUSE":
            cost = run.sheet.order(alert.order_no)
            line = next(
                (l for l in cost.material_lines if l.material == alert.material), None
            )
            if line is not None:
                lines.append(
                    f"   - 明细：领用 {_qty(line.qty)} vs 标准 {_qty(line.std_qty)}，"
                    f"单价 {_money(line.unit_price)} vs 标准 {_money(line.std_price)}"
                )
        if signal:
            lines.append(f"   - 判断：{signal}")
        lines.append(f"   - 动作：{suggestion}")
        lines.append(f"   - 责任：{owner}")
    if len(alerts) > limit:
        lines.append(f"（另有 {len(alerts) - limit} 条预警未展开，可在看板查看）")
    lines.append("")

    notes = (sense.raw.get("allocation", {}) or {}).get("notes") or []
    if notes:
        lines.append("## 行业视角")
        for note in notes[:2]:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("## 下一步")
    lines.append(
        "先处理会阻断结账的项（未分配费用、严重超支），再按金额从大到小核查工单；"
        "本报告由离线规则生成，配置大模型后可得更完整的归因与沟通话术。"
    )
    lines.append("")
    lines.append("> 本报告为分析建议，不替代审批，也不会自动修改账务。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 模型提供方
# --------------------------------------------------------------------------


def urllib_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LLMError(f"调用模型失败：{exc}") from exc


Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]


def _parse_openai_tool_call(data: dict[str, Any], label: str) -> ToolCall:
    try:
        message = data["choices"][0]["message"]
        calls = message.get("tool_calls") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"{label} 返回格式异常：{data}") from exc
    if len(calls) != 1:
        raise LLMError(f"{label} 应返回 1 个工具调用，实际 {len(calls)} 个")
    function = calls[0].get("function") or {}
    name = str(function.get("name") or "")
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError as exc:
            raise LLMError(f"{label} 工具参数不是合法 JSON：{exc}") from exc
    if not isinstance(arguments, dict):
        raise LLMError(f"{label} 工具参数必须是对象")
    return ToolCall(name=name, arguments=arguments)


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, prompt: ChatPrompt) -> ChatResult: ...

    def complete_tool(
        self, prompt: ChatPrompt, tool_schema: dict[str, Any]
    ) -> ToolCall: ...


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/chat/completions",
        timeout: float = 60.0,
        transport: Transport = urllib_post_json,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 DEEPSEEK_API_KEY")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self._transport = transport

    def complete(self, prompt: ChatPrompt) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": prompt.as_messages(),
            "temperature": 0.2,
            "stream": False,
        }
        data = self._transport(
            self.base_url,
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"DeepSeek 返回格式异常：{data}") from exc
        return ChatResult(text=str(text).strip(), provider=self.name, model=self.model)

    def complete_tool(
        self,
        prompt: ChatPrompt,
        tool_schema: dict[str, Any],
    ) -> ToolCall:
        """要求模型调用指定工具，返回函数名与参数。"""
        payload = {
            "model": self.model,
            "messages": prompt.as_messages(),
            "temperature": 0.2,
            "stream": False,
            "tools": [tool_schema],
            "tool_choice": {
                "type": "function",
                "function": {"name": tool_schema["function"]["name"]},
            },
        }
        data = self._transport(
            self.base_url,
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        return _parse_openai_tool_call(data, "DeepSeek")


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://127.0.0.1:11434/api/chat",
        timeout: float = 120.0,
        transport: Transport = urllib_post_json,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self._transport = transport

    def complete(self, prompt: ChatPrompt) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": prompt.as_messages(),
            "stream": False,
            "options": {"temperature": 0.2},
        }
        data = self._transport(self.base_url, payload, {}, self.timeout)
        try:
            text = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Ollama 返回格式异常：{data}") from exc
        return ChatResult(text=str(text).strip(), provider=self.name, model=self.model)

    def complete_tool(
        self,
        prompt: ChatPrompt,
        tool_schema: dict[str, Any],
    ) -> ToolCall:
        payload = {
            "model": self.model,
            "messages": prompt.as_messages(),
            "stream": False,
            "tools": [tool_schema],
            "options": {"temperature": 0.2},
        }
        data = self._transport(self.base_url, payload, {}, self.timeout)
        message = data.get("message") or {}
        calls = message.get("tool_calls") or []
        if len(calls) != 1:
            raise LLMError(f"Ollama 应返回 1 个工具调用，实际 {len(calls)} 个")
        function = calls[0].get("function") or {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError as exc:
                raise LLMError(f"Ollama 工具参数不是合法 JSON：{exc}") from exc
        if not isinstance(arguments, dict):
            raise LLMError("Ollama 工具参数必须是对象")
        return ToolCall(name=name, arguments=arguments)


def provider_from_env(env: dict[str, str] | None = None) -> LLMProvider | None:
    """按环境变量选择模型；显式 offline 或未配置时返回 None。"""
    values = env if env is not None else dict(os.environ)
    choice = values.get("CC_LLM_PROVIDER", "").strip().lower()
    if choice in ("", "offline", "none"):
        return None
    if choice == "deepseek":
        return DeepSeekProvider(
            api_key=values.get("DEEPSEEK_API_KEY", ""),
            model=values.get("DEEPSEEK_MODEL", "deepseek-chat"),
        )
    if choice == "ollama":
        host = values.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        return OllamaProvider(
            model=values.get("OLLAMA_MODEL", "qwen2.5:7b"),
            base_url=f"{host}/api/chat",
        )
    raise LLMError(f"未知的 CC_LLM_PROVIDER：{choice}（可选 deepseek / ollama / offline）")


# --------------------------------------------------------------------------
# 解释器
# --------------------------------------------------------------------------


class LLMExplainer:
    def __init__(
        self,
        sense: BusinessSense | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self.sense = sense or load_sense()
        self.provider = provider

    def explain(
        self,
        run: CostRun,
        audience: str | None = None,
        max_items: int | None = None,
    ) -> Explanation:
        prompt = build_prompt(run, self.sense, audience)
        if self.provider is None:
            text = render_offline_checklist(run, self.sense, max_items)
            return Explanation(
                text=text,
                provider="offline",
                model="rule-based",
                prompt=prompt,
            )
        result = self.provider.complete(prompt)
        return Explanation(
            text=result.text,
            provider=result.provider,
            model=result.model,
            prompt=prompt,
        )


__all__ = [
    "BusinessSense",
    "ChatPrompt",
    "ChatResult",
    "DeepSeekProvider",
    "Explanation",
    "LLMError",
    "LLMExplainer",
    "LLMProvider",
    "OllamaProvider",
    "ToolCall",
    "build_facts",
    "build_prompt",
    "load_sense",
    "provider_from_env",
    "render_offline_checklist",
]
