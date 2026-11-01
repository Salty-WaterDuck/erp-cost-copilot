import http.client
import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from costcontrol import load_dataset, run_cost
from costcontrol import web

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_ningbo.json"


class WebServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cost_run = run_cost(load_dataset(DATA))
        cls.server = web.make_server(cls.cost_run, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def test_dashboard(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("成本控制台", body)
        self.assertIn("实际总成本", body)
        self.assertIn("60,054.14", body)
        self.assertIn('href="/order/PO-1001"', body)

    def test_order_detail(self):
        status, body = self.get("/order/PO-1003")
        self.assertEqual(status, 200)
        self.assertIn("壶身组件", body)
        self.assertIn("差异分解", body)

    def test_unknown_order_404(self):
        status, body = self.get("/order/PO-NOPE")
        self.assertEqual(status, 404)
        self.assertIn("页面不存在", body)

    def test_journal(self):
        status, body = self.get("/journal")
        self.assertEqual(status, 200)
        self.assertIn("试算平衡表", body)
        self.assertIn("5001", body)

    def test_restore(self):
        status, body = self.get("/restore")
        self.assertEqual(status, 200)
        self.assertIn("差异穿透", body)
        self.assertIn("680.13", body)

    def test_standard(self):
        status, body = self.get("/standard")
        self.assertEqual(status, 200)
        self.assertIn("FG-KT100", body)
        self.assertIn("48.8540", body)

    def test_api_summary(self):
        status, body = self.get("/api/summary")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["period"], "2026-10")
        self.assertEqual(payload["total_cost"], "60054.14")
        self.assertEqual(len(payload["orders"]), 4)

    def test_404(self):
        status, _body = self.get("/nope")
        self.assertEqual(status, 404)

    def test_only_get_is_supported(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/")
            response = conn.getresponse()
            self.assertEqual(response.status, 501)
        finally:
            conn.close()


class EscapingTest(unittest.TestCase):
    def test_material_name_is_escaped(self):
        ds = load_dataset(DATA)
        ds.materials["RM-SS304"] = replace(
            ds.materials["RM-SS304"], name="<script>alert(1)</script>"
        )
        run = run_cost(ds)
        status, _content_type, body = web.render(run, "/order/PO-1001")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)


if __name__ == "__main__":
    unittest.main()
