# Changelog

## 0.15.0 — 2026-11-02

### 新增

- **结构化反证**：`--challenge` 在接入模型时改为 Function Calling，调用
  `update_confidence(verdict, hit_evidence, adjusted_score, rationale, actions)`。
- **证据 ID 白名单**：prompt 给证据分配 `E1..En`，模型只能引用、不能自造；
- **分数跳变约束**：0–1 边界、无新证据上调 ≤0.10、下调 ≤0.50；
- **verdict 自洽校验**：`upheld` / `weakened` / `overturned` / `needs_evidence`
  与分数、命中证据互相校验；
- **失败回退**：Schema/语义校验失败、工具调用异常、provider 不支持工具时，
  自动回退离线 `challenge()`，并打印回退原因；
- **审计日志**：通过校验的裁决追加到 `data/challenges_<期间>.jsonl`，
  也可用 `--challenge-log` 指定路径。

### 兼容

- 离线 `--challenge` 行为不变（0.54 → 0.29），仍可完全断网演示。
- DeepSeek / Ollama 增加 `complete_tool`，文本模式保持原样。

## 0.14.0 — 2026-10-31

首个开源版本：**ERP-Cost-Copilot**。

### 新增

- **排雷工单化**：数据异常生成待办（责任人 / 截止日 / 补什么单），
  支持 `--tasks`、`--task-done`、`--rerun-tasks`；补证后自动对比异常数、
  未分配费用、总成本、诊断置信度与逐工单单位成本。
- **数据排雷**：完工未报工、负数领用无退料、分摊动因为 0、未分配费用；
  `--explain` 先报数据异常，再报差异。
- **ERP 配置翻译**：`--translate-config` / `--config-text`，把分摊说明翻译成
  大白话，并识别动因未更新、比例不闭合、系统动因不一致等风险。
- **反证复核**：`--diagnose --challenge "..."`，按命中关系下调置信度，
  给出补证动作与「结论是否仍成立」。
- **强归因诊断**：候选原因、证据链、反证条件与置信度（`--diagnose`）。
- **差异归因/规范事实**：`--attribute`、`--facts`，成本要素带维度与证据。
- **导出覆盖率检查**：`--check-export`，按表头识别 14 类单据，给出能力矩阵。
- **报表级成本体检**：`--reports`，趋势、异常、跨表勾稽。
- **第二层复算**：`--auto-parts --month`，13 表归集/分配/在产拆分并与 ERP 对账。
- **SAP 标准卡基准**：`--sap-baseline`，四要素差异与系统性偏差识别。

### 引擎能力

- BOM 多层卷算、工单归集、约当产量、制造费用分配、半成品逐级结转；
- 六项差异、成本还原、差异穿透、责任归因；
- 凭证、试算平衡、期末结账检查、SQLite 多期间、跨期在产结转；
- 纯标准库，零第三方依赖；`unittest` 239 个测试。

### 边界

- AI 只解释，不改账；所有金额来自确定性引擎。
- 离线规则兜底，模型可选 DeepSeek / Ollama。

### 文档

- 新增业务逻辑思维导图（`docs/assets/erp-cost-copilot-mindmap.jpg`），
  标注纯 Python 引擎与 AI 层的边界。
