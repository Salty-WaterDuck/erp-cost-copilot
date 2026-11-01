# 持久化与多期间

内存里的成本计算是「一次性」的：脚本跑完，数据就没了，也没法回答「上期结完账之后发生了什么」。这一层把数据落到 SQLite，让同一套主数据、多个期间、期间状态都能留住。

## 1. 为什么是 SQLite，而不是直接上服务器

- 单文件、零部署，`git clone` 就能跑；
- 标准库自带 `sqlite3`，不破坏「纯标准库」这条约束；
- 事务、索引、约束够用，真到多人并发再换 PostgreSQL 也只是换一层仓储。

## 2. 表结构

| 表 | 作用 | 主键 |
|---|---|---|
| `meta` | 结构版本 | `key` |
| `periods` | 期间与状态 | `period` |
| `cost_centers` | 成本中心、费率、费用预算 | `code` |
| `materials` | 物料主数据 | `code` |
| `bom` | BOM | `(parent, component)` |
| `routing` | 工艺路线 | `(product, seq)` |
| `orders` | 工单 | `(period, order_no)` |
| `issues` | 领料单 | `(period, doc_no)` |
| `labor` | 报工单 | `(period, doc_no)` |
| `overhead` | 制造费用发生额 | `(period, doc_no)` |
| `opening_balances` | 期初余额 | `(period, account)` |
| `wip_opening` | 期初在产（按成本项目） | `(period, order_no)` |
| `period_params` | 控制阈值 | `(period, key)` |

主数据全局共享，业务单据按期间分区。加载某一期时只取 `period = ?` 的单据，主数据全量读取。

## 3. 金额为什么存 TEXT

SQLite 没有十进制类型。`NUMERIC` 在遇到小数时会退化成 `REAL`（IEEE 754 双精度），`0.1` 存进去就不再是 `0.1`。

成本数据一旦在数据库里失真，前面所有 Decimal 的努力都白费。所以：

```text
金额 / 数量 / 费率 → TEXT，读出来再 Decimal(str)
日期               → TEXT（ISO 8601）
布尔               → INTEGER 0/1
```

代价是不能在 SQL 里直接 `SUM` 金额。这个项目里所有计算都在 Python 里做，数据库只负责存取，所以没有损失。

## 4. 期间状态机

```text
        导入
         │
         ▼
      ┌──────┐   结账检查通过   ┌────────┐
      │ OPEN │ ───────────────▶│ CLOSED │
      └──────┘                 └────────┘
         ▲                          │
         └────────── 解锁 ──────────┘
```

| 操作 | OPEN | CLOSED |
|---|---|---|
| 重新导入（覆盖） | 允许 | **拒绝** |
| 成本计算 / 看报表 | 允许 | 允许（查询视图） |
| 结账 | 允许 | 拒绝（不能重复结账） |
| 删除期间 | 允许 | 拒绝 |
| 解锁 | — | 允许（显式操作） |

CLOSED 期间拒绝覆盖，是为了防止「结完账又改单据」。真要改，先 `--unlock`，改完重新结账——这个动作会留下明确的痕迹，而不是静默改数。

## 5. 用法

```bash
# 初始化
python -m costcontrol.cli --db data/cost.db --init

# 导入一期（JSON 里带 period）
python -m costcontrol.cli --db data/cost.db --import-json data/demo_ningbo.json

# 看有哪些期间、是否结账
python -m costcontrol.cli --db data/cost.db --periods

# 对某一期出报表
python -m costcontrol.cli --db data/cost.db --period 2026-10 --detail
python -m costcontrol.cli --db data/cost.db --period 2026-10 --journal

# 结账（检查通过后期间自动置为 CLOSED）
python -m costcontrol.cli --db data/cost.db --period 2026-10 --close

# 需要返工时先解锁
python -m costcontrol.cli --db data/cost.db --period 2026-10 --unlock
```

`--periods` 的输出：

```text
期间     公司                  状态    工单数  结账时间
-------------------------------------------------------
2026-10  宁波恒宇电器有限公司  已结账       4  2026-10-31 09:16:02
```

## 6. 一致性保证

- **导入是一个事务**：主数据、单据、期初余额、参数要么全写进去，要么全不写。
- **重复导入幂等**：同一期间再导一次，先删该期单据再插入，不会出现双份数据。
- **读取后校验**：`load_period` 出来会跑一遍 `validate`，物料缺失、工单超产、期初不平这类问题在算之前就报错。
- **关账与期初结转同一事务**：`store.close_with_carry()` 在一个事务里写期间状态、下期 `wip_opening`、下期 `opening_balances`、下期工单脚手架；中途失败整体回滚，不会出现「关了账、没期初」。
- **金额精度**：见第 3 节。

## 7. 已知限制

1. **主数据没有版本**。改 BOM 会影响所有期间的重算结果。真实 ERP 用「BOM 版本 + 生效日期」，这是下一步。
2. **凭证没有落库**。每次看凭证都是按成本结果重算的；好处是永不漂移，坏处是不能做「已过账凭证不可修改」。
3. **没有并发控制**。单进程使用；多人场景需要加锁或换数据库。
4. **没有迁移框架**。`meta.schema_version` 已留位，目前 v2；升级只做加列，不重写历史数据。

## 8. 下一步

- 主数据版本与生效日期；
- 凭证落库与过账状态；
- 报表层从数据库直读，支持跨期对比。
