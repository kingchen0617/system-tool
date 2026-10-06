"""資料來源：Prometheus / Loki（正式），以及 StaticSource（模擬情境、測試用）。

所有 DataSource 都提供：
  query_range(source, query, start, end, step) -> list[(unix_ts, value)]
  query_logs(selector_or_query, start, end, limit) -> list[str]
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any, Protocol

Series = list[tuple[float, float]]


class DataSource(Protocol):
    def query_range(self, source: str, query: str, start: float, end: float, step: int) -> Series: ...
    def query_logs(self, query: str, start: float, end: float, limit: int = 50) -> list[str]: ...


class LiveSource:
    """連線到真正的 Prometheus 與 Loki（唯讀查詢）"""

    def __init__(self, prometheus_url: str, loki_url: str, timeout: float = 15.0):
        import httpx  # 延遲匯入，讓沒有 httpx 的環境也能跑模擬/測試

        self.prom = prometheus_url.rstrip("/")
        self.loki = loki_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout)

    def query_range(self, source: str, query: str, start: float, end: float, step: int) -> Series:
        if source == "loki":
            url = f"{self.loki}/loki/api/v1/query_range"
            params = {"query": query, "start": int(start * 1e9), "end": int(end * 1e9), "step": f"{step}s"}
        else:
            url = f"{self.prom}/api/v1/query_range"
            params = {"query": query, "start": start, "end": end, "step": step}
        r = self.client.get(url, params=params)
        r.raise_for_status()
        result = r.json().get("data", {}).get("result", [])
        if not result:
            return []
        # 規則要求單一序列；若有多條，取「每個時間點的最大值」
        merged: dict[float, float] = {}
        for series in result:
            for ts, val in series.get("values", []):
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    continue
                if math.isnan(v):
                    continue
                merged[float(ts)] = max(merged.get(float(ts), v), v)
        return sorted(merged.items())

    def query_logs(self, query: str, start: float, end: float, limit: int = 50) -> list[str]:
        r = self.client.get(
            f"{self.loki}/loki/api/v1/query_range",
            params={"query": query, "start": int(start * 1e9), "end": int(end * 1e9),
                    "limit": limit, "direction": "backward"},
        )
        r.raise_for_status()
        lines: list[tuple[str, str]] = []
        for stream in r.json().get("data", {}).get("result", []):
            for ts, line in stream.get("values", []):
                lines.append((ts, line))
        lines.sort(reverse=True)
        return [l for _, l in lines[:limit]]


class StaticSource:
    """模擬情境：從 examples/sample-incidents/*.json 產生時間序列與 log。

    scenario 格式：
    {
      "name": "...",
      "expected_root_cause": "provider-api",
      "metrics": {
         "<rule_id>": {"baseline": 0.3, "noise": 0.05, "recent": [3.5, 4.0, 4.2, 4.1, 4.4, 4.6]}
      },
      "logs": {"proxy": ["...timeout..."]},
      "changes": [{"service": "payment-api", "type": "deployment", "description": "...", "minutes_ago": 8}]
    }
    沒列出的規則 → 用「正常」數值（default_baseline）產生。
    """

    def __init__(self, scenario: dict[str, Any], rules: list[dict[str, Any]], seed: int = 42):
        self.scenario = scenario
        self.rules = {r["id"]: r for r in rules}
        self.query_to_rule = {r["query"]: r["id"] for r in rules}
        self.seed = seed

    @classmethod
    def from_file(cls, path: Path, rules: list[dict[str, Any]]) -> "StaticSource":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), rules)

    def _spec(self, rule_id: str) -> dict[str, Any]:
        spec = self.scenario.get("metrics", {}).get(rule_id)
        if spec:
            return spec
        rule = self.rules[rule_id]
        thr = (rule.get("threshold") or {}).get("value", 1.0)
        base = float(rule.get("normal", thr * 0.3 if thr else 1.0))
        return {"baseline": base, "noise": abs(base) * 0.05 + 1e-6, "recent": []}

    def query_range(self, source: str, query: str, start: float, end: float, step: int) -> Series:
        rule_id = self.query_to_rule.get(query)
        if rule_id is None:
            return []
        spec = self._spec(rule_id)
        if spec.get("no_data"):
            return []
        rng = random.Random(f"{self.seed}-{rule_id}")
        n = int((end - start) // step) + 1
        recent = [float(v) for v in spec.get("recent", [])]
        base, noise = float(spec["baseline"]), float(spec.get("noise", abs(float(spec["baseline"])) * 0.05 + 1e-6))
        values: list[float] = []
        for i in range(n):
            idx_from_end = n - 1 - i
            if idx_from_end < len(recent):
                values.append(recent[len(recent) - 1 - idx_from_end])
            else:
                values.append(max(0.0, base + rng.gauss(0, noise)))
        return [(start + i * step, v) for i, v in enumerate(values)]

    def query_logs(self, query: str, start: float, end: float, limit: int = 50) -> list[str]:
        logs = self.scenario.get("logs", {})
        for service, lines in logs.items():
            if f'service="{service}"' in query:
                return list(lines)[:limit]
        return []
