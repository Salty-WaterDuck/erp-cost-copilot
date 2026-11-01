import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costcontrol.cli import main
from costcontrol.config_translator import (
    parse_config,
    render_offline_translation,
    translate_config,
)
from costcontrol.llm_explainer import ChatResult, load_sense

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "erp_allocation_config.txt"


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.prompt = None

    def complete(self, prompt):
        self.prompt = prompt
        return ChatResult(text="模型输出", provider=self.name, model=self.model)


class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CONFIG.read_text(encoding="utf-8")
        cls.review = parse_config(cls.text)

    def test_rules(self):
        self.assertEqual(len(self.review.rules), 4)
        first = self.review.rules[0]
        self.assertEqual(first.pool, "电费")
        self.assertEqual(first.driver, "机器工时")
        self.assertEqual(first.centers, ("总装车间", "喷涂车间", "质检中心"))
        self.assertEqual(first.shares, (40, 35, 25))

    def test_notes_for_direct_charge(self):
        self.assertTrue(any("维修" in note for note in self.review.notes))

    def test_warnings(self):
        text = "\n".join(self.review.warnings)
        self.assertIn("尚未更新", text)
        self.assertIn("机器工时", text)

    def test_offline_render(self):
        text = render_offline_translation(self.review)
        self.assertIn("这条配置在说什么", text)
        self.assertIn("总装车间 40%", text)
        self.assertIn("可能踩的坑", text)
        self.assertIn("建议动作", text)


class TranslateTest(unittest.TestCase):
    def test_provider_used(self):
        provider = FakeProvider()
        result = translate_config(
            CONFIG.read_text(encoding="utf-8"), load_sense(), provider
        )
        self.assertEqual(result.text, "模型输出")
        self.assertEqual(result.provider, "fake")
        self.assertIn("分摊配置", result.prompt.user)


class TranslateCliTest(unittest.TestCase):
    def test_config_file(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--translate-config", str(CONFIG), "--offline"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("这条配置在说什么", text)
        self.assertIn("尚未更新", text)

    def test_inline_text(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                [
                    "--config-text",
                    "电费按机器工时分摊：总装车间 60%，质检中心 40%。",
                    "--offline",
                ]
            )
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("总装车间 60%", text)


if __name__ == "__main__":
    unittest.main()
