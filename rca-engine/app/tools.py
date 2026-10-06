"""Agent Tools：RCA Agent 只能呼叫這些「白名單、唯讀」工具。

每次呼叫都會寫入 incident.tool_calls（稽核紀錄），之後才能回溯 AI 為什麼得出這個結論。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from .datasource import DataSource
from .knowledge import KnowledgeBase
from .models import ChangeEvent, Incident, ToolCall
from .topology import Topology


class AgentTools:
    def __init__(self, source: DataSource, topo: Topology, kb: KnowledgeBase,
                 changes_provider: Callable[[], list[ChangeEvent]],
                 incidents_provider: Callable[[], list[Incident]],
                 incident: Incident | None = None, now: float | None = None):
        self.source = source
        self.topo = topo
        self.kb = kb
        self._changes = changes_provider
        self._incidents = incidents_provider
        self.incident = incident
        self.now = now or datetime.now(timezone.utc).timestamp()

    # ---- 稽核包裝 ----
    def _audit(self, tool: str, args: dict[str, Any], fn: Callable[[], Any], summarize: Callable[[Any], str]):
        t0 = time.monotonic()
        ok, result = True, None
        try:
            result = fn()
            summary = summarize(result)
        except Exception as e:
            ok, summary = False, f"error: {e}"
        if self.incident is not None:
            self.incident.tool_calls.append(ToolCall(
                tool=tool, arguments=args, duration_ms=int((time.monotonic() - t0) * 1000),
                success=ok, result_summary=summary[:300]))
        return result

    # ---- 工具 ----
    def query_prometheus(self, query: str, minutes: int = 30, step: int = 60):
        args = {"query": query, "minutes": minutes}
        return self._audit("query_prometheus", args,
                           lambda: self.source.query_range("prometheus", query, self.now - minutes * 60, self.now, step),
                           lambda r: f"{len(r)} points" + (f", last={r[-1][1]:.4g}" if r else ""))

    def query_loki(self, query: str, minutes: int = 15, limit: int = 20) -> list[str]:
        args = {"query": query, "minutes": minutes, "limit": limit}
        return self._audit("query_loki", args,
                           lambda: self.source.query_logs(query, self.now - minutes * 60, self.now, limit),
                           lambda r: f"{len(r)} lines") or []

    def get_topology(self, service_id: str) -> dict[str, Any]:
        return self._audit("get_topology", {"service_id": service_id},
                           lambda: self.topo.subgraph(service_id), lambda r: f"{len(r)} nodes") or {}

    def get_dependencies(self, service_id: str) -> list[str]:
        return self._audit("get_dependencies", {"service_id": service_id},
                           lambda: sorted(self.topo.all_dependencies(service_id)), lambda r: ",".join(r)) or []

    def get_service_owner(self, service_id: str) -> str:
        return self._audit("get_service_owner", {"service_id": service_id},
                           lambda: self.topo.owner(service_id), str)

    def search_runbook(self, query: str = "", services: list[str] | None = None,
                       kinds: list[str] | None = None, limit: int = 3):
        return self._audit("search_runbook", {"query": query, "services": services, "kinds": kinds},
                           lambda: self.kb.search(query, services, kinds, limit),
                           lambda r: ",".join(x.id for x in r)) or []

    def search_incidents(self, service_id: str, limit: int = 5) -> list[Incident]:
        def run():
            hist = [i for i in self._incidents()
                    if i.rca and (i.rca.suspected_component == service_id or i.affected_service == service_id)
                    and (self.incident is None or i.id != self.incident.id)]
            hist.sort(key=lambda i: i.created_at, reverse=True)
            return hist[:limit]
        return self._audit("search_incidents", {"service_id": service_id}, run,
                           lambda r: ",".join(i.id for i in r)) or []

    def get_recent_changes(self, services: list[str], minutes: int = 30) -> list[ChangeEvent]:
        def run():
            since = datetime.fromtimestamp(self.now - minutes * 60, timezone.utc)
            return [c for c in self._changes() if c.service in services and c.timestamp >= since]
        return self._audit("get_recent_changes", {"services": services, "minutes": minutes}, run,
                           lambda r: f"{len(r)} changes") or []
