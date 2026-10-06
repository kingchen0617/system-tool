"""RCA Orchestrator：用白名單工具收集證據 → 交給 LLM（Ollama/Qwen）推理 → 失敗則退回規則式 RCA。

MVP 採「固定調查流程」（deterministic tool plan），而不是讓小模型自由決定要呼叫哪些工具，
這樣 4B 等小模型也能穩定運作；工具呼叫全部有稽核紀錄。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .knowledge import Runbook
from .models import ChangeEvent, Incident, RCAResult, Signal
from .tools import AgentTools
from .topology import Topology


@dataclass
class RCAContext:
    incident: Incident
    topo: Topology
    affected: str
    scope: list[str]                         # 受影響服務 + 其所有依賴 + 群組內服務
    abnormal: dict[str, list[Signal]]        # 異常服務 → 異常訊號
    normal: dict[str, list[Signal]]          # 有監控且正常的服務 → 訊號
    unmonitored: list[str]
    logs: dict[str, list[str]] = field(default_factory=dict)
    changes: list[ChangeEvent] = field(default_factory=list)
    runbooks: list[Runbook] = field(default_factory=list)
    history: list[Incident] = field(default_factory=list)
    all_signals: list[Signal] = field(default_factory=list)

    def signals_for(self, service: str) -> list[Signal]:
        return [s for s in self.all_signals if s.service == service and not s.no_data]

    def to_prompt_dict(self) -> dict[str, Any]:
        def sig(s: Signal) -> dict[str, Any]:
            return {"rule": s.rule_id, "kind": s.kind, "current": round(s.current, 4),
                    "baseline": None if s.baseline_mean is None else round(s.baseline_mean, 4),
                    "zscore": None if s.zscore is None else round(s.zscore, 1),
                    "threshold": f"{s.operator} {s.threshold}" if s.threshold is not None else None,
                    "threshold_breached": s.threshold_breached, "anomalous": s.anomalous}
        return {
            "incident_id": self.incident.id,
            "affected_service": self.affected,
            "topology": {s: self.topo.subgraph(s)[s] for s in self.scope if self.topo.get(s)},
            "abnormal_services": {k: [sig(s) for s in v] for k, v in self.abnormal.items()},
            "normal_services": {k: [sig(s) for s in v] for k, v in self.normal.items()},
            "unmonitored_services": self.unmonitored,
            "recent_logs": {k: v[:10] for k, v in self.logs.items() if v},
            "recent_changes": [c.model_dump(mode="json") for c in self.changes],
            "runbooks": [{"id": r.id, "title": r.title, "actions": r.actions[:5]} for r in self.runbooks],
            "similar_past_incidents": [
                {"id": h.id, "root_cause": h.rca.root_cause, "component": h.rca.suspected_component,
                 "confirmed": (h.feedback or {}).get("correct")}
                for h in self.history if h.rca
            ],
        }


def build_context(incident: Incident, all_signals: list[Signal], tools: AgentTools) -> RCAContext:
    topo = tools.topo
    affected = incident.affected_service
    tools.get_topology(affected)
    deps = tools.get_dependencies(affected)
    scope = list(dict.fromkeys([affected, *deps, *incident.services]))

    abnormal: dict[str, list[Signal]] = {}
    normal: dict[str, list[Signal]] = {}
    for svc in scope:
        sigs = [s for s in all_signals if s.service == svc and not s.no_data]
        bad = [s for s in sigs if s.abnormal]
        if bad:
            abnormal[svc] = bad
        elif sigs:
            normal[svc] = sigs
    unmonitored = [s for s in scope if s not in abnormal and s not in normal]

    logs: dict[str, list[str]] = {}
    for svc in [*abnormal.keys(), affected]:
        sel = (topo.get(svc) or {}).get("log_selector")
        if sel and svc not in logs:
            logs[svc] = tools.query_loki(f'{sel} |~ "(?i)error|timeout|fail|refused|exception"', minutes=15, limit=20)

    changes = tools.get_recent_changes(scope, minutes=30)
    kinds = sorted({s.kind for v in abnormal.values() for s in v})
    runbooks = tools.search_runbook(" ".join(kinds), services=list(abnormal.keys()) or [affected], kinds=kinds)
    history: list[Incident] = []
    for svc in abnormal.keys():
        history.extend(tools.search_incidents(svc, limit=2))
    for svc in scope:
        tools.get_service_owner(svc)

    return RCAContext(incident=incident, topo=topo, affected=affected, scope=scope, abnormal=abnormal,
                      normal=normal, unmonitored=unmonitored, logs=logs, changes=changes,
                      runbooks=runbooks, history=history, all_signals=all_signals)


def run_rca(incident: Incident, all_signals: list[Signal], tools: AgentTools,
            llm: Optional[Any] = None) -> RCAResult:
    from .rule_rca import rule_rca

    ctx = build_context(incident, all_signals, tools)
    baseline = rule_rca(ctx)
    if llm is not None:
        try:
            result = llm.analyze(ctx, baseline)
            if result is not None:
                return result
        except Exception as e:
            print(f"[rca] LLM 推理失敗，改用規則式 RCA: {e}")
    return baseline
