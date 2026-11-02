import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from uuid import uuid4

from costcontrol import load_dataset, run_cost
from costcontrol.challenge import (
    TOOL_NAME,
    ChallengeAction,
    ChallengeDecision,
    ChallengeValidationError,
    challenge,
    decision_from_arguments,
    evidence_items,
    pick_target,
    record_review,
    render_challenge,
    review_with_llm,
    validate_decision,
)
from costcontrol.cli import main
from costcontrol.diagnosis import diagnose_facts
from costcontrol.facts import build_variance_facts
from costcontrol.llm_explainer import ToolCall, load_sense

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "demo_ningbo.json"


class ChallengeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(DEMO)
        cls.cost_run = run_cost(cls.ds)
        cls.sense = load_sense()
        facts = build_variance_facts(cls.cost_run, cls.sense.raw.get("org", {}))
        cls.diagnoses = diagnose_facts(facts, cls.cost_run)

    def test_pick_matching_diagnosis(self):
        target = pick_target(self.diagnoses, "车间说已经退料了，只是单据没录")
        self.assertEqual(target.fact.material, "RM-SS304")
        self.assertEqual(target.fact.order_no, "PO-1001")

    def test_challenge_lowers_confidence(self):
        target = pick_target(self.diagnoses, "车间说已经退料了，只是单据没录")
        before = target.top.score
        outcome = challenge(target, "车间说已经退料了，只是单据没录")
        self.assertTrue(outcome.matched)
        self.assertLess(outcome.adjusted_score, before)
        self.assertFalse(outcome.still_valid)
        self.assertEqual(outcome.adjusted_confidence, "低")
        self.assertIn("退料单", outcome.actions[0])

    def test_unmatched_challenge_only_slight_drop(self):
        target = pick_target(self.diagnoses, "车间说已经退料了，只是单据没录")
        outcome = challenge(target, "我们觉得这个数字看起来不对")
        self.assertFalse(outcome.matched)
        self.assertAlmostEqual(
            outcome.original_score - outcome.adjusted_score, 0.12, places=2
        )

    def test_render(self):
        target = pick_target(self.diagnoses, "车间说已经退料了，只是单据没录")
        text = render_challenge(challenge(target, "车间说已经退料了，只是单据没录"))
        self.assertIn("反证复核", text)
        self.assertIn("原结论", text)
        self.assertIn("下一步", text)
        self.assertIn("退料", text)


class ChallengeCliTest(unittest.TestCase):
    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                [
                    "--diagnose",
                    "--challenge",
                    "车间说已经退料了，只是单据没录",
                    "--offline",
                ]
            )
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("反证复核", text)
        self.assertIn("置信度由 0.54 下调到 0.29", text)

    def test_requires_diagnose(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--challenge", "退料了"])
        self.assertEqual(code, 2)
        self.assertIn("--diagnose", out.getvalue() + err.getvalue())


class FakeToolProvider:
    name = "fake"
    model = "fake-tool"

    def __init__(self, arguments=None, function_name=TOOL_NAME, error=None):
        self.arguments = arguments
        self.function_name = function_name
        self.error = error
        self.prompt = None
        self.tool_schema = None

    def complete(self, prompt):
        raise AssertionError("不应调用文本模式")

    def complete_tool(self, prompt, tool_schema):
        self.prompt = prompt
        self.tool_schema = tool_schema
        if self.error is not None:
            raise self.error
        return ToolCall(self.function_name, self.arguments)


class TextOnlyProvider:
    name = "text"
    model = "text-model"

    def complete(self, prompt):
        raise AssertionError("不应调用文本模式")


def valid_arguments(target, score=0.29, verdict="weakened", evidence=("E2",)):
    return {
        "verdict": verdict,
        "hit_evidence": list(evidence),
        "adjusted_score": score,
        "rationale": "反证直接削弱了无退料单这条证据。",
        "actions": [
            {
                "owner": "仓库主管",
                "action": "补录退料单并重新导出",
                "evidence_needed": "退料单号、日期、数量",
            }
        ],
    }


class StructuredChallengeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_dataset(DEMO)
        cost_run = run_cost(ds)
        cls.sense = load_sense()
        facts = build_variance_facts(cost_run, cls.sense.raw.get("org", {}))
        diagnoses = diagnose_facts(facts, cost_run)
        cls.target = pick_target(diagnoses, "车间说已经退料了，只是单据没录")
        cls.original = cls.target.top.score

    def test_valid_tool_call(self):
        provider = FakeToolProvider(valid_arguments(self.target))
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        self.assertEqual(review.provider, "fake")
        self.assertIsNotNone(review.decision)
        self.assertEqual(review.decision.verdict, "weakened")
        self.assertEqual(review.decision.adjusted_score, 0.29)
        self.assertIn("证据被削弱", review.text)
        self.assertIn("E2", provider.prompt.user)
        self.assertEqual(provider.tool_schema["function"]["name"], TOOL_NAME)

    def test_score_out_of_range_falls_back(self):
        provider = FakeToolProvider(valid_arguments(self.target, score=1.4))
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        self.assertIsNone(review.decision)
        self.assertEqual(review.provider, "fallback")
        self.assertIn("0–1", review.fallback_reason)
        self.assertIn("已回退离线规则", review.text)

    def test_unknown_evidence_falls_back(self):
        provider = FakeToolProvider(valid_arguments(self.target, evidence=("E99",)))
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        self.assertIsNone(review.decision)
        self.assertIn("E99", review.fallback_reason)

    def test_verdict_score_conflict_falls_back(self):
        provider = FakeToolProvider(
            valid_arguments(self.target, score=0.8, verdict="overturned")
        )
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        self.assertIsNone(review.decision)
        self.assertIn("上调", review.fallback_reason)

    def test_unknown_function_falls_back(self):
        provider = FakeToolProvider(
            valid_arguments(self.target), function_name="drop_table"
        )
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        self.assertIsNone(review.decision)
        self.assertIn("未知函数", review.fallback_reason)

    def test_provider_without_tools_falls_back(self):
        review = review_with_llm(
            self.target,
            "车间说已经退料了，只是单据没录",
            self.sense,
            TextOnlyProvider(),
        )
        self.assertIsNone(review.decision)
        self.assertEqual(review.provider, "fallback")
        self.assertIn("不支持工具调用", review.fallback_reason)

    def test_record_review(self):
        provider = FakeToolProvider(valid_arguments(self.target))
        review = review_with_llm(
            self.target, "车间说已经退料了，只是单据没录", self.sense, provider
        )
        directory = ROOT / f".test-{uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            path = directory / "challenges.jsonl"
            record = record_review(review, self.target, path)
            self.assertIsNotNone(record)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            saved = json.loads(lines[0])
            self.assertEqual(saved["verdict"], "weakened")
            self.assertEqual(saved["adjusted_score"], 0.29)
            self.assertEqual(saved["provider"], "fake")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class DecisionValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = load_dataset(DEMO)
        cost_run = run_cost(ds)
        sense = load_sense()
        facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
        cls.target = pick_target(
            diagnose_facts(facts, cost_run), "车间说已经退料了，只是单据没录"
        )

    def test_decision_from_arguments(self):
        decision = decision_from_arguments(valid_arguments(self.target))
        self.assertEqual(decision.verdict, "weakened")
        self.assertEqual(decision.hit_evidence, ("E2",))
        self.assertEqual(decision.actions[0].owner, "仓库主管")

    def test_reject_string_score(self):
        arguments = valid_arguments(self.target)
        arguments["adjusted_score"] = "0.3"
        with self.assertRaises(ChallengeValidationError):
            decision_from_arguments(arguments)

    def test_reject_extra_field(self):
        arguments = valid_arguments(self.target)
        arguments["note"] = "extra"
        with self.assertRaises(ChallengeValidationError):
            decision_from_arguments(arguments)

    def test_reject_empty_actions(self):
        arguments = valid_arguments(self.target)
        arguments["actions"] = []
        with self.assertRaises(ChallengeValidationError):
            decision_from_arguments(arguments)

    def test_upheld_cannot_hit_evidence(self):
        decision = ChallengeDecision(
            verdict="upheld",
            hit_evidence=("E2",),
            adjusted_score=0.54,
            rationale="反证不成立",
            actions=(ChallengeAction("仓库主管", "复核", "退料单"),),
        )
        with self.assertRaises(ChallengeValidationError):
            validate_decision(decision, self.target)

    def test_needs_evidence_cannot_raise(self):
        decision = ChallengeDecision(
            verdict="needs_evidence",
            hit_evidence=(),
            adjusted_score=0.60,
            rationale="需要补证",
            actions=(ChallengeAction("仓库主管", "补单", "退料单"),),
        )
        with self.assertRaises(ChallengeValidationError):
            validate_decision(decision, self.target)

    def test_valid_weakened(self):
        decision = ChallengeDecision(
            verdict="weakened",
            hit_evidence=("E2",),
            adjusted_score=0.29,
            rationale="无退料单这条证据被削弱",
            actions=(ChallengeAction("仓库主管", "补录退料单", "退料单号"),),
        )
        validate_decision(decision, self.target)
        self.assertIn("E2", evidence_items(self.target))


if __name__ == "__main__":
    unittest.main()
