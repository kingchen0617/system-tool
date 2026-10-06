"""授權伺服器核心邏輯（與 HTTP 無關，方便測試）。

資料存在 SQLite（licenses / activations / audit）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import tokens
from .editions import ALL_FEATURES, edition_defaults

STALE_ACTIVATION_DAYS = 30   # 超過 30 天沒續驗的啟用，不再佔用名額


class LicenseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
  license_id TEXT PRIMARY KEY,
  customer TEXT NOT NULL,
  edition TEXT NOT NULL,
  token TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  grace_days INTEGER NOT NULL,
  max_activations INTEGER NOT NULL,
  offline INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  revoked_reason TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS activations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  license_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  hostname TEXT,
  version TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  deactivated INTEGER NOT NULL DEFAULT 0,
  usage TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  license_id TEXT,
  detail TEXT
);
"""


class LicenseService:
    def __init__(self, db_path: Path | str, private_key: Ed25519PrivateKey, lease_days: int = 7):
        self.priv = private_key
        self.public_key = tokens.public_key_str(private_key)
        self.lease_days = lease_days
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ helpers
    def _audit(self, actor: str, action: str, license_id: Optional[str], **detail: Any) -> None:
        self.db.execute("INSERT INTO audit (ts, actor, action, license_id, detail) VALUES (?,?,?,?,?)",
                        (iso(now_utc()), actor, action, license_id, json.dumps(detail, ensure_ascii=False)))
        self.db.commit()

    def _next_id(self, edition: str, at: datetime) -> str:
        prefix = f"ST-{edition[:3].upper()}-{at.year}-"
        n = self.db.execute("SELECT COUNT(*) FROM licenses WHERE license_id LIKE ?", (prefix + "%",)).fetchone()[0]
        return f"{prefix}{n + 1:05d}"

    # ------------------------------------------------------------ 簽發 / 管理
    def issue(self, customer: str, edition: str, days: int = 365, expires_at: Optional[str] = None,
              max_services: Optional[int] = None, max_nodes: Optional[int] = None,
              max_activations: Optional[int] = None, features: Optional[list[str]] = None,
              extra_features: Optional[list[str]] = None, grace_days: int = 14,
              offline: bool = False, notes: str = "", actor: str = "admin") -> dict[str, Any]:
        with self.lock:
            d = edition_defaults(edition)
            feats = list(features) if features is not None else d["features"]
            for f in extra_features or []:
                if f not in feats:
                    feats.append(f)
            unknown = set(feats) - set(ALL_FEATURES)
            if unknown:
                raise LicenseError("bad_request", f"未知功能：{sorted(unknown)}")
            if offline and "offline_license" not in feats:
                raise LicenseError("bad_request", "離線授權需要 offline_license 功能（Enterprise，或用 --extra-feature 加上）")
            issued = now_utc()
            exp = parse(expires_at) if expires_at else issued + timedelta(days=days)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            lid = self._next_id(edition, issued)
            payload = {
                "v": 1, "type": "license", "license_id": lid, "customer": customer, "edition": edition,
                "issued_at": iso(issued), "expires_at": iso(exp), "grace_days": grace_days,
                "max_services": d["max_services"] if max_services is None else max_services,
                "max_nodes": d["max_nodes"] if max_nodes is None else max_nodes,
                "max_activations": d["max_activations"] if max_activations is None else max_activations,
                "features": sorted(feats), "offline": bool(offline),
            }
            token = tokens.sign(payload, self.priv)
            self.db.execute(
                "INSERT INTO licenses (license_id, customer, edition, token, issued_at, expires_at, grace_days,"
                " max_activations, offline, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lid, customer, edition, token, payload["issued_at"], payload["expires_at"], grace_days,
                 payload["max_activations"], int(offline), notes))
            self.db.commit()
            self._audit(actor, "issue", lid, customer=customer, edition=edition, expires_at=payload["expires_at"],
                        offline=offline)
            return {"license_id": lid, "license_key": token, "payload": payload}

    def revoke(self, license_id: str, reason: str = "", actor: str = "admin") -> None:
        with self.lock:
            cur = self.db.execute("UPDATE licenses SET status='revoked', revoked_reason=? WHERE license_id=?",
                                  (reason, license_id))
            self.db.commit()
            if cur.rowcount == 0:
                raise LicenseError("unknown_license", f"找不到授權 {license_id}")
            self._audit(actor, "revoke", license_id, reason=reason)

    def list_licenses(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM licenses ORDER BY issued_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("token")
            d["active_activations"] = len(self._active_activations(r["license_id"]))
            out.append(d)
        return out

    def get(self, license_id: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM licenses WHERE license_id=?", (license_id,)).fetchone()
        if not r:
            raise LicenseError("unknown_license", f"找不到授權 {license_id}")
        acts = [dict(a) for a in self.db.execute(
            "SELECT * FROM activations WHERE license_id=? ORDER BY last_seen DESC", (license_id,)).fetchall()]
        return {**dict(r), "activations": acts}

    def audit_log(self, license_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        if license_id:
            rows = self.db.execute("SELECT * FROM audit WHERE license_id=? ORDER BY id DESC LIMIT ?",
                                   (license_id, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 客戶端呼叫
    def _active_activations(self, license_id: str) -> list[sqlite3.Row]:
        cutoff = iso(now_utc() - timedelta(days=STALE_ACTIVATION_DAYS))
        return self.db.execute(
            "SELECT * FROM activations WHERE license_id=? AND deactivated=0 AND last_seen>=?",
            (license_id, cutoff)).fetchall()

    def _check_license(self, license_key: str) -> sqlite3.Row:
        try:
            payload = tokens.verify(license_key, [self.public_key])
        except ValueError as e:
            raise LicenseError("invalid_license", f"License Key 無效：{e}")
        if payload.get("type") != "license":
            raise LicenseError("invalid_license", "不是 License Key")
        row = self.db.execute("SELECT * FROM licenses WHERE license_id=?", (payload["license_id"],)).fetchone()
        if not row or row["token"] != license_key.strip():
            raise LicenseError("unknown_license", "授權伺服器上找不到這組授權")
        if row["status"] == "revoked":
            raise LicenseError("revoked", f"授權已被撤銷：{row['revoked_reason'] or ''}")
        if now_utc() > parse(row["expires_at"]) + timedelta(days=row["grace_days"]):
            raise LicenseError("expired", f"授權已於 {row['expires_at']} 到期")
        return row

    def activate(self, license_key: str, instance_id: str, fingerprint: str, hostname: str = "",
                 version: str = "", usage: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """首次啟用與定期續驗都呼叫這裡；同一個 instance 重複呼叫只會更新 last_seen。"""
        with self.lock:
            try:
                row = self._check_license(license_key)
            except LicenseError as e:
                self._audit(instance_id, "activate_denied", None, code=e.code, message=e.message)
                raise
            lid = row["license_id"]
            now = iso(now_utc())
            usage_json = json.dumps(usage or {}, ensure_ascii=False)
            existing = self.db.execute(
                "SELECT * FROM activations WHERE license_id=? AND instance_id=? AND fingerprint=? AND deactivated=0",
                (lid, instance_id, fingerprint)).fetchone()
            if existing:
                self.db.execute("UPDATE activations SET last_seen=?, hostname=?, version=?, usage=? WHERE id=?",
                                (now, hostname, version, usage_json, existing["id"]))
                action = "heartbeat"
            else:
                if len(self._active_activations(lid)) >= row["max_activations"]:
                    self._audit(instance_id, "activate_denied", lid, code="too_many_activations")
                    raise LicenseError("too_many_activations",
                                       f"已達啟用台數上限（{row['max_activations']}），請先在舊主機停用或聯絡供應商")
                self.db.execute(
                    "INSERT INTO activations (license_id, instance_id, fingerprint, hostname, version, first_seen,"
                    " last_seen, usage) VALUES (?,?,?,?,?,?,?,?)",
                    (lid, instance_id, fingerprint, hostname, version, now, now, usage_json))
                action = "activate"
            self.db.commit()
            self._audit(instance_id, action, lid, hostname=hostname, version=version, usage=usage or {})

            lease_end = min(now_utc() + timedelta(days=self.lease_days), parse(row["expires_at"]))
            act = {"v": 1, "type": "activation", "license_id": lid, "instance_id": instance_id,
                   "fingerprint": fingerprint, "issued_at": now, "lease_expires_at": iso(lease_end)}
            return {"status": "ok", "license_id": lid, "activation_token": tokens.sign(act, self.priv),
                    "lease_expires_at": act["lease_expires_at"], "server_time": now}

    def deactivate(self, license_key: str, instance_id: str) -> None:
        with self.lock:
            row = self._check_license(license_key)
            self.db.execute("UPDATE activations SET deactivated=1 WHERE license_id=? AND instance_id=?",
                            (row["license_id"], instance_id))
            self.db.commit()
            self._audit(instance_id, "deactivate", row["license_id"])
