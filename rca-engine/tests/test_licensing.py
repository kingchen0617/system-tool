"""授權機制端對端測試：真的啟動一個（以 license-server 核心邏輯實作的）授權伺服器，讓用戶端去啟用 / 續驗。

license-server 目錄不存在時（例如出貨給客戶的版本）自動略過。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT.parent / "license-server"
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.licensing import LicenseManager, _parse  # noqa: E402

HAS_SERVER = (SERVER_DIR / "app" / "core.py").exists()
if HAS_SERVER:
    # license-server 的模組用相對匯入；用獨立的套件名稱 "lsapp" 載入，避免和 rca-engine 的 app 衝突
    import importlib
    import types
    pkg = types.ModuleType("lsapp")
    pkg.__path__ = [str(SERVER_DIR / "app")]
    sys.modules["lsapp"] = pkg
    ls_tokens = importlib.import_module("lsapp.tokens")
    ls_core = importlib.import_module("lsapp.core")


def make_server(service):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                if self.path == "/api/v1/activate":
                    res = service.activate(body["license_key"], body["instance_id"], body["fingerprint"],
                                           body.get("hostname", ""), body.get("version", ""), body.get("usage"))
                else:
                    service.deactivate(body["license_key"], body["instance_id"])
                    res = {"status": "ok"}
                code = 200
            except ls_core.LicenseError as e:
                code = {"revoked": 403, "expired": 403, "too_many_activations": 409}.get(e.code, 400)
                res = {"detail": {"code": e.code, "message": e.message}}
            out = json.dumps(res).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(HAS_SERVER, "license-server 不在此版本中")
class LicensingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        priv, self.pub = ls_tokens.generate_keypair()
        self.svc = ls_core.LicenseService(self.tmp / "licenses.db", priv, lease_days=7)
        self.srv = make_server(self.svc)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, name="node1", key="", url=None) -> LicenseManager:
        return LicenseManager(self.tmp / name, license_key=key, server_url=self.url if url is None else url,
                              public_keys=[self.pub])

    def engine(self, lm: LicenseManager) -> Engine:
        s = Settings()
        s.llm_enabled = True
        return Engine(s, persist=False, license_manager=lm)

    # ------------------------------------------------------------ 基本狀態
    def test_no_key_runs_as_community(self):
        lm = self.client()
        eng = self.engine(lm)
        self.assertEqual(eng.license.status, "unlicensed")
        self.assertEqual(eng.license.edition, "community")
        self.assertEqual(len(eng.monitored_services), 5)        # 7 個有規則的服務，只監控 5 個
        self.assertEqual(len(eng.unmonitored_by_license), 2)
        self.assertIn("mariadb", eng.monitored_services)        # 依 criticality / 拓撲順序挑選
        self.assertFalse(eng.has_feature("ai_rca"))
        self.assertEqual(eng.notifier.allowed_channels, {"console"})
        # 模擬情境（試用 / 驗收）不受服務數上限影響
        inc = [i for i in eng.simulate(eng.load_scenario("provider-timeout")) if i.status == "incident"][0]
        self.assertEqual(inc.rca.suspected_component, "provider-api")
        self.assertEqual(inc.rca.reasoning_source, "rule")       # 無 ai_rca 授權 → 不呼叫 LLM

    def test_forged_key_is_invalid(self):
        other_priv, _ = ls_tokens.generate_keypair()
        fake = ls_tokens.sign({"type": "license", "license_id": "X", "edition": "enterprise",
                               "expires_at": "2099-01-01T00:00:00Z", "features": ["ai_rca"]}, other_priv)
        lm = self.client(key=fake)
        st = lm.evaluate()
        self.assertEqual(st.status, "invalid")
        self.assertEqual(st.edition, "community")

    # ------------------------------------------------------------ 線上啟用
    def test_online_activation_and_entitlements(self):
        key = self.svc.issue("ABC Corp", "professional", days=365)["license_key"]
        lm = self.client(key=key)
        self.assertEqual(lm.evaluate().status, "unactivated")
        eng = self.engine(lm)
        st = eng.refresh_license()
        self.assertEqual(st.status, "valid", st.reason)
        self.assertEqual(st.edition, "professional")
        self.assertTrue(eng.has_feature("ai_rca"))
        self.assertIsNone(eng.notifier.allowed_channels)
        self.assertEqual(eng.unmonitored_by_license, [])
        self.assertEqual(len(self.svc.get(st.license_id)["activations"]), 1)
        # 再續驗一次不會多佔名額
        lm.refresh()
        self.assertEqual(len(self.svc.get(st.license_id)["activations"]), 1)

    def test_offline_grace_then_expired(self):
        key = self.svc.issue("ABC", "professional", days=365, grace_days=14)["license_key"]
        lm = self.client(key=key)
        lm.refresh()
        lease = _parse(lm.evaluate().lease_expires_at)
        # 授權伺服器連不上：租期內仍有效
        lm.server_url = "http://127.0.0.1:9"
        self.assertEqual(lm.refresh().status, "valid")
        self.assertIn("無法連線", lm.evaluate().last_error)
        self.assertEqual(lm.evaluate(now=lease + timedelta(days=1)).status, "grace")
        st = lm.evaluate(now=lease + timedelta(days=15))
        self.assertEqual(st.status, "expired")
        self.assertEqual(st.edition, "community")

    def test_revoked_takes_effect_on_next_refresh(self):
        res = self.svc.issue("ABC", "business", days=365)
        lm = self.client(key=res["license_key"])
        self.assertEqual(lm.refresh().status, "valid")
        self.svc.revoke(res["license_id"], "未付款")
        st = lm.refresh()
        self.assertEqual(st.status, "revoked")
        self.assertEqual(st.edition, "community")

    def test_activation_limit_and_deactivate(self):
        key = self.svc.issue("ABC", "professional", max_activations=1)["license_key"]
        a, b = self.client("a", key), self.client("b", key)
        self.assertEqual(a.refresh().status, "valid")
        st = b.refresh()
        self.assertEqual(st.status, "unactivated")
        self.assertIn("啟用台數", st.reason)
        self.assertTrue(a.deactivate())                        # 舊主機停用後，新主機就能啟用
        self.assertEqual(b.refresh().status, "valid")

    def test_activation_copied_to_other_machine_is_invalid(self):
        key = self.svc.issue("ABC", "professional")["license_key"]
        a = self.client("a", key)
        a.refresh()
        b = self.client("b", key)
        shutil.copy(a.state_path, b.state_path)               # 把 a 的啟用資料複製給 b
        self.assertEqual(b.evaluate().status, "invalid")

    def test_clock_rollback_detected(self):
        key = self.svc.issue("ABC", "professional")["license_key"]
        lm = self.client(key=key)
        lm.refresh()
        now = datetime.now(timezone.utc)
        self.assertEqual(lm.evaluate(now=now).status, "valid")
        self.assertEqual(lm.evaluate(now=now - timedelta(days=3)).status, "invalid")

    def test_license_expiry_grace(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        key = self.svc.issue("ABC", "professional", expires_at=past, grace_days=14)["license_key"]
        lm = self.client(key=key)
        st = lm.refresh()
        self.assertEqual(st.status, "grace", st.reason)
        self.assertIn("續約", st.reason)
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        key2 = self.svc.issue("ABC", "professional", expires_at=old, grace_days=14)["license_key"]
        lm2 = self.client("n2", key=key2)
        self.assertEqual(lm2.refresh().status, "expired")

    # ------------------------------------------------------------ 離線授權
    def test_offline_license_needs_no_server(self):
        key = self.svc.issue("銀行A", "enterprise", days=365, offline=True)["license_key"]
        lm = self.client(key=key, url="")
        st = lm.refresh()
        self.assertEqual(st.status, "valid", st.reason)
        self.assertEqual(st.mode, "offline")
        self.assertEqual(st.max_services, 0)

    def test_offline_requires_feature(self):
        with self.assertRaises(ls_core.LicenseError):
            self.svc.issue("ABC", "professional", offline=True)

    def test_set_license_key_rejects_garbage(self):
        lm = self.client()
        with self.assertRaises(ValueError):
            lm.set_license_key("ST1.abc.def")


if __name__ == "__main__":
    unittest.main()
