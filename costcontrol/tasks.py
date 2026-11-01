"""排雷工单化：把数据异常变成可分派、可追踪、可复跑的待办。

一条异常不只是报告里的一行字，它应该变成：

    谁（责任人） · 何时前（截止日） · 补什么（单据/动作） · 做完没有（状态）

补完之后用新导出重跑，系统对比「补证前 / 补证后」的异常数量、未分配费用、
成本金额与诊断置信度，证明这条雷真的排掉了，而不是换了个说法。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .audit import DataAnomaly
from .models import Dataset
from .money import q_money
from .report import render_table
from .run import CostRun

SEVERITY_DUE_DAYS = {"高": 1, "中": 3, "低": 5}
SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", text).strip("-")[:24]


def _task_id(anomaly: DataAnomaly) -> str:
    digest = hashlib.md5(anomaly.title.encode("utf-8")).hexdigest()[:4]
    return f"T-{anomaly.code}-{digest}"


@dataclass
class Task:
    task_id: str
    code: str
    severity: str
    title: str
    owner: str
    due_date: str
    action: str
    evidence: list[str] = field(default_factory=list)
    status: str = "open"
    done_at: str = ""
    note: str = ""


@dataclass
class TaskBoard:
    company: str
    period: str
    source: str
    as_of: str
    created_at: str
    tasks: list[Task] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)

    @property
    def open_tasks(self) -> list[Task]:
        return [task for task in self.tasks if task.status != "done"]

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "TaskBoard":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw["tasks"] = [Task(**task) for task in raw.get("tasks", [])]
        return cls(**raw)

    def mark_done(self, task_id: str, note: str = "") -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                task.status = "done"
                task.done_at = datetime.now().isoformat(timespec="minutes")
                task.note = note
                return task
        raise KeyError(f"工单不存在：{task_id}")


def diagnosis_stats(diagnoses: list) -> dict[str, int]:
    return {
        "total": len(diagnoses),
        "high": sum(1 for item in diagnoses if item.confidence == "高"),
        "medium": sum(1 for item in diagnoses if item.confidence == "中"),
        "low": sum(1 for item in diagnoses if item.confidence == "低"),
    }


def build_board(
    anomalies: list[DataAnomaly],
    run: CostRun,
    diagnoses: list,
    as_of: date | None = None,
) -> TaskBoard:
    as_of = as_of or date.today()
    tasks = []
    for anomaly in anomalies:
        due = as_of + timedelta(days=SEVERITY_DUE_DAYS.get(anomaly.severity, 3))
        tasks.append(
            Task(
                task_id=_task_id(anomaly),
                code=anomaly.code,
                severity=anomaly.severity,
                title=anomaly.title,
                owner=anomaly.owner,
                due_date=due.isoformat(),
                action=anomaly.action,
                evidence=list(anomaly.evidence),
            )
        )
    tasks.sort(key=lambda task: (SEVERITY_ORDER.get(task.severity, 9), task.due_date))
    ds = run.dataset
    return TaskBoard(
        company=ds.company,
        period=ds.period,
        source="",
        as_of=as_of.isoformat(),
        created_at=datetime.now().isoformat(timespec="minutes"),
        tasks=tasks,
        snapshot={
            "anomalies": [
                {"code": item.code, "title": item.title} for item in anomalies
            ],
            "anomaly_codes": [item.code for item in anomalies],
            "anomaly_titles": {item.code: item.title for item in anomalies},
            "unallocated": str(run.unallocated_overhead),
            "total_cost": str(run.sheet.total_cost),
            "unit_costs": {cost.order_no: str(cost.unit_cost) for cost in run.sheet.orders},
            "diagnosis": diagnosis_stats(diagnoses),
        },
    )


def render_board(board: TaskBoard) -> str:
    open_count = len(board.open_tasks)
    lines = [
        f"## 排雷工单 · {board.period}",
        f"共 {len(board.tasks)} 条，未完成 {open_count} 条；"
        f"截止日基准 {board.as_of}（高 1 天 / 中 3 天 / 低 5 天）。",
        "",
    ]
    rows = [
        [
            task.task_id,
            task.severity,
            task.title,
            task.owner,
            task.due_date,
            task.action,
            "已完成" if task.status == "done" else "待处理",
        ]
        for task in board.tasks
    ]
    lines.append(
        render_table(
            ["编号", "级别", "事项", "责任人", "截止日", "补什么", "状态"],
            rows,
            ["l", "l", "l", "l", "l", "l", "l"],
        )
    )
    return "\n".join(lines)


@dataclass
class Comparison:
    resolved: list[str]
    remaining: list[str]
    rows: list[tuple[str, str, str, str]]


def compare(
    board: TaskBoard,
    run: CostRun,
    diagnoses: list,
    anomalies: list[DataAnomaly],
) -> Comparison:
    before_titles = board.snapshot.get("anomaly_titles", {})
    before_items = board.snapshot.get("anomalies") or [
        {"code": code, "title": before_titles.get(code, code)}
        for code in board.snapshot.get("anomaly_codes", [])
    ]
    before_pairs = [(item["code"], item["title"]) for item in before_items]
    after_pairs = [(item.code, item.title) for item in anomalies]

    def _money(value: object) -> str:
        return f"{q_money(Decimal(str(value))):,.2f}"

    before_unallocated = board.snapshot.get("unallocated", "0")
    before_total = board.snapshot.get("total_cost", "0")
    after_unallocated = str(run.unallocated_overhead)
    after_total = str(run.sheet.total_cost)
    before_diag = board.snapshot.get("diagnosis", {})
    after_diag = diagnosis_stats(diagnoses)
    before_units = board.snapshot.get("unit_costs", {})
    after_units = {cost.order_no: str(cost.unit_cost) for cost in run.sheet.orders}

    rows = [
        (
            "数据异常条数",
            str(len(before_pairs)),
            str(len(after_pairs)),
            f"{len(after_pairs) - len(before_pairs):+d}",
        ),
        (
            "未分配制造费用",
            _money(before_unallocated),
            _money(after_unallocated),
            _money(Decimal(after_unallocated) - Decimal(before_unallocated)),
        ),
        (
            "实际总成本",
            _money(before_total),
            _money(after_total),
            _money(Decimal(after_total) - Decimal(before_total)),
        ),
        (
            "高置信诊断",
            str(before_diag.get("high", 0)),
            str(after_diag["high"]),
            f"{after_diag['high'] - before_diag.get('high', 0):+d}",
        ),
        (
            "中/低置信诊断",
            f"{before_diag.get('medium', 0)} / {before_diag.get('low', 0)}",
            f"{after_diag['medium']} / {after_diag['low']}",
            f"{after_diag['medium'] - before_diag.get('medium', 0):+d} / "
            f"{after_diag['low'] - before_diag.get('low', 0):+d}",
        ),
    ]
    for order_no in sorted(set(before_units) | set(after_units)):
        before = Decimal(before_units.get(order_no, "0"))
        after = Decimal(after_units.get(order_no, "0"))
        if abs(after - before) >= Decimal("0.01"):
            rows.append(
                (
                    f"{order_no} 单位成本",
                    _money(before),
                    _money(after),
                    _money(after - before),
                )
            )

    resolved = [title for _code, title in before_pairs if (_code, title) not in after_pairs]
    remaining = [title for _code, title in after_pairs]
    return Comparison(resolved=resolved, remaining=remaining, rows=rows)


def render_comparison(comparison: Comparison) -> str:
    lines = [
        "## 补证后复跑 · 前后对比",
        "",
        render_table(
            ["指标", "补证前", "补证后", "变化"],
            [list(row) for row in comparison.rows],
            ["l", "r", "r", "r"],
        ),
        "",
    ]
    if comparison.resolved:
        lines.append("已消除：")
        for title in comparison.resolved:
            lines.append(f"- {title}")
    else:
        lines.append("已消除：无（数据没有变化，或补证未落地）")
    if comparison.remaining:
        lines.append("")
        lines.append("仍存在：")
        for title in comparison.remaining:
            lines.append(f"- {title}")
        lines.append("")
        lines.append("说明：剩余项可能是结构性缺口（如辅助部门本就没有报工），需要改配置而不是补单。")
    return "\n".join(lines)


__all__ = [
    "Comparison",
    "Task",
    "TaskBoard",
    "build_board",
    "compare",
    "diagnosis_stats",
    "render_board",
    "render_comparison",
]
