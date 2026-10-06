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
from .llm_rca import OllamaRCA
from .models import ChangeEvent, Incident, Signal, utcnow
from .notifications import Notifier
from .orchestrator import run_rca
from .store import Store
from .tools import AgentTools
from .topology import Topology


class Engine:
    def __init__(self, settings: Settings, persist: bool = True):
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
        signals = evaluate_rules(self.rules, source, now.timestamp())
        self.last_signals, self.last_run = signals, now
        changes = [*self.store.changes, *(extra_changes or [])]
        candidates = correlate(signals, self.topo, self.rules, changes, self.store.maintenance_services(),
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
        tools = AgentTools(source, self.topo, self.kb, changes_provider=lambda: changes,
                           incidents_provider=self.store.list, incident=inc)
        inc.rca = run_rca(inc, signals, tools, self.llm if use_llm else None)
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
        src = StaticSource(scenario, self.rules.rules)
        now = utcnow()
        changes = [ChangeEvent(service=c["service"], type=c.get("type", "deployment"),
                               description=c.get("description", ""),
                               timestamp=now - timedelta(minutes=float(c.get("minutes_ago", 5))))
                   for c in scenario.get("changes", [])]
        return self.run_cycle(src, extra_changes=changes, simulated=True, notify=notify, use_llm=use_llm)
