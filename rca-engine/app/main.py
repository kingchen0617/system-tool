"""FastAPI 入口：uvicorn app.main:app --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import __version__
from .config import settings
from .engine import Engine
from .licensing import FEATURE_NAMES
from .models import ChangeEvent, Incident

engine = Engine(settings)
_stop = threading.Event()


def _loop() -> None:
    while not _stop.is_set():
        try:
            incs = engine.run_cycle()
            if incs:
                print(f"[detect] {len(incs)} incident/warning(s): {[i.id for i in incs]}")
        except Exception as e:
            print(f"[detect] cycle failed: {e}")
        _stop.wait(settings.detection_interval)


def _license_loop() -> None:
    """啟動時立即啟用 / 續驗，之後每 LICENSE_REFRESH_HOURS 小時一次；失敗時 30 分鐘後重試。"""
    while not _stop.is_set():
        try:
            st = engine.refresh_license()
            print(f"[license] {st.status}：{st.reason}（生效方案 {st.edition}）")
            wait = settings.license_refresh_hours * 3600 if st.last_error is None else 1800
        except Exception as e:
            print(f"[license] refresh failed: {e}")
            wait = 1800
        _stop.wait(wait)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_license_loop, daemon=True, name="license").start()
    if settings.detection_enabled:
        threading.Thread(target=_loop, daemon=True, name="detector").start()
    yield
    _stop.set()


app = FastAPI(title="system-tool · AI SRE RCA Engine", version=__version__, lifespan=lifespan)


def require_feature(feature: str):
    def dep() -> None:
        if not engine.has_feature(feature):
            raise HTTPException(402, {"code": "feature_not_licensed", "feature": feature,
                                      "message": f"目前方案（{engine.license.edition}）不含「{FEATURE_NAMES.get(feature, feature)}」功能"})
    return dep


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(401, "需要 X-Admin-Token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "llm_enabled": settings.llm_enabled,
        "llm_available": engine.llm.healthy() if engine.llm else False,
        "model": settings.ollama_model if engine.llm else None,
        "rules": len(engine.rules.rules),
        "services": len(engine.topo.services),
        "last_detection": engine.last_run,
        "version": __version__,
        "license": {"status": engine.license.status, "edition": engine.license.edition,
                    "reason": engine.license.reason, "warnings": engine.license.warnings},
    }


@app.get("/services")
def services() -> dict[str, Any]:
    status: dict[str, str] = {}
    for s in engine.last_signals:
        if s.no_data:
            status.setdefault(s.service, "no_data")
        elif s.abnormal:
            status[s.service] = "abnormal"
        else:
            status.setdefault(s.service, "ok")
            if status[s.service] == "no_data":
                status[s.service] = "ok"
    return {sid: {**svc, "status": status.get(sid, "unknown")} for sid, svc in engine.topo.services.items()}


@app.get("/signals")
def signals() -> list[dict[str, Any]]:
    return [s.model_dump() | {"abnormal": s.abnormal} for s in engine.last_signals]


@app.get("/incidents")
def incidents(limit: int = 50) -> list[dict[str, Any]]:
    return [
        {"id": i.id, "status": i.status, "severity": i.severity, "affected_service": i.affected_service,
         "suspected_component": i.rca.suspected_component if i.rca else None,
         "confidence": i.rca.confidence if i.rca else None, "score": i.score,
         "created_at": i.created_at, "simulated": i.simulated}
        for i in engine.store.list(limit)
    ]


@app.get("/incidents/{incident_id}", response_model=Incident)
def incident(incident_id: str) -> Incident:
    inc = engine.store.incidents.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    return inc


@app.post("/rca/{incident_id}", response_model=Incident)
def rerun_rca(incident_id: str, use_llm: bool = True) -> Incident:
    inc = engine.store.incidents.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    if inc.simulated:
        raise HTTPException(400, "模擬事件請重新執行 POST /test/incident")
    signals = engine.last_signals or inc.signals
    engine.analyze(inc, signals, engine.live, engine.store.changes, use_llm=use_llm)
    engine.store.put(inc)
    return inc


class TestIncidentReq(BaseModel):
    scenario: str = "provider-timeout"
    notify: bool = False
    use_llm: bool = True


@app.get("/test/scenarios")
def scenarios() -> list[str]:
    return engine.list_scenarios()


@app.post("/test/incident")
def test_incident(req: TestIncidentReq) -> dict[str, Any]:
    """用 examples/sample-incidents 的模擬情境跑完整流程（不需要真的故障）"""
    try:
        sc = engine.load_scenario(req.scenario)
    except FileNotFoundError:
        raise HTTPException(404, f"scenario not found, available: {engine.list_scenarios()}")
    incs = engine.simulate(sc, notify=req.notify, use_llm=req.use_llm)
    return {"scenario": req.scenario, "expected_root_cause": sc.get("expected_root_cause"),
            "incidents": [i.model_dump(mode="json") for i in incs]}


@app.post("/detect/run")
def detect_now() -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in engine.run_cycle()]


class ChangeReq(BaseModel):
    service: str
    type: str = "deployment"
    description: str = ""
    author: Optional[str] = None


@app.post("/changes", dependencies=[Depends(require_feature("change_tracking"))])
def add_change(req: ChangeReq) -> ChangeEvent:
    """CI/CD 部署完成時呼叫，讓 RCA 能關聯「最近變更」"""
    if not engine.topo.get(req.service):
        raise HTTPException(400, "unknown service")
    c = ChangeEvent(**req.model_dump())
    engine.store.add_change(c)
    return c


class MaintenanceReq(BaseModel):
    service: str
    minutes: int = 60


@app.post("/maintenance", dependencies=[Depends(require_feature("maintenance"))])
def maintenance(req: MaintenanceReq) -> dict[str, Any]:
    until = datetime.now(timezone.utc) + timedelta(minutes=req.minutes)
    engine.store.maintenance[req.service] = until
    return {"service": req.service, "until": until}


class FeedbackReq(BaseModel):
    correct: bool
    actual_root_cause: Optional[str] = None
    actual_component: Optional[str] = None
    action_taken: Optional[str] = None
    recovered: Optional[bool] = None


@app.post("/incidents/{incident_id}/feedback", dependencies=[Depends(require_feature("feedback_dataset"))])
def feedback(incident_id: str, req: FeedbackReq) -> dict[str, Any]:
    """工程師回饋 RCA 對錯 → 累積成 RCA 評估資料集"""
    inc = engine.store.incidents.get(incident_id)
    if not inc:
        raise HTTPException(404, "incident not found")
    inc.feedback = req.model_dump() | {"at": datetime.now(timezone.utc).isoformat()}
    if req.recovered:
        inc.status = "resolved"
    engine.store.put(inc)
    return {"ok": True}


# ---------------------------------------------------------------- 授權
@app.get("/license")
def license_status() -> dict[str, Any]:
    st = engine.license
    return st.model_dump() | {
        "monitored_services": engine.monitored_services,
        "unmonitored_by_license": engine.unmonitored_by_license,
        "feature_names": {f: FEATURE_NAMES.get(f, f) for f in st.features},
    }


class LicenseReq(BaseModel):
    license_key: str


@app.post("/license", dependencies=[Depends(require_admin)])
def set_license(req: LicenseReq) -> dict[str, Any]:
    """輸入新的 License Key 並立即線上啟用"""
    try:
        engine.license_manager.set_license_key(req.license_key)
    except ValueError as e:
        raise HTTPException(400, f"License Key 無效：{e}")
    engine.refresh_license()
    return license_status()


@app.post("/license/refresh", dependencies=[Depends(require_admin)])
def refresh_license() -> dict[str, Any]:
    engine.refresh_license()
    return license_status()


@app.post("/license/deactivate", dependencies=[Depends(require_admin)])
def deactivate_license() -> dict[str, Any]:
    """換主機前先停用本機，釋放啟用名額"""
    ok = engine.license_manager.deactivate()
    engine.apply_license()
    return {"deactivated": ok} | license_status()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    rows = "".join(
        f"<tr><td><a href='/incidents/{i.id}'>{i.id}</a></td><td>{i.status}</td><td>{i.severity}</td>"
        f"<td>{i.affected_service}</td><td>{i.rca.suspected_component if i.rca else '-'}</td>"
        f"<td>{int(i.rca.confidence * 100) if i.rca else '-'}%</td><td>{(i.rca.root_cause if i.rca else '')[:120]}</td></tr>"
        for i in engine.store.list(50))
    return f"""<!doctype html><meta charset=utf-8><title>system-tool</title>
<style>body{{font-family:sans-serif;margin:24px}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}</style>
<h2>system-tool · AI SRE RCA</h2>{_license_banner()}<p><a href=/docs>API 文件</a> · <a href=/services>服務狀態</a> · <a href=/signals>訊號</a></p>
<table><tr><th>ID</th><th>狀態</th><th>嚴重度</th><th>受影響</th><th>可疑元件</th><th>信心</th><th>根因</th></tr>{rows}</table>"""


def _license_banner() -> str:
    st = engine.license
    color = {"valid": "#2e7d32", "grace": "#ef6c00"}.get(st.status, "#c62828")
    extra = "".join(f"<br>⚠ {w}" for w in st.warnings)
    if engine.unmonitored_by_license:
        extra += f"<br>⚠ 超出方案服務數上限，未監控：{', '.join(engine.unmonitored_by_license)}"
    return (f"<p style='padding:8px;border-left:4px solid {color}'>授權：<b>{st.status}</b>"
            f"｜生效方案：<b>{st.edition}</b>{'｜' + st.customer if st.customer else ''}｜{st.reason}{extra}"
            f"｜<a href=/license>詳情</a></p>")
