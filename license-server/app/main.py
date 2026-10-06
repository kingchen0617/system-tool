"""授權伺服器 HTTP API（由你部署，客戶的 system-tool 會連到這裡啟用 / 續驗）

  uvicorn app.main:app --host 0.0.0.0 --port 8100

客戶端 API（不需登入，靠 License Key 本身驗證）：
  POST /api/v1/activate     首次啟用與定期續驗
  POST /api/v1/deactivate   停用本機（換主機時）
  GET  /api/v1/public-key   公鑰（方便核對）

管理 API（Header：Authorization: Bearer <ADMIN_TOKEN>）：
  POST /admin/licenses                 簽發
  GET  /admin/licenses                 列表
  GET  /admin/licenses/{id}            詳情 + 啟用紀錄
  POST /admin/licenses/{id}/revoke     撤銷
  GET  /admin/audit                    稽核紀錄

⚠ 一定要放在 HTTPS 後面（例如 Nginx / Cloudflare），並設定強密碼的 ADMIN_TOKEN。
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import tokens
from .core import LicenseError, LicenseService

KEY_PATH = Path(os.getenv("LICENSE_PRIVATE_KEY", "secrets/license_private.pem"))
DB_PATH = Path(os.getenv("LICENSE_DB", "data/licenses.db"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
LEASE_DAYS = int(os.getenv("LEASE_DAYS", "7"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
svc = LicenseService(DB_PATH, tokens.load_private_key(KEY_PATH), lease_days=LEASE_DAYS)
app = FastAPI(title="system-tool License Server", version="0.3.0")

STATUS = {"invalid_license": 400, "bad_request": 400, "unknown_license": 404, "revoked": 403,
          "expired": 403, "too_many_activations": 409}


def _err(e: LicenseError) -> HTTPException:
    return HTTPException(STATUS.get(e.code, 400), {"code": e.code, "message": e.message})


def require_admin(authorization: str = Header(default="")) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN 未設定，管理 API 停用")
    if not hmac.compare_digest(authorization, f"Bearer {ADMIN_TOKEN}"):
        raise HTTPException(401, "unauthorized")


class ActivateReq(BaseModel):
    license_key: str
    instance_id: str = Field(min_length=8, max_length=64)
    fingerprint: str = Field(min_length=8, max_length=128)
    hostname: str = ""
    version: str = ""
    usage: dict[str, Any] = {}


class DeactivateReq(BaseModel):
    license_key: str
    instance_id: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/public-key")
def public_key() -> dict[str, str]:
    return {"public_key": svc.public_key}


@app.post("/api/v1/activate")
def activate(req: ActivateReq) -> dict[str, Any]:
    try:
        return svc.activate(req.license_key, req.instance_id, req.fingerprint, req.hostname[:200],
                            req.version[:50], req.usage)
    except LicenseError as e:
        raise _err(e)


@app.post("/api/v1/deactivate")
def deactivate(req: DeactivateReq) -> dict[str, str]:
    try:
        svc.deactivate(req.license_key, req.instance_id)
    except LicenseError as e:
        raise _err(e)
    return {"status": "ok"}


class IssueReq(BaseModel):
    customer: str
    edition: str
    days: int = 365
    expires_at: Optional[str] = None
    max_services: Optional[int] = None
    max_nodes: Optional[int] = None
    max_activations: Optional[int] = None
    extra_features: list[str] = []
    grace_days: int = 14
    offline: bool = False
    notes: str = ""


@app.post("/admin/licenses", dependencies=[Depends(require_admin)])
def issue(req: IssueReq) -> dict[str, Any]:
    try:
        return svc.issue(req.customer, req.edition, days=req.days, expires_at=req.expires_at,
                         max_services=req.max_services, max_nodes=req.max_nodes,
                         max_activations=req.max_activations, extra_features=req.extra_features,
                         grace_days=req.grace_days, offline=req.offline, notes=req.notes, actor="admin-api")
    except (LicenseError, ValueError) as e:
        raise _err(e) if isinstance(e, LicenseError) else HTTPException(400, str(e))


@app.get("/admin/licenses", dependencies=[Depends(require_admin)])
def list_licenses() -> list[dict[str, Any]]:
    return svc.list_licenses()


@app.get("/admin/licenses/{license_id}", dependencies=[Depends(require_admin)])
def get_license(license_id: str) -> dict[str, Any]:
    try:
        d = svc.get(license_id)
    except LicenseError as e:
        raise _err(e)
    d.pop("token", None)
    return d


class RevokeReq(BaseModel):
    reason: str = ""


@app.post("/admin/licenses/{license_id}/revoke", dependencies=[Depends(require_admin)])
def revoke(license_id: str, req: RevokeReq) -> dict[str, str]:
    try:
        svc.revoke(license_id, req.reason, actor="admin-api")
    except LicenseError as e:
        raise _err(e)
    return {"status": "revoked"}


@app.get("/admin/audit", dependencies=[Depends(require_admin)])
def audit(license_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    return svc.audit_log(license_id, limit)
