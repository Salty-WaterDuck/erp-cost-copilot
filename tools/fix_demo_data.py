"""生成「已补证」的演示数据：把脏数据里的三处问题补上，用于前后对比。

- PO-1002 补一条 27h 的报工（原先完工未报工）；
- MI-1016 标记为退料（原先负数领用无退料）；
- PO-1003 补一条喷涂车间 15h 的报工（原先动因为 0）。

这只是演示用的「模拟补证」，真实项目里应当等仓库/车间补录后重新导出。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "dirty_erp_export.json"
TARGET = ROOT / "data" / "dirty_erp_export_fixed.json"


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    data["company"] = "宁波恒宇电器有限公司（已补证）"

    data["labor"].append(
        {
            "doc_no": "LR-2002",
            "order_no": "PO-1002",
            "cost_center": "CC-ASM",
            "hours": "27",
            "hourly_rate": "29.00",
            "work_date": "2026-10-11",
        }
    )
    data["labor"].append(
        {
            "doc_no": "LR-2007",
            "order_no": "PO-1003",
            "cost_center": "CC-PAINT",
            "hours": "15",
            "hourly_rate": "30.00",
            "work_date": "2026-10-20",
            "operation": "喷涂",
        }
    )
    for issue in data["issues"]:
        if issue["doc_no"] == "MI-1016":
            issue["is_return"] = True

    TARGET.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已生成 {TARGET}")


if __name__ == "__main__":
    main()
