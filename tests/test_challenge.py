import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.challenge import challenge, pick_target, render_challenge
from costcontrol.cli import main
from costcontrol.diagnosis import diagnose_facts
from costcontrol.facts import build_variance_facts
from costcontrol.llm_explainer import load_sense

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


if __name__ == "__main__":
    unittest.main()
