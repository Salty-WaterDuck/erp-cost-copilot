"""小型评测集：用固定用例守住「排雷 / 归因 / 反证 / RAG / 配置翻译」的行为。

不追求大而全，只覆盖最容易回归的判断：

- 诊断：某条差异的主因编码与最低置信度；
- 排雷：脏数据必须命中的异常编码；
- 反证：给定反证后的确定性置信度；
- RAG：查询必须命中指定文档；
- 配置：规则条数与风险提示。

用例放在 ``eval/cases.jsonl``，运行 ``python tools/run_eval.py``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .audit import detect_anomalies
from .challenge import challenge, pick_target
from .config_translator import parse_config
from .dataset import load_dataset
from .diagnosis import diagnose_facts
from .facts import build_variance_facts
from .llm_explainer import load_sense
from .rag import search
from .run import run_cost

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "eval" / "cases.jsonl"


@dataclass
class EvalResult:
    case_id: str
    kind: str
    passed: bool
    detail: str = ""


@dataclass
class EvalReport:
    results: list[EvalResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.results if item.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def by_kind(self) -> dict[str, tuple[int, int]]:
        summary: dict[str, list[int]] = {}
        for item in self.results:
            bucket = summary.setdefault(item.kind, [0, 0])
            bucket[0] += 1
            bucket[1] += 1 if item.passed else 0
        return {kind: (total, passed) for kind, (total, passed) in summary.items()}


def load_cases(path: str | Path | None = None) -> list[dict]:
    target = Path(path) if path else DEFAULT_CASES
    cases = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def _data_path(root: Path, name: str) -> Path:
    path = root / "data" / name
    if not path.exists():
        raise FileNotFoundError(f"数据集不存在：{path}")
    return path


def _diagnosis_case(case: dict, root: Path) -> tuple[bool, str]:
    ds = load_dataset(_data_path(root, case["dataset"]))
    cost_run = run_cost(ds)
    sense = load_sense()
    facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
    diagnoses = diagnose_facts(facts, cost_run)
    def matches(item) -> bool:
        if item.fact.cost_element != case["cost_element"]:
            return False
        if case.get("material") is not None and item.fact.material != case["material"]:
            return False
        if case.get("order") is not None and item.fact.order_no != case["order"]:
            return False
        if "cost_center" in case and item.fact.cost_center != case["cost_center"]:
            return False
        return True

    target = next((item for item in diagnoses if matches(item)), None)
    if target is None or target.top is None:
        return False, "未找到目标差异或诊断"
    expect = case["expect"]
    ok = target.top.code == expect["top_code"] and target.top.score >= expect.get(
        "min_score", 0.0
    )
    return (
        ok,
        f"top={target.top.code} score={target.top.score:.2f} "
        f"expect={expect['top_code']}/{expect.get('min_score', 0)}",
    )


def _anomaly_case(case: dict, root: Path) -> tuple[bool, str]:
    ds = load_dataset(_data_path(root, case["dataset"]))
    cost_run = run_cost(ds)
    codes = {item.code for item in detect_anomalies(ds, cost_run)}
    missing = [code for code in case["expect"]["codes"] if code not in codes]
    return not missing, f"codes={sorted(codes)}" if missing else "ok"


def _challenge_case(case: dict, root: Path) -> tuple[bool, str]:
    ds = load_dataset(_data_path(root, case["dataset"]))
    cost_run = run_cost(ds)
    sense = load_sense()
    facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
    diagnoses = diagnose_facts(facts, cost_run)
    target = pick_target(diagnoses, case["text"])
    outcome = challenge(target, case["text"])
    expect = case["expect"]
    ok = abs(outcome.adjusted_score - expect["adjusted_score"]) <= 0.01
    if "still_valid" in expect:
        ok = ok and outcome.still_valid == expect["still_valid"]
    return ok, f"score={outcome.adjusted_score:.2f} matched={outcome.matched}"


def _rag_case(case: dict, root: Path) -> tuple[bool, str]:
    hits = search(case["query"], k=case.get("k", 5), root=root)
    wanted = case["expect"]["source_contains"]
    ok = any(wanted in hit.document.source for hit in hits)
    return ok, f"hit={[hit.document.source for hit in hits]}"


def _config_case(case: dict, root: Path) -> tuple[bool, str]:
    text = (root / "data" / case["file"]).read_text(encoding="utf-8")
    review = parse_config(text)
    expect = case["expect"]
    ok = len(review.rules) == expect["rules"]
    if "warning_contains" in expect:
        ok = ok and any(expect["warning_contains"] in item for item in review.warnings)
    return ok, f"rules={len(review.rules)} warnings={len(review.warnings)}"


def run_case(case: dict, root: Path | None = None) -> EvalResult:
    base = root or ROOT
    kind = case["kind"]
    handlers = {
        "diagnosis": _diagnosis_case,
        "anomaly": _anomaly_case,
        "challenge": _challenge_case,
        "rag": _rag_case,
        "config": _config_case,
    }
    handler = handlers.get(kind)
    if handler is None:
        return EvalResult(case["id"], kind, False, f"未知用例类型：{kind}")
    try:
        ok, detail = handler(case, base)
    except Exception as exc:  # 评测要报告失败，而不是中断整批
        return EvalResult(case["id"], kind, False, f"异常：{exc}")
    return EvalResult(case["id"], kind, ok, detail)


def run_all(root: str | Path | None = None, cases_path: str | Path | None = None) -> EvalReport:
    base = Path(root) if root else ROOT
    return EvalReport(
        results=[run_case(case, base) for case in load_cases(cases_path)]
    )


def render_report(report: EvalReport) -> str:
    lines = [
        "## 评测报告",
        f"用例 {report.total} · 通过 {report.passed} · 通过率 {report.pass_rate * 100:.1f}%",
        "",
    ]
    lines.append("| 类型 | 总数 | 通过 |")
    lines.append("|---|---|---|")
    for kind, (total, passed) in sorted(report.by_kind().items()):
        lines.append(f"| {kind} | {total} | {passed} |")
    failures = [item for item in report.results if not item.passed]
    if failures:
        lines.append("")
        lines.append("失败项：")
        for item in failures:
            lines.append(f"- {item.case_id}（{item.kind}）：{item.detail}")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_CASES",
    "EvalReport",
    "EvalResult",
    "load_cases",
    "render_report",
    "run_all",
    "run_case",
]
