import unittest

from costcontrol.evaluation import load_cases, render_report, run_all, run_case


class EvaluationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_all()
        cls.by_id = {item.case_id: item for item in cls.report.results}

    def test_cases_loaded(self):
        self.assertGreaterEqual(len(load_cases()), 12)

    def test_all_pass(self):
        failures = [item for item in self.report.results if not item.passed]
        self.assertEqual(failures, [], render_report(self.report))
        self.assertEqual(self.report.passed, self.report.total)

    def test_covers_all_kinds(self):
        kinds = {item.kind for item in self.report.results}
        self.assertEqual(
            kinds, {"diagnosis", "anomaly", "challenge", "rag", "config", "abc"}
        )

    def test_unknown_kind_fails_gracefully(self):
        result = run_case({"id": "x", "kind": "unknown"})
        self.assertFalse(result.passed)
        self.assertIn("未知用例类型", result.detail)

    def test_render(self):
        text = render_report(self.report)
        self.assertIn("评测报告", text)
        self.assertIn("100.0%", text)


if __name__ == "__main__":
    unittest.main()
