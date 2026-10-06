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
    """RCA 測試一律用「全功能」授權狀態，授權本身另外在 test_licensing.py 測"""
    from app.licensing import FEATURE_NAMES, LicenseState
    s = Settings()
    s.llm_enabled = llm
    if ollama_url:
        s.ollama_url = ollama_url
    full = LicenseState(status="valid", reason="test", edition="enterprise", features=sorted(FEATURE_NAMES),
                        max_services=0, max_nodes=0)
    return Engine(s, persist=False, license_state=full)


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

    def test_baseline_not_polluted_by_long_incident(self):
        # 事故已持續 15 分鐘：guard band 讓 baseline 仍是正常值，z-score 不會被拉低
        vals = [0.8 + (i % 3) * 0.05 for i in range(60)] + [5.0] * 15
        s = evaluate_series(self.rule, series(vals), 10_000.0, DEFAULTS | {"guard": 600})
        self.assertLess(s.baseline_mean, 1.0)
        self.assertTrue(s.anomalous)

    def test_robust_baseline_ignores_history_spikes(self):
        from app.detection import robust_baseline
        med, sd = robust_baseline([1.0] * 50 + [100.0] * 3)
        self.assertEqual(med, 1.0)
        self.assertLess(sd, 1.0)

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
                    continue
                self.assertTrue(incs, f"{name} 應產生 incident")
                rca = incs[0].rca
                self.assertIn(rca.suspected_component, sc.get("acceptable_root_causes") or [exp])
                if "max_confidence" in sc:  # 模稜兩可的情境：信心度不可過高
                    self.assertLessEqual(rca.confidence, sc["max_confidence"])
                else:
                    self.assertGreaterEqual(rca.confidence, 0.6)
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


class ScoringTest(unittest.TestCase):
    def _sig(self, **kw):
        from app.models import Signal
        base = dict(rule_id="r", service="payment-api", kind="errors", severity="critical", current=0.06,
                    threshold=0.05, operator=">", threshold_breached=True, anomalous=True)
        return Signal(**(base | kw))

    def test_single_mild_signal_is_warning_only(self):
        from app.detection import DEFAULT_SCORING, score_group
        score, b = score_group({"payment-api"}, [self._sig()], [], set(), DEFAULT_SCORING)
        self.assertEqual(score, 3)          # 門檻 +2、z +1；不再因 kind=errors 額外加分
        self.assertNotIn("error_signal", b)

    def test_single_severe_signal_is_incident(self):
        from app.detection import DEFAULT_SCORING, score_group
        score, b = score_group({"payment-api"}, [self._sig(current=0.4)], [], set(), DEFAULT_SCORING)
        self.assertGreaterEqual(score, 5)
        self.assertIn("severe_breach", b)


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
            srv.server_close()

    def test_llm_result_used(self):
        inc = self._run({"suspected_component": "provider-api", "root_cause": "服務商延遲", "confidence": 0.9,
                         "evidence_ids": ["sig_provider-latency", "sig_proxy-cpu", "log_proxy_0"],
                         "ruled_out": ["redis", "not-a-service"],
                         "recommended_actions": ["確認服務商狀態"]})
        self.assertEqual(inc.rca.reasoning_source, "llm")
        self.assertEqual(inc.rca.suspected_component, "provider-api")
        self.assertEqual(inc.rca.confidence, 0.9)
        # 證據內容由 Python 從 context 組裝，而不是 LLM 自己寫
        self.assertTrue(inc.rca.evidence[0].description.startswith("provider-api provider-latency"))
        self.assertEqual([r.component for r in inc.rca.ruled_out], ["redis"])

    def test_llm_invented_evidence_id_falls_back(self):
        inc = self._run({"suspected_component": "provider-api", "root_cause": "x", "confidence": 0.9,
                         "evidence_ids": ["sig_made_up_metric"]})
        self.assertEqual(inc.rca.reasoning_source, "rule")
        self.assertEqual(_FakeOllama.calls, 2)

    def test_llm_cannot_rule_out_abnormal_component(self):
        inc = self._run({"suspected_component": "provider-api", "root_cause": "x", "confidence": 0.9,
                         "evidence_ids": ["sig_provider-latency"], "ruled_out": ["proxy"]})
        self.assertEqual(inc.rca.reasoning_source, "rule")

    def test_llm_hallucinated_component_falls_back(self):
        inc = self._run({"suspected_component": "kafka", "root_cause": "kafka down", "confidence": 0.99,
                         "evidence_ids": ["sig_provider-latency"]})
        self.assertEqual(inc.rca.reasoning_source, "rule")       # 不存在的服務 → 驗證失敗 → 規則式
        self.assertEqual(_FakeOllama.calls, 2)                    # 有重試一次

    def test_llm_blamed_healthy_component_gets_low_confidence(self):
        inc = self._run({"suspected_component": "redis", "root_cause": "redis", "confidence": 0.95,
                         "evidence_ids": ["sig_redis-latency"]})
        self.assertLessEqual(inc.rca.confidence, 0.5)
        self.assertEqual(inc.rca.confidence_label, "unknown")

    def test_ollama_down_falls_back(self):
        eng = make_engine(llm=True, ollama_url="http://127.0.0.1:9")
        inc = eng.simulate(eng.load_scenario("db-latency"))[0]
        self.assertEqual(inc.rca.reasoning_source, "rule")
        self.assertEqual(inc.rca.suspected_component, "mariadb")


if __name__ == "__main__":
    unittest.main()
