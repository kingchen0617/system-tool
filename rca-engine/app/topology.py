"""Service Graph：從 topology.yaml 載入，提供依賴查詢。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

CRIT_RANK = {"P1": 3, "P2": 2, "P3": 1}


class Topology:
    def __init__(self, services: list[dict[str, Any]]):
        self.services: dict[str, dict[str, Any]] = {}
        for s in services:
            s.setdefault("depends_on", [])
            s.setdefault("owner", "unknown")
            s.setdefault("criticality", "P3")
            s.setdefault("type", "application")
            self.services[s["id"]] = s
        unknown = {d for s in self.services.values() for d in s["depends_on"] if d not in self.services}
        if unknown:
            raise ValueError(f"topology.yaml depends_on 參照了不存在的服務: {sorted(unknown)}")

    @classmethod
    def load(cls, path: Path) -> "Topology":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(data.get("services", []))

    def get(self, sid: str) -> Optional[dict[str, Any]]:
        return self.services.get(sid)

    def owner(self, sid: str) -> str:
        return (self.services.get(sid) or {}).get("owner", "unknown")

    def criticality(self, sid: str) -> int:
        return CRIT_RANK.get((self.services.get(sid) or {}).get("criticality", "P3"), 1)

    def dependencies(self, sid: str) -> list[str]:
        """直接依賴"""
        return list((self.services.get(sid) or {}).get("depends_on", []))

    def all_dependencies(self, sid: str) -> set[str]:
        """遞移依賴（往下游全部）"""
        seen: set[str] = set()
        stack = self.dependencies(sid)
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            stack.extend(self.dependencies(d))
        return seen

    def dependents(self, sid: str) -> list[str]:
        """直接依賴 sid 的上游服務"""
        return [s for s, v in self.services.items() if sid in v["depends_on"]]

    def all_dependents(self, sid: str) -> set[str]:
        seen: set[str] = set()
        stack = self.dependents(sid)
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            stack.extend(self.dependents(d))
        return seen

    def connected_groups(self, nodes: set[str]) -> list[set[str]]:
        """把異常服務依「有沒有依賴路徑相連」分組（只考慮 nodes 之間的路徑，中間可以經過正常服務）。"""
        groups: list[set[str]] = []
        remaining = set(nodes)
        while remaining:
            start = remaining.pop()
            group = {start}
            frontier = [start]
            while frontier:
                cur = frontier.pop()
                related = (self.all_dependencies(cur) | self.all_dependents(cur)) & remaining
                for r in related:
                    remaining.discard(r)
                    group.add(r)
                    frontier.append(r)
            groups.append(group)
        return groups

    def subgraph(self, sid: str) -> dict[str, Any]:
        """給 LLM / API 用：sid 以及其所有依賴的精簡描述"""
        ids = [sid, *sorted(self.all_dependencies(sid))]
        return {
            i: {k: self.services[i].get(k) for k in ("name", "type", "owner", "criticality", "depends_on")}
            for i in ids if i in self.services
        }
