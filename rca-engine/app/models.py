"""資料模型（Pydantic）。RCA 輸出不論來自 LLM 或規則引擎，都必須符合 RCAResult。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Kind = Literal["latency", "errors", "saturation", "traffic", "availability"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Signal(BaseModel):
    """單一規則的偵測結果"""
    rule_id: str
    service: str
    kind: str
    source: str = "prometheus"
    severity: str = "warning"
    current: float
    baseline_mean: Optional[float] = None
    baseline_std: Optional[float] = None
    zscore: Optional[float] = None
    threshold: Optional[float] = None
    operator: Optional[str] = None
    threshold_breached: bool = False  # 整段 for 期間持續超標
    anomalous: bool = False           # z-score 超過設定
    momentary: bool = False           # 只有最後一點超標（瞬間尖峰）
    no_data: bool = False

    @property
    def abnormal(self) -> bool:
        return (self.threshold_breached or self.anomalous) and not self.no_data

    def describe(self) -> str:
        parts = [f"{self.service} {self.rule_id}: 目前 {fmt(self.current)}"]
        if self.baseline_mean is not None:
            parts.append(f"(基準 {fmt(self.baseline_mean)}")
            if self.zscore is not None:
                parts[-1] += f", z={self.zscore:.1f}"
            parts[-1] += ")"
        if self.threshold is not None:
            parts.append(f"門檻 {self.operator} {fmt(self.threshold)}")
        return " ".join(parts)


def fmt(v: Optional[float]) -> str:
    if v is None:
        return "-"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.4f}"


class ChangeEvent(BaseModel):
    service: str
    type: str = "deployment"  # deployment | config | infra
    description: str = ""
    timestamp: datetime = Field(default_factory=utcnow)
    author: Optional[str] = None


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}
    timestamp: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    success: bool = True
    result_summary: str = ""


class Evidence(BaseModel):
    type: Literal["metric", "log", "trace", "change", "topology", "runbook", "history"] = "metric"
    source: str = "prometheus"
    description: str


class RuledOut(BaseModel):
    component: str
    reason: str


class Action(BaseModel):
    priority: int = 1
    action: str


class RCAResult(BaseModel):
    incident_id: str
    status: Literal["critical", "warning", "ok"] = "critical"
    affected_service: str
    suspected_component: str
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_label: Literal["likely", "suspected", "unknown"] = "unknown"
    reasoning_source: Literal["llm", "rule"] = "rule"
    model: Optional[str] = None
    evidence: list[Evidence] = []
    ruled_out: list[RuledOut] = []
    alternatives: list[str] = []
    recommended_actions: list[Action] = []
    owner: str = "unknown"
    generated_at: datetime = Field(default_factory=utcnow)

    @field_validator("confidence")
    @classmethod
    def _round(cls, v: float) -> float:
        return round(v, 2)


def confidence_label(c: float) -> str:
    if c >= 0.85:
        return "likely"
    if c >= 0.6:
        return "suspected"
    return "unknown"


class Incident(BaseModel):
    id: str
    status: Literal["incident", "warning", "suppressed", "resolved"] = "incident"
    severity: Literal["critical", "warning"] = "critical"
    score: int = 0
    score_breakdown: dict[str, int] = {}
    services: list[str] = []
    affected_service: str = ""
    signals: list[Signal] = []
    changes: list[ChangeEvent] = []
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    fingerprint: str = ""
    rca: Optional[RCAResult] = None
    tool_calls: list[ToolCall] = []
    notified_at: Optional[datetime] = None
    feedback: Optional[dict[str, Any]] = None
    simulated: bool = False
