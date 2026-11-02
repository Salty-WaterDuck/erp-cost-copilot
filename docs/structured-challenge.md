# 结构化反证：Tool Calling + 校验 + 回退

## 1. 解决什么问题

`--challenge` 最早让模型输出 Markdown，再由人阅读。问题是：

- 分数是否合理、证据是否真实，系统无法判断；
- 同一段反证跑两次，模型可能给出不同的置信度；
- 模型可能引用一条根本不存在的“证据”。

0.15.0 改为：模型必须调用 `update_confidence` 工具，返回结构化裁决；
Python 做 Schema 与语义校验，任何一项不通过就回退离线规则。

## 2. 工具 Schema

```json
{
  "name": "update_confidence",
  "arguments": {
    "verdict": "weakened",
    "hit_evidence": ["E2"],
    "adjusted_score": 0.29,
    "rationale": "反证直接削弱了“本期无退料单”这条证据。",
    "actions": [
      {
        "owner": "仓库主管",
        "action": "补录退料单并重新导出",
        "evidence_needed": "退料单号、日期、数量"
      }
    ]
  }
}
```

`verdict` 四选一：

| verdict | 含义 | 分数约束 |
|---|---|---|
| `upheld` | 反证不成立，维持原结论 | 变化 ≤ ±0.05，且不得命中证据 |
| `weakened` | 命中部分证据，置信度下调 | 下调 0.05–0.45，必须命中至少一条 |
| `overturned` | 核心证据被推翻 | 分数 ≤ 0.30，必须命中至少一条 |
| `needs_evidence` | 无法判断，需补证 | 不上调，下调 ≤0.15 |

## 3. 三道校验闸

1. **证据 ID 白名单**：prompt 给每条证据分配 `E1..En`，`hit_evidence` 只能是这些 ID；
2. **分数跳变约束**：0–1 边界；没有新证据时上调 ≤0.10、下调 ≤0.50；
3. **verdict 自洽**：分数、命中证据、动作数量必须与裁决类型一致。

任何一项失败，抛出 `ChallengeValidationError`。

## 4. 回退链

```text
Function Calling（DeepSeek / Ollama）
        │ 失败/不支持
        ▼
离线规则 challenge()
        │
        ▼
输出“已回退离线规则（原因）”，不改账
```

回退原因会打印在报告里，例如：

```text
> 结构化输出未通过校验（adjusted_score 超出 0–1：1.4），已回退离线规则。
```

回退时不会写入审计日志，避免把无效裁决当成事实。

## 5. 审计日志

通过校验的裁决追加到 `data/challenges_<期间>.jsonl`（可用 `--challenge-log` 改路径）：

```json
{
  "created_at": "2026-11-02T10:00:00",
  "fact_id": "VF-2026-10-005",
  "cost_element": "MAT-QTY-VAR",
  "order_no": "PO-1001",
  "material": "RM-SS304",
  "original_label": "净领用超耗（已扣退料）",
  "original_score": 0.54,
  "verdict": "weakened",
  "hit_evidence": ["E2"],
  "adjusted_score": 0.29,
  "rationale": "...",
  "actions": [...],
  "provider": "deepseek",
  "model": "deepseek-chat",
  "fallback_reason": ""
}
```

这份日志可以用于：

- 审计：谁在什么时间把哪条结论改成了什么；
- 评估：fallback rate、平均下调幅度、人工复核率；
- 学习：把高频命中模式沉淀回 `diagnosis.py` 的假设库。

## 6. 为什么不让模型直接改账

- 模型输出是“判断”，不是“事实”；
- 所有金额仍来自确定性引擎；
- 审计日志只记录“模型建议”，不自动生成凭证或调整成本。

## 7. 测试

- 合法 tool call → 解析、校验、写日志；
- 分数越界 / 证据 ID 不存在 / verdict 与分数冲突 → 回退；
- 多个 tool calls / 未知函数 / 非法 JSON → 回退；
- provider 不支持工具 → 回退；
- 离线模式行为不变（0.54 → 0.29）。

测试全部使用 fake transport，不依赖网络。
