"""生成脏数据演示集：在干净演示数据上故意制造三类典型问题。

1. PO-1002 完工但没有报工（人工 0，费用也未吸收）；
2. PO-1002 出现负数领用，但没有退料标记；
3. 新增喷涂中心 CC-PAINT：有费用、有工艺路线，但没有任何报工（动因全 0）。

生成脚本保留在仓库里，方便复现与调整。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "demo_ningbo.json"
TARGET = ROOT / "data" / "dirty_erp_export.json"


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))

    data["company"] = "宁波恒宇电器有限公司（脏数据演示）"

    # 1. 新增喷涂中心：有费用、有工艺路线，但没有报工
    data["cost_centers"].append(
        {
            "code": "CC-PAINT",
            "name": "喷涂车间",
            "labor_rate": "30.00",
            "overhead_budget": "1200.00",
            "budget_hours": "50.00",
        }
    )
    data["routing"].append(
        {
            "product": "FG-KT100",
            "seq": 15,
            "cost_center": "CC-PAINT",
            "std_hours": "0.03",
            "operation": "喷涂",
        }
    )
    data["overhead"].append(
        {
            "doc_no": "OH-PAINT-01",
            "cost_center": "CC-PAINT",
            "expense_type": "喷涂线电费",
            "amount": "1200.00",
        }
    )

    # 2. PO-1002 完工，但删除它的报工记录
    for order in data["orders"]:
        if order["order_no"] == "PO-1002":
            order["status"] = "COMPLETED"
            order["qty_planned"] = "692"
            order["wip_progress"] = "0"
    data["labor"] = [
        row for row in data["labor"] if row["order_no"] != "PO-1002"
    ]

    # 3. 负数领用，但没有退料标记（is_return 缺省为 false）
    data["issues"].append(
        {
            "doc_no": "MI-1016",
            "order_no": "PO-1002",
            "material": "RM-ABS",
            "qty": "-10",
            "unit_price": "17.60",
            "issue_date": "2026-10-18",
        }
    )

    TARGET.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已生成 {TARGET}")


if __name__ == "__main__":
    main()
