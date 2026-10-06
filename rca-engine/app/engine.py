"""Engine：把「偵測 → 關聯評分 → 建立事件 → RCA → 通知」串起來。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .datasource import DataSource, LiveSource, StaticSource
from .detection import RuleSet, correlate, evaluate_rules
from .knowledge import KnowledgeBase
from .licensing import LicenseManager, LicenseState
from .llm_rca import OllamaRCA
from .models import ChangeEvent, Incident, Signal, utcnow
from .notifications import Notifier
from .orchestrator import run_rca
from .store import Store
from .tools import AgentTools
from .topology import Topology


class Engine:
    def __init__(self, settings: Settings, persist: bool = True,
                 license_manager: Optional[LicenseManager] = None, license_state: Optional[LicenseState] = None):
        self.s = settings
        self.topo = Topology.load(settings.config_dir / "topology.yaml")
        self.rules = RuleSet.load(settings.config_dir / "rules.yaml")
        unknown = {r["service"] for r in self.rules.rules} - set(self.topo.services)
        if unknown:
            raise ValueError(f"rules.yaml 參照了 topology 中不存在的服務: {sorted(unknown)}")
        self.kb = KnowledgeBase(settings.runbook_dir)
        self.notifier = Notifier(settings.config_dir / "notifications.yaml", settings)
        self.store = Store(settings.data_dir if persist else None)
        self.llm: Optional[OllamaRCA] = (
            OllamaRCA(settings.ollama_url, settings.ollama_model, settings.ollama_timeout, settings.ollama_think)
            if settings.llm_enabled else None)
        self._live: Optional[LiveSource] = None
        self.last_signals: list[Signal] = []
        self.last_run: Optional[datetime] = None

        # ---- 授權 ----
        self.all_rules = self.rules
        self.license_manager = license_manager or LicenseManager(
            settings.data_dir, license_key=settings.license_key,
            license_file=Path(settings.license_file) if settings.license_file else None,
            server_url=settings.license_server_url, telemetry_opt_in=settings.telemetry_opt_in, persist=persist)
        self._fixed_license = license_state   # 測試用：直接指定授權狀態
        self.license: LicenseState = LicenseState()
        self.monitored_services: list[str] = []
        self.unmonitored_by_license: list[str] = []
        self.apply_license()

    # ------------------------------------------------------------------ 授權
    def apply_license(self, state: Optional[LicenseState] = None) -> LicenseState:
        """依目前授權調整：可監控的服務數、AI RCA、通知管道、歷史檢索。"""
        st = state or self._fixed_license or self.license_manager.evaluate()
        self.license = st
        ruled = list(dict.fromkeys(r["service"] for r in self.all_rules.rules))
        if st.max_services and len(ruled) > st.max_services:
            order = {sid: i for i, sid in enumerate(self.topo.services)}
            ranked = sorted(ruled, key=lambda sid: (-self.topo.criticality(sid), order.get(sid, 0)))
            keep = set(ranked[:st.max_services])
        else:
            keep = set(ruled)
        self.monitored_services = [s for s in ruled if s in keep]
        self.unmonitored_by_license = [s for s in ruled if s not in keep]
        self.rules = RuleSet(rules=[r for r in self.all_rules.rules if r["service"] in keep],
                             defaults=self.all_rules.defaults, scoring=self.all_rules.scoring)
        self.notifier.allowed_channels = None if self.has_feature("notifications") else {"console"}
        if self.unmonitored_by_license:
            print(f"[license] 方案 {st.edition} 最多監控 {st.max_services} 個服務，"
                  f"未監控：{', '.join(self.unmonitored_by_license)}")
        return st

    def has_feature(self, feature: str) -> bool:
        return feature in self.license.features

    def refresh_license(self) -> LicenseState:
        usage = {"services_configured": len(self.topo.services), "services_monitored": len(self.monitored_services),
                 "incidents": len(self.store.incidents)}
        return self.apply_license(self.license_manager.refresh(usage=usage))

    @property
    def live(self) -> LiveSource:
        if self._live is None:
            self._live = LiveSource(self.s.prometheus_url, self.s.loki_url)
        return self._live

    # ------------------------------------------------------------------
    def run_cycle(self, source: Optional[DataSource] = None, extra_changes: Optional[list[ChangeEvent]] = None,
                  simulated: bool = False, notify: bool = True, use_llm: bool = True) -> list[Incident]:
        source = source or self.live
        now = utcnow()
        # 模擬情境（展示 / 驗收用）不受服務數上限限制；正式監控依授權
        rules = self.all_rules if simulated else self.rules
        signals = evaluate_rules(rules, source, now.timestamp())
        self.last_signals, self.last_run = signals, now
        changes = [*self.store.changes, *(extra_changes or [])]
        candidates = correlate(signals, self.topo, rules, changes, self.store.maintenance_services(),
                               now, self.s.change_lookback)
        out: list[Incident] = []
        for c in candidates:
            if c.status == "suppressed":
                continue
            inc = None if simulated else self.store.open_by_fingerprint(c.fingerprint)
            is_new = inc is None
            if inc is None:
                inc = Incident(id=self.store.new_id(), fingerprint=c.fingerprint, simulated=simulated)
            escalated = inc.status == "warning" and c.status == "incident"
            inc.status = c.status  # type: ignore[assignment]
            inc.severity = c.severity  # type: ignore[assignment]
            inc.score, inc.score_breakdown = c.score, c.breakdown
            inc.services = sorted(c.services)
            inc.affected_service = c.affected_service
            inc.signals = c.signals
            inc.changes = c.changes
            inc.updated_at = now
            if c.status == "incident" and (is_new or escalated or inc.rca is None):
                self.analyze(inc, signals, source, changes, use_llm=use_llm)
                if notify:
                    print(f"[notify] {inc.id}: {self.notifier.notify(inc)}")
            self.store.put(inc)
            out.append(inc)
        return out

    def analyze(self, inc: Incident, signals: list[Signal], source: DataSource,
                changes: list[ChangeEvent], use_llm: bool = True) -> Incident:
        history = self.store.list if self.has_feature("history_rag") else (lambda: [])
        tools = AgentTools(source, self.topo, self.kb, changes_provider=lambda: changes,
                           incidents_provider=history, incident=inc)
        llm = self.llm if (use_llm and self.has_feature("ai_rca")) else None
        inc.rca = run_rca(inc, signals, tools, llm)
        return inc

    # ------------------------------------------------------------------
    def load_scenario(self, name_or_path: str) -> dict[str, Any]:
        p = Path(name_or_path)
        if not p.exists():
            p = self.s.scenario_dir / (name_or_path if name_or_path.endswith(".json") else f"{name_or_path}.json")
        return json.loads(p.read_text(encoding="utf-8"))

    def list_scenarios(self) -> list[str]:
        return sorted(p.stem for p in self.s.scenario_dir.glob("*.json"))

    def simulate(self, scenario: dict[str, Any], notify: bool = False, use_llm: bool = True) -> list[Incident]:
        src = StaticSource(scenario, self.all_rules.rules)
        now = utcnow()
        changes = [ChangeEvent(service=c["service"], type=c.get("type", "deployment"),
                               description=c.get("description", ""),
                               timestamp=now - timedelta(minutes=float(c.get("minutes_ago", 5))))
                   for c in scenario.get("changes", [])]
        return self.run_cycle(src, extra_changes=changes, simulated=True, notify=notify, use_llm=use_llm)
