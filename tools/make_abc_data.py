"""生成 ABC 动因数据演示集：40+ 条 MES/IoT 风格记录，故意埋 8 类错误。

生成脚本保留在仓库里，方便调整规则与评测用例。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "data" / "abc_driver_sample.json"


def dt(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00"


def main() -> None:
    resources = [
        {"code": "MC-01", "name": "CNC-01", "available_hours_per_day": "20", "calendar": "两班"},
        {"code": "PT-01", "name": "喷涂线", "available_hours_per_day": "16", "calendar": "两班"},
        {"code": "AS-01", "name": "装配线", "available_hours_per_day": "20", "calendar": "两班"},
        {"code": "QA-01", "name": "检测台", "available_hours_per_day": "8", "calendar": "白班"},
    ]

    facts = []

    def add(
        event_id: str,
        activity: str,
        driver_type: str,
        quantity: str,
        unit: str,
        cost_object: str,
        resource: str,
        start: str,
        end: str,
        ingested: str,
        state: str = "RUN",
        estimated: bool = False,
        tag: str = "",
        note: str = "",
    ) -> dict:
        item = {
            "event_id": event_id,
            "period": "2026-10",
            "activity": activity,
            "driver_type": driver_type,
            "quantity": quantity,
            "unit": unit,
            "cost_object": cost_object,
            "resource": resource,
            "event_start": start,
            "event_end": end,
            "ingested_at": ingested,
            "source_system": "MES" if driver_type != "energy_kwh" else "IoT",
            "source_tag": tag or f"{resource}-TAG",
            "state": state,
            "estimated": estimated,
            "note": note,
        }
        facts.append(item)
        return item

    cost_objects = [f"PO-260{i}" for i in range(1, 7)]
    durations = [3, 5, 4, 2, 6, 3, 4]
    days = [1, 5, 9, 13, 17, 21, 25]
    for index, day in enumerate(days):
        start = dt(day, 8)
        end = dt(day, 8 + durations[index])
        add(
            f"EV-MC-{index + 1:02d}",
            "机加",
            "machine_hours",
            str(durations[index]),
            "h",
            cost_objects[index % len(cost_objects)],
            "MC-01",
            start,
            end,
            dt(day, 23),
        )

    for index, day in enumerate([2, 6, 10, 14, 18, 22, 26]):
        hours = 4 + index % 3
        add(
            f"EV-PT-{index + 1:02d}",
            "喷涂",
            "machine_hours",
            str(hours),
            "h",
            cost_objects[(index + 2) % len(cost_objects)],
            "PT-01",
            dt(day, 8),
            dt(day, 8 + hours),
            dt(day, 23),
        )

    for index, day in enumerate([1, 4, 8, 12, 16, 20, 24]):
        hours = 6 + index % 2
        add(
            f"EV-AS-{index + 1:02d}",
            "装配",
            "labor_hours",
            str(hours),
            "h",
            cost_objects[(index + 3) % len(cost_objects)],
            "AS-01",
            dt(day, 8),
            dt(day, 8 + hours),
            dt(day, 23),
        )

    for index, day in enumerate([3, 7, 11, 15, 19, 23, 27]):
        count = 1 + index % 5
        add(
            f"EV-QA-{index + 1:02d}",
            "检测",
            "inspection_count",
            str(count),
            "次",
            cost_objects[(index + 1) % len(cost_objects)],
            "QA-01",
            dt(day, 9),
            dt(day, 9, 20),
            dt(day, 23),
        )

    # 传感器僵值：同一采集点连续 5 条相同读数
    for index, day in enumerate([3, 4, 5, 6, 7]):
        add(
            f"EV-EN-{index + 1:02d}",
            "能源",
            "energy_kwh",
            "12.5",
            "kWh",
            cost_objects[index % len(cost_objects)],
            "PT-01",
            dt(day, 0),
            dt(day, 1),
            dt(day, 2),
            tag="PT-01-KWH",
        )

    # 错误 1：零动因
    add(
        "EV-ERR-01", "机加", "machine_hours", "0", "h", "PO-2601", "MC-01",
        dt(17, 0), dt(17, 0), dt(17, 1),
    )
    # 错误 2：负动因
    add(
        "EV-ERR-02", "机加", "machine_hours", "-2", "h", "PO-2602", "MC-01",
        dt(22, 8), dt(22, 10), dt(22, 23),
    )
    # 错误 3：同资源重叠
    add(
        "EV-ERR-03", "机加", "machine_hours", "2", "h", "PO-2603", "MC-01",
        dt(5, 10), dt(5, 12), dt(5, 23),
    )
    # 错误 4：缺成本对象
    add(
        "EV-ERR-04", "装配", "labor_hours", "6", "h", "", "AS-01",
        dt(28, 8), dt(28, 14), dt(28, 23),
    )
    # 错误 5：单位不一致
    add(
        "EV-ERR-05", "喷涂", "machine_hours", "240", "min", "PO-2604", "PT-01",
        dt(28, 8), dt(28, 12), dt(28, 23),
    )
    # 错误 6：完全重复（与 EV-MC-03 同时段）
    add(
        "EV-ERR-06", "机加", "machine_hours", "4", "h", "PO-2604", "MC-01",
        dt(9, 8), dt(9, 12), dt(9, 23),
    )
    # 错误 7：单日超产能（22h，超过 20h 可用产能）
    add(
        "EV-ERR-07", "机加", "machine_hours", "22", "h", "PO-2605", "MC-01",
        dt(30, 0), dt(30, 22), dt(30, 23),
    )
    # 错误 8：关账后迟到
    add(
        "EV-ERR-08", "喷涂", "machine_hours", "5", "h", "PO-2606", "PT-01",
        dt(29, 8), dt(29, 13), "2026-11-20T10:00:00",
    )
    # 错误 9：停机却记动因
    add(
        "EV-ERR-09", "检测", "inspection_count", "2", "次", "PO-2601", "QA-01",
        dt(23, 10), dt(23, 10, 20), dt(23, 23), state="DOWN",
    )
    # 错误 10：估算值（允许进入，但必须打标）
    add(
        "EV-ERR-10", "装配", "labor_hours", "8", "h", "PO-2602", "AS-01",
        dt(29, 8), dt(29, 16), dt(29, 23),
        estimated=True, note="考勤缺失，按标准工时估算",
    )

    totals = {}
    for item in facts:
        activity = item["activity"]
        totals[activity] = totals.get(activity, Decimal("0")) + Decimal(item["quantity"])
    # 故意让喷涂台账比事实少 4h，触发对账异常
    totals["喷涂"] = totals["喷涂"] - Decimal("4")

    payload = {
        "company": "宁波恒宇电器有限公司（ABC 动因演示）",
        "period": "2026-10",
        "close_date": "2026-11-05",
        "resources": resources,
        "facts": facts,
        "driver_totals": {activity: str(value) for activity, value in totals.items()},
    }
    TARGET.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已生成 {TARGET}，{len(facts)} 条动因事实")


if __name__ == "__main__":
    main()
