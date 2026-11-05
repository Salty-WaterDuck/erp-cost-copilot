import importlib.util
import unittest

HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
HAS_HTTPX = importlib.util.find_spec("httpx") is not None


@unittest.skipUnless(HAS_FASTAPI and HAS_HTTPX, "fastapi/httpx 未安装")
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from service.app import app

        cls.client = TestClient(app)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertIn("demo", payload["datasets"])

    def test_rag(self):
        response = self.client.post(
            "/v1/rag", json={"query": "6602 管理费用 5101 制造费用", "k": 2}
        )
        self.assertEqual(response.status_code, 200)
        hits = response.json()["hits"]
        self.assertTrue(hits)
        self.assertTrue(any("accounting" in hit["source"] for hit in hits))

    def test_checkup_dirty(self):
        response = self.client.get("/v1/checkup?dataset=dirty")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["anomaly_count"], 3)
        self.assertIn("数据排雷", payload["markdown"])

    def test_diagnose(self):
        response = self.client.get("/v1/diagnose?dataset=demo&limit=3")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload["total"], 0)
        self.assertTrue(payload["items"][0]["top"]["code"])

    def test_challenge(self):
        response = self.client.post(
            "/v1/challenge",
            json={
                "dataset": "demo",
                "text": "车间说已经退料了，只是单据没录",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertAlmostEqual(payload["adjusted_score"], 0.29, places=2)
        self.assertFalse(payload["still_valid"])

    def test_translate_config(self):
        text = (
            "电费按机器工时分摊：总装车间 60%，质检中心 40%。\n"
            "备注：机器工时本月尚未更新。"
        )
        response = self.client.post("/v1/translate-config", json={"text": text})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["rules"], 1)
        self.assertTrue(payload["warnings"])

    def test_eval(self):
        response = self.client.get("/v1/eval")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["passed"], payload["total"])
        self.assertEqual(payload["pass_rate"], 1.0)

    def test_abc_check(self):
        response = self.client.get("/v1/abc-check?file=sample")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["period"], "2026-10")
        self.assertGreaterEqual(payload["counts"]["rejected"], 8)
        self.assertIn("ABC 动因数据前置校验", payload["markdown"])

    def test_unknown_dataset(self):
        response = self.client.get("/v1/checkup?dataset=../../etc/passwd")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
