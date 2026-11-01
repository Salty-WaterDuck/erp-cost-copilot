"""结账前数据排雷：先找会让成本失真的单据问题，再谈差异归因。

确定性的成本引擎只能保证「按现有单据算对」，不能保证「单据本身齐全」。
这一层专门盯住三类最常见的脏数据：

1. 工单完工了，却没有报工（人工 0，费用也没被吸收）；
2. 出现负数领用，却没有退料标记；
3. 成本中心有费用、有工艺路线，但动因（工时）为 0，费用整笔挂账。

它不修数据、不调账，只把问题、证据、动作和责任人列清楚，交给结账前处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Dataset
from .money import q_money
from .run import CostRun

SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}


@dataclass(frozen=True)
class DataAnomaly:
    code: str
    severity: str
    title: str
    detail: str
    evidence: tuple[str, ...] = ()
    action: str = ""
    owner: str = "成本会计"
    amount: Decimal = field(default=Decimal("0"))


def _hours_by_order(ds: Dataset) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for report in ds.labor:
        result[report.order_no] = result.get(report.order_no, Decimal(0)) + report.hours
    return result


def detect_anomalies(ds: Dataset, run: CostRun) -> list[DataAnomaly]:
    anomalies: list[DataAnomaly] = []
    hours = _hours_by_order(ds)

    for order in ds.orders:
        if order.qty_completed <= 0:
            continue
        if hours.get(order.order_no, Decimal(0)) > 0:
            continue
        cost = run.sheet.order(order.order_no)
        anomalies.append(
            DataAnomaly(
                code="ORDER-NO-LABOR",
                severity="高",
                title=f"完工未报工 · {order.order_no}",
                detail=(
                    f"完工 {order.qty_completed}、废品 {order.qty_scrapped}，"
                    f"但报工 0 小时：人工 {q_money(cost.labor_cost)}、"
                    f"制造费用 {q_money(cost.overhead_cost)}，成本被低估。"
                ),
                evidence=(
                    f"工单 {order.order_no} 状态 {order.status.value}",
                    "本期无该工单的报工记录",
                    "制造费用分配动因为 0，未吸收任何费用",
                ),
                action="补录报工单；确认是否委外、计时漏登或跨期报工",
                owner="生产统计 / 车间主任",
                amount=cost.material_cost + cost.labor_cost,
            )
        )

    for issue in ds.issues:
        if issue.qty >= 0 or issue.is_return:
            continue
        anomalies.append(
            DataAnomaly(
                code="NEGATIVE-ISSUE-NO-RETURN",
                severity="中",
                title=f"负数领用无退料标记 · {issue.doc_no}",
                detail=(
                    f"工单 {issue.order_no} 领用 {issue.material} "
                    f"{issue.qty}，但未标记退料，材料成本被直接冲减。"
                ),
                evidence=(
                    f"单据 {issue.doc_no}：数量 {issue.qty}，单价 {issue.unit_price}",
                    "is_return=false，仓库没有对应退料单",
                ),
                action="补退料单或冲销负数领用；核对是否串单、错单",
                owner="仓库主管",
                amount=abs(issue.qty * issue.unit_price),
            )
        )

    for center, pool in run.overhead.pools.items():
        if pool <= 0:
            continue
        if run.overhead.driver_totals.get(center, Decimal(0)) > 0:
            continue
        cc = ds.center(center)
        anomalies.append(
            DataAnomaly(
                code="DRIVER-ZERO",
                severity="高",
                title=f"分摊动因为 0 · {center}",
                detail=(
                    f"{cc.name} 本期费用 {q_money(pool)}，但工时动因为 0，"
                    f"整笔挂账未分配。"
                ),
                evidence=(
                    f"费用池 {q_money(pool)}，预算 {q_money(cc.overhead_budget)}",
                    "无任何工单在该中心报工",
                    "工艺路线存在该中心工序" if _in_routing(ds, center) else "工艺路线未维护该中心",
                ),
                action="补录该中心工时/机器工时；确认费用是否应转由其他中心承担",
                owner="车间主任 / 设备科",
                amount=pool,
            )
        )

    anomalies.sort(key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), -item.amount))
    return anomalies


def _in_routing(ds: Dataset, center: str) -> bool:
    return any(op.cost_center == center for ops in ds.routing.values() for op in ops)


def render_anomalies(anomalies: list[DataAnomaly], limit: int = 6) -> str:
    if not anomalies:
        return "## 数据排雷\n未发现单据级异常。\n"
    lines = ["## 数据排雷（先修数据，再谈差异）"]
    for index, item in enumerate(anomalies[:limit], 1):
        lines.append(f"{index}. **[{item.severity}] {item.title}**")
        lines.append(f"   - 事实：{item.detail}")
        for evidence in item.evidence:
            lines.append(f"   - 证据：{evidence}")
        lines.append(f"   - 动作：{item.action}")
        lines.append(f"   - 责任：{item.owner}")
    if len(anomalies) > limit:
        lines.append(f"（另有 {len(anomalies) - limit} 条，未展开）")
    lines.append("")
    return "\n".join(lines)


__all__ = ["DataAnomaly", "detect_anomalies", "render_anomalies"]
