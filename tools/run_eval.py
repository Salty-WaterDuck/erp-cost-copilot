"""运行评测集：python tools/run_eval.py [--json eval/report.json]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from costcontrol.evaluation import DEFAULT_CASES, render_report, run_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 ERP-Cost-Copilot 评测集")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json", type=Path, help="把汇总写入 JSON")
    args = parser.parse_args()

    report = run_all(ROOT, args.cases)
    print(render_report(report))

    if args.json:
        payload = {
            "total": report.total,
            "passed": report.passed,
            "pass_rate": round(report.pass_rate, 4),
            "by_kind": {
                kind: {"total": total, "passed": passed}
                for kind, (total, passed) in report.by_kind().items()
            },
            "failures": [
                {
                    "id": item.case_id,
                    "kind": item.kind,
                    "detail": item.detail,
                }
                for item in report.results
                if not item.passed
            ],
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n汇总已写入 {args.json}")

    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
