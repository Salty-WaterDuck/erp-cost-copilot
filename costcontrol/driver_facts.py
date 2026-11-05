"""ABC / TDABC 的动因事实契约：进入 ABC 之前的「可追溯数据单元」。

这一层不计算 ABC，只回答一件事：**这条动因记录能不能进 ABC，凭什么。**

每条事实都带来源、时间、资源、成本对象与采集时间；后续规则引擎据此判断
完整性、时序、重复、容量、单位、口径与对账问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

EXPECTED_UNITS = {
    "machine_hours": "h",
    "labor_hours": "h",
    "setup_hours": "h",
    "inspection_count": "次",
    "energy_kwh": "kWh",
}


@dataclass(frozen=True)
class Resource:
    code: str
    name: str
    available_hours_per_day: Decimal
    calendar: str = "两班"


@dataclass(frozen=True)
class DriverFact:
    event_id: str
    period: str
    activity: str
    driver_type: str
    quantity: Decimal
    unit: str
    cost_object: str
    resource: str
    event_start: datetime
    event_end: datetime
    ingested_at: datetime
    source_system: str
    source_tag: str
    state: str = "RUN"
    estimated: bool = False
    note: str = ""

    @property
    def duration_hours(self) -> Decimal:
        seconds = (self.event_end - self.event_start).total_seconds()
        return Decimal(str(seconds)) / Decimal(3600)

    @property
    def expected_unit(self) -> str | None:
        return EXPECTED_UNITS.get(self.driver_type)


@dataclass
class DriverDataset:
    company: str
    period: str
    close_date: date
    resources: dict[str, Resource]
    facts: list[DriverFact]
    declared_totals: dict[str, Decimal] = field(default_factory=dict)

    @property
    def activities(self) -> list[str]:
        return sorted({fact.activity for fact in self.facts})

    @property
    def cost_objects(self) -> list[str]:
        return sorted({fact.cost_object for fact in self.facts if fact.cost_object})


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_driver_dataset(path: str | Path) -> DriverDataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    resources = {
        item["code"]: Resource(
            code=item["code"],
            name=item.get("name", item["code"]),
            available_hours_per_day=_d(item.get("available_hours_per_day", 24)),
            calendar=item.get("calendar", "两班"),
        )
        for item in raw.get("resources", [])
    }
    facts = [
        DriverFact(
            event_id=item["event_id"],
            period=item.get("period", raw["period"]),
            activity=item["activity"],
            driver_type=item["driver_type"],
            quantity=_d(item["quantity"]),
            unit=item["unit"],
            cost_object=item.get("cost_object", ""),
            resource=item["resource"],
            event_start=_dt(item["event_start"]),
            event_end=_dt(item["event_end"]),
            ingested_at=_dt(item["ingested_at"]),
            source_system=item.get("source_system", "MES"),
            source_tag=item.get("source_tag", ""),
            state=item.get("state", "RUN"),
            estimated=bool(item.get("estimated", False)),
            note=item.get("note", ""),
        )
        for item in raw.get("facts", [])
    ]
    return DriverDataset(
        company=raw.get("company", ""),
        period=raw["period"],
        close_date=date.fromisoformat(raw["close_date"]),
        resources=resources,
        facts=facts,
        declared_totals={
            activity: _d(value)
            for activity, value in (raw.get("driver_totals", {}) or {}).items()
        },
    )


__all__ = [
    "DriverDataset",
    "DriverFact",
    "EXPECTED_UNITS",
    "Resource",
    "load_driver_dataset",
]
