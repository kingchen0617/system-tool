"""LLM RCA：用本地 Ollama（預設 qwen3:4b）根據「已收集的證據」推理 root cause。

安全設計：
  - LLM 只能看到 orchestrator 收集到的證據，不能自己執行任何指令
  - 輸出必須是合法 JSON 且通過 Pydantic 驗證；失敗重試 1 次，仍失敗 → 回傳 None（由規則式 RCA 接手）
  - suspected_component 必須是拓撲中存在的服務；若 LLM 指認「沒有任何異常訊號」的服務，信心度強制降到 0.5 以下
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Optional

from pydantic import ValidationError

from .models import Action, Evidence, RCAResult, RuledOut, confidence_label
from .orchestrator import RCAContext

SYSTEM_PROMPT = """You are an SRE root-cause-analysis assistant.
You receive monitoring evidence (metrics anomalies, logs, topology, recent changes, runbooks) for ONE incident.

Rules:
- Use ONLY the evidence given. Do not invent facts, metrics or services.
- In the topology, "depends_on" means the service calls those services. A failure deep in the dependency
  chain usually propagates UP to callers; prefer the deepest abnormal service whose own dependencies are normal.
- A service with normal saturation metrics but timeouts towards a dependency is usually a symptom, not the cause.
- If evidence is insufficient, set suspected_component to "unknown" and confidence below 0.6.
- Distinguish observed facts (evidence) from hypotheses (root_cause) and list ruled-out components.
- "baseline_hypothesis" is the result of a rule engine. Agree with it unless the evidence clearly contradicts it.
- Write root_cause and actions in Traditional Chinese (繁體中文).
- Return ONLY valid JSON with this shape:
{"suspected_component": "<service id or unknown>",
 "root_cause": "<one or two sentences>",
 "confidence": <0..1>,
 "evidence": [{"type": "metric|log|change|topology|runbook|history", "source": "...", "description": "..."}],
 "ruled_out": [{"component": "...", "reason": "..."}],
 "recommended_actions": ["...", "..."]}
"""


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class OllamaRCA:
    def __init__(self, url: str, model: str, timeout: float = 90.0, think: str = ""):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.think = think

    # ---- Ollama HTTP ----
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.url}{path}", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=3) as r:
                tags = json.loads(r.read().decode())
            names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
            if self.model not in names and f"{self.model}:latest" not in names:
                print(f"[llm] 模型 {self.model} 尚未下載（ollama pull {self.model}）")
                return False
            return True
        except Exception:
            return False

    def chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                   "format": "json", "options": {"temperature": 0.1}}
        if self.think != "":
            payload["think"] = self.think.lower() in ("1", "true", "yes")
        return self._post("/api/chat", payload).get("message", {}).get("content", "")

    # ---- RCA ----
    def analyze(self, ctx: RCAContext, baseline: RCAResult) -> Optional[RCAResult]:
        if not self.healthy():
            print("[llm] Ollama 無法使用 → 規則式 RCA")
            return None
        data = ctx.to_prompt_dict()
        data["baseline_hypothesis"] = {"suspected_component": baseline.suspected_component,
                                       "root_cause": baseline.root_cause, "confidence": baseline.confidence}
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)}]
        for attempt in range(2):
            raw = self.chat(messages)
            try:
                return self._to_result(_extract_json(raw), ctx, baseline)
            except (ValueError, ValidationError, KeyError, TypeError) as e:
                print(f"[llm] 輸出驗證失敗（第 {attempt + 1} 次）：{e}")
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": f"Invalid output: {e}. Return ONLY the JSON object."}]
        return None

    def _to_result(self, d: dict[str, Any], ctx: RCAContext, baseline: RCAResult) -> RCAResult:
        topo = ctx.topo
        comp = str(d.get("suspected_component", "unknown")).strip()
        if comp != "unknown" and topo.get(comp) is None:
            raise ValueError(f"suspected_component '{comp}' 不在拓撲中")
        conf = float(d.get("confidence", 0))
        if conf > 1:
            conf = conf / 100.0
        # 防幻覺：指認的元件完全沒有異常證據 → 降信心
        if comp != "unknown" and comp not in ctx.abnormal:
            conf = min(conf, 0.5)
        if comp == "unknown":
            conf = min(conf, 0.55)
        evidence = [Evidence(**e) if isinstance(e, dict) else Evidence(description=str(e))
                    for e in d.get("evidence", [])]
        if not evidence:
            evidence = baseline.evidence
        ruled_out = [RuledOut(**r) for r in d.get("ruled_out", []) if isinstance(r, dict)]
        acts = d.get("recommended_actions", [])
        actions = [Action(priority=i + 1, action=a if isinstance(a, str) else a.get("action", str(a)))
                   for i, a in enumerate(acts)] or baseline.recommended_actions
        root_cause = str(d.get("root_cause", "")).strip() or "unknown"
        if not root_cause:
            raise ValueError("root_cause 為空")
        return RCAResult(
            incident_id=ctx.incident.id, status=ctx.incident.severity, affected_service=ctx.affected,
            suspected_component=comp, root_cause=root_cause, confidence=max(0.0, min(conf, 0.99)),
            confidence_label=confidence_label(conf), reasoning_source="llm", model=self.model,
            evidence=evidence, ruled_out=ruled_out or baseline.ruled_out,
            alternatives=[a for a in [baseline.suspected_component] if a != comp],
            recommended_actions=actions[:6],
            owner=topo.owner(comp) if comp != "unknown" else topo.owner(ctx.affected),
        )
