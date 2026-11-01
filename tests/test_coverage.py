import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costcontrol.cli import main
from costcontrol.coverage import CoverageError, check_export, render_coverage

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "auto_reports"


class CoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = check_export(DATA)
        cls.caps = {item.name: item for item in cls.report.capabilities}

    def test_all_files_recognized(self):
        self.assertEqual(len(self.report.files), 13)
        self.assertEqual(self.report.recognized, 13)
        kinds = {item.kind for item in self.report.files}
        self.assertEqual(
            kinds,
            {
                "sap_cost_structure",
                "sap_inventory",
                "kingdee_cost_sheet",
                "kingdee_restoration",
                "yonyou_daily",
                "yonyou_scrap",
                "bom",
                "issues",
                "labor",
                "materials",
                "orders",
                "overhead",
                "routing",
            },
        )

    def test_report_level_available(self):
        self.assertEqual(self.caps["报表级成本体检"].status, "可用")
        self.assertEqual(self.caps["跨表勾稽"].status, "可用")

    def test_document_level_partial(self):
        self.assertEqual(self.caps["材料差异（六项）"].status, "部分")
        self.assertEqual(self.caps["人工效率差异"].status, "部分")
        self.assertEqual(self.caps["制造费用分配"].status, "可用")
        self.assertEqual(self.caps["约当产量/在产"].status, "可用")

    def test_semantic_checks(self):
        checks = {text: ok for ok, text in self.report.checks}
        self.assertTrue(any("18/18" in text and "领料" in text for text in checks))
        self.assertTrue(any("18/18" in text and "人工" in text for text in checks))
        self.assertTrue(any("6/6" in text for text in checks))
        bom = next(text for text in checks if "BOM 物料" in text)
        self.assertFalse(checks[bom])
        self.assertIn("4/7", bom)
        hours = next(text for text in checks if "标准工时" in text)
        self.assertFalse(checks[hours])
        self.assertIn("11.3 倍", hours)

    def test_render(self):
        text = render_coverage(self.report)
        self.assertIn("导出覆盖率检查", text)
        self.assertIn("可用", text)
        self.assertIn("部分", text)
        self.assertIn("BOM 物料 vs 领料编码", text)

    def test_missing_directory(self):
        with self.assertRaises(CoverageError):
            check_export(DATA / "missing")


class CoverageCliTest(unittest.TestCase):
    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--check-export", str(DATA)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("导出覆盖率检查", text)

    def test_cli_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--check-export", str(DATA / "missing")])
        self.assertEqual(code, 2)
        self.assertIn("覆盖率检查失败", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
