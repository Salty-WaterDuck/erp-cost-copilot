"""ABC 动因数据的规则引擎：只判断「能不能进 ABC」，不计算 ABC。

规则覆盖三类最常见的问题：

1. 单据级：零/负时长、缺成本对象、单位不对、状态与数量矛盾；
2. 时序级：同资源重叠、重复事件、容量超限、迟到数据；
3. 口径级：动因量 vs 台账合计对不上、传感器僵值。

每条异常都带证据、反证条件、责任人与动作；Gate 决定它进入 ABC 的方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .driver_facts import DriverDataset, DriverFact

SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}
STATUS_LABELS = {
    "accepted": "通过",
    "review": "待复核",
    "estimated": "估算",
    "rejected": "拒绝",
}
ZERO = Decimal("0")


@dataclass
class DriverIssue:
    code: str
    severity: str
    title: str
    detail: str
    evidence: tuple[str, ...] = ()
    falsifier: str = ""
    owner: str = "成本会计"
    action: str = ""
    event_id: str = ""


@dataclass
class DriverGate:
    fact: DriverFact
    status: str
    confidence: str
    issues: list[DriverIssue] = field(default_factory=list)


@dataclass
class DriverAudit:
    dataset: DriverDataset
    gates: list[DriverGate]
    issues: list[DriverIssue]
    actual_totals: dict[str, Decimal]

    @property
    def accepted(self) -> list[DriverGate]:
        return [gate for gate in self.gates if gate.status == "accepted"]

    @property
    def review(self) -> list[DriverGate]:
        return [gate for gate in self.gates if gate.status == "review"]

    @property
    def estimated(self) -> list[DriverGate]:
        return [gate for gate in self.gates if gate.status == "estimated"]

    @property
    def rejected(self) -> list[DriverGate]:
        return [gate for gate in self.gates if gate.status == "rejected"]

    def counts(self) -> dict[str, int]:
        return {
            "accepted": len(self.accepted),
            "review": len(self.review),
            "estimated": len(self.estimated),
            "rejected": len(self.rejected),
        }


def _issue(
    code: str,
    severity: str,
    title: str,
    detail: str,
    evidence: tuple[str, ...],
    falsifier: str,
    owner: str,
    action: str,
    event_id: str = "",
) -> DriverIssue:
    return DriverIssue(
        code=code,
        severity=severity,
        title=title,
        detail=detail,
        evidence=evidence,
        falsifier=falsifier,
        owner=owner,
        action=action,
        event_id=event_id,
    )


def _fact_issues(fact: DriverFact, ds: DriverDataset) -> list[DriverIssue]:
    issues: list[DriverIssue] = []
    duration = fact.duration_hours

    if fact.quantity <= 0 or duration <= 0:
        issues.append(
            _issue(
                "ZERO-OR-NEGATIVE",
                "高",
                f"零/负动因量 · {fact.event_id}",
                f"{fact.activity} 动因量 {fact.quantity}，时长 {duration:.4f}h，不能进入 ABC。",
                (
                    f"事件 {fact.event_id}，资源 {fact.resource}",
                    f"开始 {fact.event_start.isoformat()}，结束 {fact.event_end.isoformat()}",
                ),
                "若是换班切割或跨零点事件，提供排班表与原始采集点；否则不是有效动因。",
                "设备科 / 生产统计",
                "补采或拆分事件；确认不是 MES 占位记录。",
                fact.event_id,
            )
        )

    if fact.resource not in ds.resources:
        issues.append(
            _issue(
                "UNKNOWN-RESOURCE",
                "高",
                f"未知资源 · {fact.event_id}",
                f"资源 {fact.resource} 不在设备台账中，无法确认产能与归属。",
                (f"资源编码 {fact.resource}",),
                "若设备是新购/租用，补设备台账后即可通过。",
                "设备科",
                "补录设备台账与产能日历。",
                fact.event_id,
            )
        )

    if not fact.cost_object or fact.cost_object.upper() == "UNKNOWN":
        issues.append(
            _issue(
                "MISSING-COST-OBJECT",
                "高",
                f"缺成本对象 · {fact.event_id}",
                f"{fact.activity} 的动因记录没有工单/产品，无法归集到成本对象。",
                (f"cost_object={fact.cost_object or '空'}",),
                "若属于公共活动（如车间照明），应进入明确的分摊池，而不是留空。",
                "生产统计",
                "补工单/产品编码，或改挂公共活动池。",
                fact.event_id,
            )
        )

    expected = fact.expected_unit
    if expected and fact.unit != expected:
        issues.append(
            _issue(
                "UNIT-MISMATCH",
                "高",
                f"单位不一致 · {fact.event_id}",
                f"动因类型 {fact.driver_type} 期望单位 {expected}，实际 {fact.unit}。",
                (f"quantity={fact.quantity}{fact.unit}",),
                "若标签元数据变更导致单位不同，提供换算系数与变更记录。",
                "IT / 设备科",
                "修正标签单位或补换算规则，重跑校验。",
                fact.event_id,
            )
        )

    if expected == "h" and duration > 0:
        deviation = abs(fact.quantity - duration)
        tolerance = max(Decimal("0.1"), duration * Decimal("0.1"))
        if deviation > tolerance:
            issues.append(
                _issue(
                    "DURATION-MISMATCH",
                    "中",
                    f"动因量与时长不匹配 · {fact.event_id}",
                    f"采集时长 {duration:.2f}h，动因量 {fact.quantity}h，差 {deviation:.2f}h。",
                    (
                        f"开始 {fact.event_start.isoformat()}",
                        f"结束 {fact.event_end.isoformat()}",
                    ),
                    "若设备有并行工位或事件被拆分，提供工位映射与拆分记录。",
                    "设备科",
                    "核对采集点与工位映射；确认是否重复计时。",
                    fact.event_id,
                )
            )

    if fact.state.upper() == "DOWN" and fact.quantity > 0:
        issues.append(
            _issue(
                "STATE-MISMATCH",
                "高",
                f"停机却记动因 · {fact.event_id}",
                f"设备状态为 DOWN，但记录了 {fact.quantity}{fact.unit} 动因量。",
                (f"state={fact.state}",),
                "若停机期间在做保养/换型，应归入 maintenance/setup 活动，而不是生产动因。",
                "设备科",
                "改挂活动类型或拆分事件。",
                fact.event_id,
            )
        )

    if fact.ingested_at.date() > ds.close_date:
        issues.append(
            _issue(
                "LATE-ARRIVAL",
                "中",
                f"关账后迟到 · {fact.event_id}",
                f"采集时间 {fact.ingested_at.date()} 晚于关账日 {ds.close_date}。",
                (f"period={ds.period}",),
                "若属于上期补录并有审批单，可按跨期调整处理；否则不应回写本期 ABC。",
                "IT / 成本会计",
                "确认补录审批；决定计入本期还是下期。",
                fact.event_id,
            )
        )

    return issues


def _mark_duplicates(facts: list[DriverFact], issues: list[DriverIssue]) -> None:
    seen: dict[tuple, list[DriverFact]] = {}
    for fact in facts:
        key = (fact.resource, fact.event_start, fact.event_end, fact.activity)
        seen.setdefault(key, []).append(fact)
    for group in seen.values():
        if len(group) < 2:
            continue
        ids = "、".join(fact.event_id for fact in group)
        for fact in group:
            issues.append(
                _issue(
                    "DUPLICATE",
                    "高",
                    f"重复事件 · {fact.event_id}",
                    f"同一资源、同一时段出现 {len(group)} 条相同活动记录：{ids}。",
                    (f"资源 {fact.resource}", f"时段 {fact.event_start.isoformat()}"),
                    "若是双通道采集，保留一条主记录并关闭另一条；否则是重复计数。",
                    "IT / 设备科",
                    "去重并保留主数据源。",
                    fact.event_id,
                )
            )


def _mark_overlaps(facts: list[DriverFact], issues: list[DriverIssue]) -> None:
    by_resource: dict[str, list[DriverFact]] = {}
    for fact in facts:
        by_resource.setdefault(fact.resource, []).append(fact)
    for resource_facts in by_resource.values():
        resource_facts.sort(key=lambda item: item.event_start)
        for index in range(1, len(resource_facts)):
            previous = resource_facts[index - 1]
            current = resource_facts[index]
            if current.event_start >= previous.event_end:
                continue
            if (
                current.event_start == previous.event_start
                and current.event_end == previous.event_end
                and current.activity == previous.activity
            ):
                continue
            for fact, other in ((current, previous), (previous, current)):
                issues.append(
                    _issue(
                        "OVERLAP",
                        "高",
                        f"同资源时间重叠 · {fact.event_id}",
                        f"{fact.resource} 与 {other.event_id} 时段重叠，可能重复计时。",
                        (
                            f"{fact.event_id}: {fact.event_start.isoformat()} → "
                            f"{fact.event_end.isoformat()}",
                            f"{other.event_id}: {other.event_start.isoformat()} → "
                            f"{other.event_end.isoformat()}",
                        ),
                        "若设备支持并行工位，提供工位拆分记录；否则重叠部分是重复动因。",
                        "设备科",
                        "拆分事件或删除重复记录。",
                        fact.event_id,
                    )
                )


def _capacity_issues(ds: DriverDataset) -> list[DriverIssue]:
    buckets: dict[tuple[str, date], Decimal] = {}
    for fact in ds.facts:
        if fact.unit != "h" or fact.expected_unit != "h":
            continue
        key = (fact.resource, fact.event_start.date())
        buckets[key] = buckets.get(key, ZERO) + fact.quantity
    issues = []
    for (resource, day), total in sorted(buckets.items()):
        master = ds.resources.get(resource)
        if master is None:
            continue
        limit = master.available_hours_per_day
        if total <= limit * Decimal("1.05"):
            continue
        issues.append(
            _issue(
                "CAPACITY-EXCEEDED",
                "中",
                f"单日动因超产能 · {resource} {day}",
                f"合计 {total}h，设备可用 {limit}h/天，超出 {total - limit}h。",
                (f"资源 {resource}", f"日期 {day}"),
                "若当天加班/临时租用设备，提供排班与租赁记录；否则是重复计时。",
                "设备科 / 生产计划",
                "核对排班与事件拆分。",
            )
        )
    return issues


def _stale_sensor_issues(ds: DriverDataset) -> list[DriverIssue]:
    groups: dict[tuple[str, str], list[DriverFact]] = {}
    for fact in ds.facts:
        if fact.driver_type != "energy_kwh":
            continue
        groups.setdefault((fact.resource, fact.source_tag), []).append(fact)
    issues = []
    for (_resource, _tag), facts in groups.items():
        facts.sort(key=lambda item: item.event_start)
        run = 1
        for index in range(1, len(facts)):
            if facts[index].quantity == facts[index - 1].quantity:
                run += 1
            else:
                run = 1
            if run == 4:
                window = facts[index - 3 : index + 1]
                ids = "、".join(item.event_id for item in window)
                issues.append(
                    _issue(
                        "STALE-SENSOR",
                        "中",
                        f"传感器僵值 · {facts[index].event_id}",
                        f"连续 {run} 条读数相同：{facts[index].quantity}{facts[index].unit}（{ids}）。",
                        (f"采集点 {facts[index].source_tag}",),
                        "若设备稳定运行在固定功率，提供设备功率曲线或校准记录。",
                        "设备科",
                        "检查传感器与校准周期；必要时剔除异常段。",
                        facts[index].event_id,
                    )
                )
                run = 1
    return issues


def _reconciliation(ds: DriverDataset) -> tuple[dict[str, Decimal], list[DriverIssue]]:
    actual: dict[str, Decimal] = {}
    for fact in ds.facts:
        actual[fact.activity] = actual.get(fact.activity, ZERO) + fact.quantity
    issues = []
    for activity, declared in ds.declared_totals.items():
        observed = actual.get(activity, ZERO)
        difference = observed - declared
        tolerance = max(Decimal("0.5"), abs(declared) * Decimal("0.01"))
        if abs(difference) > tolerance:
            issues.append(
                _issue(
                    "DRIVER-TOTAL-MISMATCH",
                    "中",
                    f"动因量对账不符 · {activity}",
                    f"事实合计 {observed}，台账 {declared}，差 {difference}。",
                    (f"活动 {activity}",),
                    "若台账含未入系统的估算/手工调整，提供调整单；否则说明漏采或重复。",
                    "成本会计",
                    "与 MES/设备台账对账，补录或剔除差异。",
                )
            )
    return actual, issues


def audit_drivers(ds: DriverDataset) -> DriverAudit:
    issues: list[DriverIssue] = []
    per_fact: dict[str, list[DriverIssue]] = {}

    for fact in ds.facts:
        found = _fact_issues(fact, ds)
        per_fact[fact.event_id] = found
        issues.extend(found)

    duplicate_issues: list[DriverIssue] = []
    _mark_duplicates(ds.facts, duplicate_issues)
    for item in duplicate_issues:
        per_fact.setdefault(item.event_id, []).append(item)
        issues.append(item)

    overlap_issues: list[DriverIssue] = []
    _mark_overlaps(ds.facts, overlap_issues)
    for item in overlap_issues:
        per_fact.setdefault(item.event_id, []).append(item)
        issues.append(item)

    global_issues = _capacity_issues(ds) + _stale_sensor_issues(ds)
    actual, reconciliation_issues = _reconciliation(ds)
    global_issues.extend(reconciliation_issues)
    issues.extend(global_issues)

    gates = []
    for fact in ds.facts:
        found = per_fact.get(fact.event_id, [])
        if any(item.severity == "高" for item in found):
            status, confidence = "rejected", "C"
        elif fact.estimated:
            status, confidence = "estimated", "B"
        elif any(item.severity == "中" for item in found):
            status, confidence = "review", "B"
        else:
            status, confidence = "accepted", "A"
        gates.append(DriverGate(fact=fact, status=status, confidence=confidence, issues=found))

    issues.sort(key=lambda item: SEVERITY_ORDER.get(item.severity, 9))
    return DriverAudit(dataset=ds, gates=gates, issues=issues, actual_totals=actual)


def render_audit(report: DriverAudit, limit: int = 12) -> str:
    ds = report.dataset
    counts = report.counts()
    lines = [
        f"## ABC 动因数据前置校验 · {ds.period}",
        "=" * 56,
        f"事实 {len(ds.facts)} 条 · 通过 {counts['accepted']} / 待复核 {counts['review']} / "
        f"估算 {counts['estimated']} / 拒绝 {counts['rejected']}",
        f"活动 {len(ds.activities)} 个 · 资源 {len(ds.resources)} 个 · "
        f"成本对象 {len(ds.cost_objects)} 个",
        "",
        "### 动因量对账",
        "| 活动 | 事实合计 | 台账合计 | 差异 | 结果 |",
        "|---|---|---|---|---|",
    ]
    for activity in sorted(set(report.actual_totals) | set(ds.declared_totals)):
        observed = report.actual_totals.get(activity, ZERO)
        declared = ds.declared_totals.get(activity)
        if declared is None:
            lines.append(f"| {activity} | {observed} | — | — | 台账未提供 |")
            continue
        difference = observed - declared
        tolerance = max(Decimal("0.5"), abs(declared) * Decimal("0.01"))
        flag = "✓" if abs(difference) <= tolerance else "✗"
        lines.append(f"| {activity} | {observed} | {declared} | {difference} | {flag} |")

    lines.extend(["", "### Gate 汇总", "| 状态 | 条数 | 进入 ABC 的方式 |", "|---|---|---|"])
    treatment = {
        "accepted": "直接进入",
        "review": "责任人签字后进入",
        "estimated": "按批准模型估算，打标进入",
        "rejected": "不进入，转异常工单",
    }
    for status in ("accepted", "review", "estimated", "rejected"):
        lines.append(f"| {STATUS_LABELS[status]} | {counts[status]} | {treatment[status]} |")

    lines.extend(["", "### 异常明细"])
    if not report.issues:
        lines.append("未发现异常。")
    for index, item in enumerate(report.issues[:limit], 1):
        subject = item.event_id or "全局"
        lines.append(f"{index}. **[{item.severity}] {item.title}**")
        lines.append(f"   - 事实：{item.detail}")
        for evidence in item.evidence[:2]:
            lines.append(f"   - 证据：{evidence}")
        lines.append(f"   - 反证：{item.falsifier}")
        lines.append(f"   - 责任：{item.owner}｜动作：{item.action}")
    if len(report.issues) > limit:
        lines.append(f"（另有 {len(report.issues) - limit} 条未展开）")

    sample = report.accepted[0] if report.accepted else None
    if sample is not None:
        fact = sample.fact
        lines.extend(
            [
                "",
                "### 可追溯示例",
                f"{fact.event_id} · {fact.activity} · {fact.resource} · "
                f"{fact.quantity}{fact.unit} · {fact.cost_object} · "
                f"{fact.source_system}/{fact.source_tag}",
            ]
        )
    lines.append("")
    lines.append("> 本层只判断动因能否进入 ABC，不做 ABC 计算；拒绝/估算必须有责任人签字。")
    return "\n".join(lines)


__all__ = [
    "DriverAudit",
    "DriverGate",
    "DriverIssue",
    "audit_drivers",
    "render_audit",
]
