"""授權用戶端（License Client）：驗證 License Key、線上啟用 / 續驗、計算目前可用的方案與功能。

運作方式（地端安裝 + 線上授權）：
  1. 客戶設定 License Key（環境變數 LICENSE_KEY 或 data/license.key）
  2. 啟動時向授權伺服器 POST /api/v1/activate，取得綁定本機的 activation token（有租期，預設 7 天）
  3. 之後每 LICENSE_REFRESH_HOURS（預設 12 小時）續驗一次，取得新的租期
  4. 連不上授權伺服器時：租期內照常運作；租期過後進入寬限期（grace_days，預設 14 天）並發出警告；
     寬限期也過了 → 降級為 Community 模式（不會停止監控，但只剩基本功能）
  5. Enterprise 可簽發「離線授權」（offline=true），完全不需連線，只驗證簽章與到期日

所有 token 都用授權方的 Ed25519 公鑰驗證（app/license_keys.py），本程式不含任何私鑰。
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import socket
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel

from . import __version__
from .license_keys import VENDOR_PUBLIC_KEYS

# 無有效授權時的降級方案（與 license-server/app/editions.py 的 community 相同）
COMMUNITY = {"edition": "community", "max_services": 5, "max_nodes": 5, "features": ["rule_rca"]}

CLOCK_TOLERANCE = timedelta(hours=24)
FEATURE_NAMES = {
    "rule_rca": "規則式 RCA", "ai_rca": "AI RCA", "notifications": "Slack/LINE/Email 通知",
    "change_tracking": "變更關聯", "maintenance": "維護時段", "feedback_dataset": "RCA 回饋資料集",
    "history_rag": "歷史事件檢索", "cloud_llm": "雲端 LLM", "aws_connector": "AWS Connector",
    "sso": "SSO", "ha": "高可用", "offline_license": "離線授權",
}


class LicenseState(BaseModel):
    status: str = "unlicensed"   # valid | grace | expired | revoked | invalid | unactivated | unlicensed
    reason: str = "尚未設定 License Key"
    mode: str = "online"          # online | offline
    license_id: Optional[str] = None
    customer: Optional[str] = None
    licensed_edition: Optional[str] = None
    edition: str = "community"    # 目前實際生效的方案
    features: list[str] = COMMUNITY["features"]
    max_services: int = COMMUNITY["max_services"]
    max_nodes: int = COMMUNITY["max_nodes"]
    expires_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    grace_until: Optional[str] = None
    instance_id: str = ""
    last_check: Optional[str] = None
    last_error: Optional[str] = None
    warnings: list[str] = []

    @property
    def active(self) -> bool:
        return self.status in ("valid", "grace")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token: str, public_keys: list[str]) -> dict[str, Any]:
    try:
        prefix, body, sig = token.strip().split(".")
    except ValueError:
        raise ValueError("格式錯誤")
    if prefix != "ST1":
        raise ValueError("不支援的版本")
    for pk in public_keys:
        try:
            Ed25519PublicKey.from_public_bytes(_b64d(pk)).verify(_b64d(sig), f"{prefix}.{body}".encode())
            return json.loads(_b64d(body))
        except (InvalidSignature, ValueError):
            continue
    raise ValueError("簽章驗證失敗")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _machine_id() -> str:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = Path(p).read_text().strip()
            if v:
                return v
        except OSError:
            pass
    return f"{platform.node()}-{uuid.getnode():x}"


class LicenseManager:
    def __init__(self, data_dir: Path, license_key: str = "", license_file: Optional[Path] = None,
                 server_url: str = "", public_keys: Optional[list[str]] = None,
                 telemetry_opt_in: bool = False, timeout: float = 10.0, persist: bool = True):
        self.data_dir = Path(data_dir)
        self.persist = persist
        if persist:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self._env_key = license_key.strip()
        self.license_file = Path(license_file) if license_file else self.data_dir / "license.key"
        self.server_url = server_url.rstrip("/")
        self.public_keys = list(VENDOR_PUBLIC_KEYS if public_keys is None else public_keys)
        self.telemetry_opt_in = telemetry_opt_in
        self.timeout = timeout
        self.lock = threading.RLock()
        self.state_path = self.data_dir / "license-state.json"
        self.audit_path = self.data_dir / "license-audit.jsonl"
        self._mem_state: dict[str, Any] = {}
        self.instance_id = self._load_instance_id()
        self.fingerprint = hashlib.sha256(f"{_machine_id()}|{self.instance_id}".encode()).hexdigest()

    # ------------------------------------------------------------ 本機資料
    def _load_instance_id(self) -> str:
        p = self.data_dir / "instance_id"
        if self.persist and p.exists():
            return p.read_text().strip()
        iid = uuid.uuid4().hex
        if self.persist:
            p.write_text(iid)
        return iid

    def _state(self) -> dict[str, Any]:
        if not self.persist:
            return self._mem_state
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict[str, Any]) -> None:
        if not self.persist:
            self._mem_state = st
            return
        self.state_path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    def audit(self, event: str, **detail: Any) -> None:
        if not self.persist:
            return
        line = {"ts": _iso(datetime.now(timezone.utc)), "event": event, **detail}
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def license_key(self) -> str:
        if self._env_key:
            return self._env_key
        try:
            return self.license_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set_license_key(self, key: str) -> None:
        """由 API / CLI 設定新的 License Key（會清除舊的啟用資料）"""
        verify_token(key, self.public_keys)  # 先確認簽章正確
        with self.lock:
            self._env_key = ""
            if self.persist:
                self.license_file.write_text(key.strip() + "\n", encoding="utf-8")
            else:
                self._env_key = key.strip()
            st = self._state()
            st.pop("activation_token", None)
            st.pop("server_status", None)
            self._save_state(st)
            self.audit("license_key_set")

    # ------------------------------------------------------------ 線上啟用 / 續驗
    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(f"{self.server_url}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode() or "{}")
            except ValueError:
                data = {}
            return e.code, data

    def refresh(self, usage: Optional[dict[str, Any]] = None) -> LicenseState:
        """向授權伺服器啟用 / 續驗。離線授權或沒有 License Key 時不會連線。"""
        with self.lock:
            key = self.license_key()
            st = self._state()
            if not key:
                return self.evaluate()
            try:
                lic = verify_token(key, self.public_keys)
            except ValueError:
                return self.evaluate()
            if lic.get("offline"):
                return self.evaluate()
            if not self.server_url:
                st["last_error"] = "LICENSE_SERVER_URL 未設定"
                self._save_state(st)
                return self.evaluate()
            body = {"license_key": key, "instance_id": self.instance_id, "fingerprint": self.fingerprint,
                    "hostname": socket.gethostname()[:200], "version": __version__,
                    "usage": usage if (self.telemetry_opt_in and usage) else {}}
            st["last_check"] = _iso(datetime.now(timezone.utc))
            try:
                code, data = self._post("/api/v1/activate", body)
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                st["last_error"] = f"無法連線授權伺服器：{e}"
                self._save_state(st)
                self.audit("refresh_unreachable", error=str(e))
                return self.evaluate()
            if code == 200 and data.get("activation_token"):
                st["activation_token"] = data["activation_token"]
                st["server_status"] = "ok"
                st["last_error"] = None
                self.audit("refresh_ok", lease_expires_at=data.get("lease_expires_at"))
            else:
                detail = data.get("detail", data) if isinstance(data, dict) else {}
                err_code = (detail or {}).get("code", f"http_{code}")
                st["last_error"] = (detail or {}).get("message", f"HTTP {code}")
                # 伺服器明確拒絕 → 立即生效（不再使用舊的租期）
                if err_code in ("revoked", "unknown_license", "expired", "invalid_license"):
                    st["server_status"] = err_code
                    st.pop("activation_token", None)
                elif err_code == "too_many_activations":
                    st["server_status"] = err_code
                self.audit("refresh_denied", code=err_code, message=st["last_error"])
            self._save_state(st)
            return self.evaluate()

    def deactivate(self) -> bool:
        key = self.license_key()
        if not key or not self.server_url:
            return False
        code, _ = self._post("/api/v1/deactivate", {"license_key": key, "instance_id": self.instance_id})
        if code == 200:
            st = self._state()
            st.pop("activation_token", None)
            self._save_state(st)
            self.audit("deactivated")
        return code == 200

    # ------------------------------------------------------------ 狀態計算
    def evaluate(self, now: Optional[datetime] = None) -> LicenseState:
        now = now or datetime.now(timezone.utc)
        st = self._state()
        base = LicenseState(instance_id=self.instance_id, last_check=st.get("last_check"),
                            last_error=st.get("last_error"))

        def downgrade(status: str, reason: str, **kw: Any) -> LicenseState:
            return base.model_copy(update={"status": status, "reason": reason, **kw})

        key = self.license_key()
        if not key:
            return base
        if not self.public_keys:
            return downgrade("invalid", "此版本未設定授權方公鑰（app/license_keys.py）")
        try:
            lic = verify_token(key, self.public_keys)
        except ValueError as e:
            return downgrade("invalid", f"License Key 無效：{e}")
        if lic.get("type") != "license":
            return downgrade("invalid", "不是 License Key")

        info = dict(license_id=lic.get("license_id"), customer=lic.get("customer"),
                    licensed_edition=lic.get("edition"), expires_at=lic.get("expires_at"),
                    mode="offline" if lic.get("offline") else "online")

        # 防止把系統時間往回調來延長授權
        max_seen = st.get("max_seen_time")
        if max_seen and now < _parse(max_seen) - CLOCK_TOLERANCE:
            return downgrade("invalid", "偵測到系統時間被往回調整，授權暫停", **info)
        if not max_seen or now > _parse(max_seen) + timedelta(hours=1):
            st["max_seen_time"] = _iso(now)
            self._save_state(st)

        if lic.get("offline") and "offline_license" not in lic.get("features", []):
            return downgrade("invalid", "此授權不允許離線使用", **info)

        grace = timedelta(days=int(lic.get("grace_days", 14)))
        expires = _parse(lic["expires_at"])
        warnings: list[str] = []
        ends = [("license", expires)]

        if not lic.get("offline"):
            srv = st.get("server_status")
            if srv == "revoked":
                return downgrade("revoked", f"授權已被撤銷：{st.get('last_error') or ''}", **info)
            if srv in ("unknown_license", "invalid_license"):
                return downgrade("invalid", f"授權伺服器拒絕：{st.get('last_error') or ''}", **info)
            if srv == "expired":
                return downgrade("expired", "授權伺服器回報授權已到期，請續約", **info)
            tok = st.get("activation_token")
            if not tok:
                msg = "尚未完成線上啟用"
                if srv == "too_many_activations":
                    msg = f"啟用台數已滿：{st.get('last_error') or ''}"
                elif st.get("last_error"):
                    msg += f"（{st['last_error']}）"
                return downgrade("unactivated", msg, **info)
            try:
                act = verify_token(tok, self.public_keys)
            except ValueError:
                return downgrade("invalid", "啟用資料簽章錯誤", **info)
            if (act.get("type") != "activation" or act.get("license_id") != lic.get("license_id")
                    or act.get("instance_id") != self.instance_id or act.get("fingerprint") != self.fingerprint):
                return downgrade("invalid", "啟用資料不屬於這台主機（請重新啟用）", **info)
            lease = _parse(act["lease_expires_at"])
            info["lease_expires_at"] = act["lease_expires_at"]
            ends.append(("lease", lease))

        # 依「最早結束」的期限判斷：正常 → 寬限 → 到期
        kind, end = min(ends, key=lambda x: x[1])
        grace_until = end + grace
        info["grace_until"] = _iso(grace_until)
        if now > grace_until:
            why = "授權已到期，請續約" if kind == "license" else "超過寬限期仍無法連線授權伺服器"
            return downgrade("expired", why, **info)
        if now > end:
            why = (f"授權已到期，寬限期至 {_iso(grace_until)}，請盡快續約" if kind == "license"
                   else f"無法連線授權伺服器，寬限期至 {_iso(grace_until)}")
            status, reason = "grace", why
            warnings.append(why)
        else:
            status, reason = "valid", "授權有效"
            if expires - now < timedelta(days=30):
                warnings.append(f"授權將於 {lic['expires_at']} 到期")

        return base.model_copy(update={
            "status": status, "reason": reason, "edition": lic.get("edition", "community"),
            "features": sorted(set(lic.get("features", [])) | {"rule_rca"}),
            "max_services": int(lic.get("max_services", 0)), "max_nodes": int(lic.get("max_nodes", 0)),
            "warnings": warnings, **info})
