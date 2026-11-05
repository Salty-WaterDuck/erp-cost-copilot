# ABC 动因数据前置校验层

## 1. 定位

这一层**不计算 ABC**，也不替换客户的 ABC/TDABC 引擎。它只回答：

> 这条动因记录能不能进入 ABC？凭什么？谁签字？

ABC 项目的失败，通常不是算法，而是：

- 动因定义拍脑袋；
- MES/IoT 数据与财务口径对不上；
- 零时长、重复、重叠、单位错误、关账后迟到；
- 异常被静默填平，最后没人敢用结果。

所以价值在**可追溯、可对账、可解释**，不在再写一遍分摊公式。

## 2. 适用判断

| 场景 | 建议 |
|---|---|
| 组装型、直接材料占比高、共用资源少 | 工单成本 + 制造费用分配通常够用，ABC 可能过度 |
| 流程/混合制造、自动化产线、多产品共线、制造费用占比高 | ABC/TDABC 有价值，前置校验层是刚需 |
| 已有 MES/IoT，但财务不信任设备数据 | 这是本层最典型的落地点 |

## 3. DriverFact 契约

每条动因事实至少包含：

```text
event_id · period · activity · driver_type
quantity · unit · cost_object · resource
event_start / event_end / ingested_at
source_system · source_tag · state · estimated
```

示例：

```json
{
  "event_id": "EV-MC-01",
  "activity": "机加",
  "driver_type": "machine_hours",
  "quantity": "3",
  "unit": "h",
  "cost_object": "PO-2601",
  "resource": "MC-01",
  "event_start": "2026-10-01T08:00:00",
  "event_end": "2026-10-01T11:00:00",
  "ingested_at": "2026-10-01T23:00:00",
  "source_system": "MES",
  "source_tag": "MC-01-TAG",
  "state": "RUN",
  "estimated": false
}
```

## 4. 规则与反证

| 规则 | 触发 | 反证条件 | 责任 |
|---|---|---|---|
| `ZERO-OR-NEGATIVE` | 动因量 ≤0 或时长 ≤0 | 换班切割 / 跨零点 / MES 占位 | 设备科 |
| `OVERLAP` | 同资源时段重叠 | 并行工位 / 工位拆分记录 | 设备科 |
| `DUPLICATE` | 同资源同时段同活动重复 | 双通道采集，保留主源 | IT |
| `UNIT-MISMATCH` | 动因单位与类型不符 | 标签元数据变更 + 换算系数 | IT |
| `DURATION-MISMATCH` | 动因量与采集时长差 >10% | 并行工位 / 事件拆分 | 设备科 |
| `MISSING-COST-OBJECT` | 无工单/产品 | 公共活动应进入明确分摊池 | 生产统计 |
| `STATE-MISMATCH` | DOWN 状态却有动因量 | 保养/换型应改挂活动 | 设备科 |
| `CAPACITY-EXCEEDED` | 单日动因 > 可用产能 | 加班 / 租赁设备记录 | 生产计划 |
| `LATE-ARRIVAL` | 采集晚于关账日 | 补录审批 / 跨期调整 | 成本会计 |
| `STALE-SENSOR` | 连续多条相同读数 | 稳定功率运行 / 校准记录 | 设备科 |
| `DRIVER-TOTAL-MISMATCH` | 事实合计 ≠ 台账合计 | 手工调整单 / 漏采说明 | 成本会计 |

每条异常都带：证据、反证条件、责任人、动作。

## 5. Gate

| 状态 | 条件 | 进入 ABC 的方式 |
|---|---|---|
| `accepted` | 无高/中风险 | 直接进入 |
| `review` | 仅中风险 | 责任人签字后进入 |
| `estimated` | 标记 estimated 且无高风险 | 按批准模型估算，打标进入 |
| `rejected` | 任一高风险 | 不进入，转异常工单 |

**不做静默 fillna。** 估算必须有标记和审批，否则 ABC 结果不可审计。

## 6. 运行

```bash
python -m costcontrol.cli --abc-check data/abc_driver_sample.json
```

```text
## ABC 动因数据前置校验 · 2026-10
事实 43 条 · 通过 32 / 待复核 1 / 估算 1 / 拒绝 9

### 动因量对账
| 活动 | 事实合计 | 台账合计 | 差异 | 结果 |
| 喷涂 | 279 | 275 | 4 | ✗ |

### Gate 汇总
| 状态 | 条数 | 进入 ABC 的方式 |
| 通过 | 32 | 直接进入 |
| 拒绝 | 9 | 不进入，转异常工单 |
```

API：

```bash
curl -s "http://127.0.0.1:8000/v1/abc-check?file=sample"
```

## 7. 与现有项目的关系

- 复用 `audit` 的证据/动作/责任人模式；
- 复用 `diagnosis` 的假设检验与 falsifier 思路；
- 复用 `tasks` 的工单化；
- 不修改现有 job costing 引擎。

ABC/TDABC 计算可以留给客户现有系统（SAP CO-ABC、Oracle、自研），
本层只保证输入可信、输出可解释。

## 8. 边界

- 不判断成本池/活动定义是否合理（那是组织共识）；
- 不把 MES/IoT 数据当财务真相，只输出“财务可用性”；
- 规则必须有 owner，否则会退化成“红点噪声”；
- 第一版只做 11 条规则，宁少勿多。
