import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol.cli import main
from costcontrol.llm_explainer import (
    DeepSeekProvider,
    LLMError,
    LLMExplainer,
    OllamaProvider,
    build_facts,
    build_prompt,
    load_sense,
    provider_from_env,
    render_offline_checklist,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "demo_ningbo.json"


class PromptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.prompt = build_prompt(cls.cost_run, cls.sense)

    def test_facts(self):
        facts = build_facts(self.cost_run)
        self.assertEqual(facts["period"], "2026-10")
        self.assertEqual(facts["totals"]["total_cost"], "60054.14")
        self.assertEqual(facts["totals"]["unallocated_overhead"], "260.00")
        self.assertEqual(len(facts["alerts"]), 8)
        self.assertEqual(len(facts["orders"]), 4)
        order = next(o for o in facts["orders"] if o["order_no"] == "PO-1001")
        self.assertEqual(order["variance"], "953.48")
        self.assertIn("材料价差", order["variances"])

    def test_system_prompt_sets_guardrails(self):
        self.assertIn("不得编造", self.prompt.system)
        self.assertIn("人工复核", self.prompt.system)

    def test_user_prompt_contains_facts_and_sense(self):
        user = self.prompt.user
        self.assertIn("2026-10", user)
        self.assertIn("PO-1001", user)
        self.assertIn("离散制造", user)
        self.assertIn("machine_hours", user)
        self.assertIn("排查清单", user)
        self.assertIn("PO-1001", user)

    def test_messages_shape(self):
        messages = self.prompt.as_messages()
        self.assertEqual([m["role"] for m in messages], ["system", "user"])


class OfflineReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.sense = load_sense()
        cls.text = render_offline_checklist(cls.cost_run, cls.sense)

    def test_contains_key_numbers_and_orders(self):
        self.assertIn("60,054.14", self.text)
        self.assertIn("PO-1001", self.text)
        self.assertIn("953.48", self.text)
        self.assertIn("PO-1003", self.text)

    def test_uses_business_sense(self):
        self.assertIn("退料未入库", self.text)
        self.assertIn("工单超支", self.text)
        self.assertIn("设备维护组", self.text)

    def test_marks_advisory_only(self):
        self.assertIn("不替代审批", self.text)
        self.assertIn("离线规则", self.text)

    def test_respects_max_items(self):
        text = render_offline_checklist(self.cost_run, self.sense, max_items=2)
        self.assertIn("另有", text)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.prompt = build_prompt(
            run_cost(load_dataset(DATA)), load_sense()
        )

    def test_deepseek_payload(self):
        captured = {}

        def transport(url, payload, headers, timeout):
            captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
            return {"choices": [{"message": {"content": "## 结论\nok"}}]}

        provider = DeepSeekProvider("test-key", transport=transport)
        result = provider.complete(self.prompt)
        self.assertEqual(result.text, "## 结论\nok")
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(captured["payload"]["model"], "deepseek-chat")
        self.assertEqual(captured["payload"]["messages"][0]["role"], "system")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")

    def test_deepseek_bad_shape(self):
        provider = DeepSeekProvider("k", transport=lambda *args: {"oops": 1})
        with self.assertRaises(LLMError):
            provider.complete(self.prompt)

    def test_ollama_payload(self):
        captured = {}

        def transport(url, payload, headers, timeout):
            captured.update(url=url, payload=payload)
            return {"message": {"content": "本地模型回答"}}

        provider = OllamaProvider(model="qwen2.5:7b", transport=transport)
        result = provider.complete(self.prompt)
        self.assertEqual(result.text, "本地模型回答")
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(captured["payload"]["stream"], False)

    def test_provider_from_env(self):
        self.assertIsNone(provider_from_env({}))
        self.assertIsNone(provider_from_env({"CC_LLM_PROVIDER": "offline"}))
        deepseek = provider_from_env(
            {"CC_LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "k"}
        )
        self.assertIsInstance(deepseek, DeepSeekProvider)
        ollama = provider_from_env({"CC_LLM_PROVIDER": "ollama"})
        self.assertIsInstance(ollama, OllamaProvider)
        with self.assertRaises(LLMError):
            provider_from_env({"CC_LLM_PROVIDER": "deepseek"})
        with self.assertRaises(LLMError):
            provider_from_env({"CC_LLM_PROVIDER": "unknown"})

    def test_explainer_with_provider(self):
        def transport(url, payload, headers, timeout):
            return {"choices": [{"message": {"content": "模型输出"}}]}

        explainer = LLMExplainer(
            load_sense(), DeepSeekProvider("k", transport=transport)
        )
        explanation = explainer.explain(run_cost(load_dataset(DATA)))
        self.assertEqual(explanation.text, "模型输出")
        self.assertEqual(explanation.provider, "deepseek")
        self.assertIn("2026-10", explanation.prompt.user)

    def test_explainer_falls_back_to_offline(self):
        explanation = LLMExplainer(load_sense()).explain(run_cost(load_dataset(DATA)))
        self.assertEqual(explanation.provider, "offline")
        self.assertIn("差异排查", explanation.text)
        self.assertIn("数据排雷", explanation.text)


class CliExplainTest(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_offline_explain(self):
        code, text = self.run_cli("--explain", "--offline")
        self.assertEqual(code, 0)
        self.assertIn("差异排查", text)
        self.assertIn("provider: offline", text)

    def test_show_prompt(self):
        code, text = self.run_cli("--explain", "--offline", "--show-prompt")
        self.assertEqual(code, 0)
        self.assertIn("system prompt", text)
        self.assertIn("不得编造", text)

    def test_missing_sense_file(self):
        code, text = self.run_cli("--explain", "--sense", "no/such.yaml")
        self.assertEqual(code, 2)
        self.assertIn("配置错误", text)


if __name__ == "__main__":
    unittest.main()
