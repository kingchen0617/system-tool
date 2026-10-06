"""測試：python -m pytest -q   或   python -m unittest discover -s tests"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.detection import evaluate_series  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.llm_rca import OllamaRCA  # noqa: E402
from app.notifications import format_message  # noqa: E402

DEFAULTS = {"step": 60, "for": 300, "window": 3600, "zscore": 3.0}


def make_engine(llm: bool = False, ollama_url: str = "") -> Engine:
    s = Settings()
    s.llm_enabled = llm
    if ollama_url:
        s.ollama_url = ollama_url
    return Engine(s, persist=False)


def series(values, now=10_000.0, step=60):
    n = len(values)
    return [(now - (n - 1 - i) * step, v) for i, v in enumerate(values)]


class DetectionTest(unittest.TestCase):
    rule = {"id": "lat", "service": "payment-api", "kind": "latency",
            "threshold": {"operator": ">", "value": 3}, "anomaly": {"enabled": True}}

    def test_sustained_breach_and_zscore(self):
        vals = [0.8 + (i % 3) * 0.05 for i in range(60)] + [5, 6, 7, 6, 7]
        s = evaluate_series(self.rule, series(vals), 10_000.0, DEFAULTS)
        self.assertTrue(s.threshold_breached)
        self.assertTrue(s.anomalous)
        self.assertGreater(s.zscore, 3)

    def test_single_spike_is_momentary(self):
        vals = [0.8 + (i % 3) * 0.05 for i in range(60)] + [0.8, 0.85, 0.9, 0.8, 9]
        s = evaluate_series(self.rule, series(vals), 10_000.0, DEFAULTS)
        self.assertFalse(s.threshold_breached)
        self.assertFalse(s.anomalous)
        self.assertTrue(s.momentary)
        self.assertFalse(s.abnormal)

    def test_no_data(self):
        s = evaluate_series(self.rule, [], 10_000.0, DEFAULTS)
        self.assertTrue(s.no_data)
        self.assertFalse(s.abnormal)


class ScenarioTest(unittest.TestCase):
    """每個 examples/sample-incidents 情境都要得到預期的 root cause"""

    def test_all_scenarios(self):
        eng = make_engine()
        for name in eng.list_scenarios():
            with self.subTest(scenario=name):
                sc = eng.load_scenario(name)
                incs = [i for i in eng.simulate(sc) if i.status == "incident"]
                exp = sc["expected_root_cause"]
                if exp == "none":
                    self.assertEqual(incs, [], f"{name} 不應產生 incident")
                else:
                    self.assertTrue(incs, f"{name} 應產生 incident")
                    self.assertEqual(incs[0].rca.suspected_component, exp)
                    self.assertGreaterEqual(incs[0].rca.confidence, 0.6)
                    self.assertTrue(incs[0].tool_calls, "應有工具呼叫稽核紀錄")

    def test_provider_timeout_rules_out_proxy_resources(self):
        eng = make_engine()
        inc = eng.simulate(eng.load_scenario("provider-timeout"))[0]
        texts = " ".join(e.description for e in inc.rca.evidence)
        self.assertIn("非 proxy 本身資源不足", texts)
        self.assertEqual(inc.rca.owner, "payment-team")
        self.assertIn({"mariadb", "redis"}, [{r.component for r in inc.rca.ruled_out} & {"mariadb", "redis"}])
        msg = format_message(inc)
        self.assertIn("provider-api", msg)

    def test_maintenance_suppresses(self):
        from datetime import datetime, timedelta, timezone
        eng = make_engine()
        for svc in ("provider-api", "proxy", "payment-api", "payment-job"):
            eng.store.maintenance[svc] = datetime.now(timezone.utc) + timedelta(minutes=30)
        incs = [i for i in eng.simulate(eng.load_scenario("provider-timeout")) if i.status == "incident"]
        self.assertEqual(incs, [])


class _FakeOllama(BaseHTTPRequestHandler):
    reply: dict = {}
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "qwen3:4b", "model": "qwen3:4b"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).calls += 1
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"message": {"content": json.dumps(type(self).reply)}}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class LLMTest(unittest.TestCase):
    def _run(self, reply: dict):
        _FakeOllama.reply, _FakeOllama.calls = reply, 0
        srv = HTTPServer(("127.0.0.1", 0), _FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            eng = make_engine(llm=True, ollama_url=f"http://127.0.0.1:{srv.server_port}")
            return eng.simulate(eng.load_scenario("provider-timeout"))[0]
        finally:
            srv.shutdown()

    def test_llm_result_used(self):
        inc = self._run({"suspected_component": "provider-api", "root_cause": "服務商延遲", "confidence": 0.9,
                         "evidence": [{"type": "metric", "source": "prometheus", "description": "P95 0.8s→42s"}],
                         "ruled_out": [{"component": "redis", "reason": "正常"}],
                         "recommended_actions": ["確認服務商狀態"]})
        self.assertEqual(inc.rca.reasoning_source, "llm")
        self.assertEqual(inc.rca.suspected_component, "provider-api")

    def test_llm_hallucinated_component_falls_back(self):
        inc = self._run({"suspected_component": "kafka", "root_cause": "kafka down", "confidence": 0.99})
        self.assertEqual(inc.rca.reasoning_source, "rule")       # 不存在的服務 → 驗證失敗 → 規則式
        self.assertEqual(_FakeOllama.calls, 2)                    # 有重試一次

    def test_llm_blamed_healthy_component_gets_low_confidence(self):
        inc = self._run({"suspected_component": "redis", "root_cause": "redis", "confidence": 0.95})
        self.assertLessEqual(inc.rca.confidence, 0.5)
        self.assertEqual(inc.rca.confidence_label, "unknown")

    def test_ollama_down_falls_back(self):
        eng = make_engine(llm=True, ollama_url="http://127.0.0.1:9")
        inc = eng.simulate(eng.load_scenario("db-latency"))[0]
        self.assertEqual(inc.rca.reasoning_source, "rule")
        self.assertEqual(inc.rca.suspected_component, "mariadb")


if __name__ == "__main__":
    unittest.main()
