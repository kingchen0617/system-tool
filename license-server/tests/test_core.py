"""授權伺服器核心測試：cd license-server && python -m pytest -q"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tokens  # noqa: E402
from app.core import LicenseError, LicenseService  # noqa: E402


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        priv, self.pub = tokens.generate_keypair()
        self.svc = LicenseService(Path(self.tmp.name) / "l.db", priv)

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def test_issue_and_verify(self):
        res = self.svc.issue("ABC", "business", days=30, extra_features=["aws_connector"])
        p = tokens.verify(res["license_key"], [self.pub])
        self.assertEqual(p["license_id"], "ST-BUS-%s-00001" % p["issued_at"][:4])
        self.assertIn("aws_connector", p["features"])
        self.assertEqual(p["max_services"], 200)
        self.assertEqual(len(self.svc.list_licenses()), 1)

    def test_tampered_key_rejected(self):
        key = self.svc.issue("ABC", "professional")["license_key"]
        prefix, body, sig = key.split(".")
        payload = tokens.peek(key) | {"edition": "enterprise"}
        forged_body = tokens.sign(payload, tokens.generate_keypair()[0]).split(".")[1]
        with self.assertRaises(LicenseError) as cm:
            self.svc.activate(f"{prefix}.{forged_body}.{sig}", "i" * 16, "f" * 16)
        self.assertEqual(cm.exception.code, "invalid_license")

    def test_activation_limit_and_audit(self):
        key = self.svc.issue("ABC", "professional", max_activations=1)["license_key"]
        r = self.svc.activate(key, "inst-0001", "fp-00000001")
        act = tokens.verify(r["activation_token"], [self.pub])
        self.assertEqual(act["type"], "activation")
        self.svc.activate(key, "inst-0001", "fp-00000001")            # 續驗不佔名額
        with self.assertRaises(LicenseError) as cm:
            self.svc.activate(key, "inst-0002", "fp-00000002")
        self.assertEqual(cm.exception.code, "too_many_activations")
        actions = [a["action"] for a in self.svc.audit_log()]
        self.assertIn("activate", actions)
        self.assertIn("heartbeat", actions)
        self.assertIn("activate_denied", actions)

    def test_revoke(self):
        res = self.svc.issue("ABC", "professional")
        self.svc.revoke(res["license_id"], "test")
        with self.assertRaises(LicenseError) as cm:
            self.svc.activate(res["license_key"], "inst-0001", "fp-00000001")
        self.assertEqual(cm.exception.code, "revoked")

    def test_unknown_edition(self):
        with self.assertRaises(ValueError):
            self.svc.issue("ABC", "gold")


if __name__ == "__main__":
    unittest.main()
