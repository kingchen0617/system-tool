"""異常偵測 + 關聯 + 降低誤報評分。

流程：
  1. evaluate_rule()   每條規則 → Signal（固定門檻 + z-score 動態基準）
  2. correlate()       把異常 Signal 依服務拓撲分組
  3. score_group()     多訊號評分，決定 incident / warning / suppressed
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .datasource import DataSource, Series
from .models import ChangeEvent, Signal
from .topology import Topology

OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}

# 評分原則：每一項代表「一個獨立的證據」，不因訊號種類本身加分（避免重複計分）
DEFAULT_SCORING = {
    "threshold_breach": 2,      # 固定門檻持續超標
    "zscore_anomaly": 1,        # 動態基準異常（與門檻高度相關，只加 1）
    "severe_breach": 2,         # critical 規則超標達門檻 2 倍以上
    "multi_signal_family": 2,   # 兩種以上不同類型的訊號（latency/errors/saturation/...）同時異常
    "dependency_abnormal": 2,   # 有依賴關係的多個服務同時異常
    "recent_change": 1,
    "momentary_only": -2,
    "maintenance": -5,
    "incident": 5, "warning": 3,
}


@dataclass
class RuleSet:
    rules: list[dict[str, Any]]
    defaults: dict[str, Any]
    scoring: dict[str, int]

    @classmethod
    def load(cls, path: Path) -> "RuleSet":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        defaults = {"step": 60, "for": 300, "window": 3600, "zscore": 3.0, **(data.get("defaults") or {})}
        scoring = {**DEFAULT_SCORING, **(data.get("scoring") or {})}
        rules = data.get("rules") or []
        ids = [r["id"] for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rules.yaml 有重複的 rule id")
        return cls(rules=rules, defaults=defaults, scoring=scoring)


# ---------------------------------------------------------------- 1. 單一規則
def robust_baseline(values: list[float]) -> tuple[float, float]:
    """回傳 (median, 1.4826 * MAD)。常態分佈下 1.4826*MAD ≈ 標準差。"""
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values]) if len(values) > 1 else 0.0
    return med, 1.4826 * mad


def evaluate_series(rule: dict[str, Any], series: Series, now: float, defaults: dict[str, Any]) -> Signal:
    thr = rule.get("threshold") or {}
    anom = rule.get("anomaly") or {}
    for_s = int(thr.get("for", rule.get("for", defaults["for"])))
    zlimit = float(anom.get("zscore", defaults["zscore"]))
    guard = int(anom.get("guard", defaults.get("guard", 600)))
    direction = anom.get("direction", "up")

    base = dict(rule_id=rule["id"], service=rule["service"], kind=rule.get("kind", "errors"),
                source=rule.get("source", "prometheus"), severity=rule.get("severity", "warning"),
                threshold=thr.get("value"), operator=thr.get("operator", ">") if thr else None)

    if not series:
        return Signal(current=0.0, no_data=True, **base)

    cutoff = now - for_s
    recent = [v for ts, v in series if ts > cutoff] or [series[-1][1]]
    # baseline 與 recent 中間留一段 guard band：事故若已持續一段時間，前段不會污染 baseline
    history = [v for ts, v in series if ts <= cutoff - guard]
    current = statistics.fmean(recent)

    # 固定門檻：整段 for 期間「每個點」都超標才算持續超標
    breached = momentary = False
    if thr and "value" in thr:
        op = OPS[thr.get("operator", ">")]
        hits = [op(v, float(thr["value"])) for v in recent]
        breached = all(hits) and len(hits) > 0
        momentary = (not breached) and hits[-1]

    # 動態基準：穩健 z-score（median + MAD），對歷史中的尖峰/長尾不敏感
    mean = std = z = None
    anomalous = False
    if history:
        mean, std = robust_baseline(history)
        floor = max(abs(mean) * 0.05, 1e-6)  # 避免 MAD≈0（平坦序列）時 z 爆大
        z = (current - mean) / max(std, floor)
        if anom.get("enabled", False) and len(history) >= 10:
            if direction == "up":
                anomalous = z >= zlimit
            elif direction == "down":
                anomalous = z <= -zlimit
            else:
                anomalous = abs(z) >= zlimit
            # 只有最後一點跳高、近期其他點正常 → 視為瞬間尖峰
            if anomalous and len(recent) >= 3:
                ups = [(v - mean) / max(std, floor) >= zlimit for v in recent]
                if sum(ups) <= 1:
                    anomalous = False
                    momentary = True

    return Signal(current=current, baseline_mean=mean, baseline_std=std, zscore=z,
                  threshold_breached=breached, anomalous=anomalous, momentary=momentary, **base)


def evaluate_rules(ruleset: RuleSet, source: DataSource, now: float | None = None) -> list[Signal]:
    now = now or datetime.now(timezone.utc).timestamp()
    out: list[Signal] = []
    for rule in ruleset.rules:
        d = ruleset.defaults
        window = int((rule.get("anomaly") or {}).get("window", d["window"]))
        guard = int((rule.get("anomaly") or {}).get("guard", d.get("guard", 600)))
        for_s = int((rule.get("threshold") or {}).get("for", d["for"]))
        step = int(rule.get("step", d["step"]))
        try:
            series = source.query_range(rule.get("source", "prometheus"), rule["query"],
                                        now - window - guard - for_s, now, step)
        except Exception as e:  # 單一規則失敗不影響其他規則
            series = []
            print(f"[detection] rule {rule['id']} query failed: {e}")
        out.append(evaluate_series(rule, series, now, d))
    return out


# ---------------------------------------------------------------- 2/3. 關聯 + 評分
@dataclass
class Candidate:
    services: set[str]
    signals: list[Signal]
    changes: list[ChangeEvent]
    score: int
    breakdown: dict[str, int]
    status: str          # incident | warning | suppressed
    affected_service: str
    severity: str
    fingerprint: str


def pick_affected(services: set[str], signals: list[Signal], topo: Topology) -> str:
    """受影響服務 = 群組中「最上游」的異常服務（沒有其他異常服務依賴它）"""
    tops = [s for s in services if not (topo.all_dependents(s) & services)] or list(services)
    weight = {s: sum(1 for x in signals if x.service == s) for s in services}
    return sorted(tops, key=lambda s: (-topo.criticality(s), -weight.get(s, 0), s))[0]


def is_severe(s: Signal) -> bool:
    """critical 規則、持續超標，且超過門檻 2 倍（> 類）或低於門檻一半（< 類）"""
    if s.severity != "critical" or not s.threshold_breached or s.threshold is None:
        return False
    if s.operator in (">", ">="):
        return s.current >= 2 * s.threshold if s.threshold > 0 else True
    return s.current <= s.threshold / 2


def score_group(services: set[str], signals: list[Signal], changes: list[ChangeEvent],
                maintenance: set[str], scoring: dict[str, int]) -> tuple[int, dict[str, int]]:
    b: dict[str, int] = {}
    if any(s.threshold_breached for s in signals):
        b["threshold_breach"] = scoring["threshold_breach"]
    if any(s.anomalous for s in signals):
        b["zscore_anomaly"] = scoring["zscore_anomaly"]
    if any(is_severe(s) for s in signals):
        b["severe_breach"] = scoring["severe_breach"]
    if len({s.kind for s in signals if s.abnormal}) >= 2:
        b["multi_signal_family"] = scoring["multi_signal_family"]
    if len(services) > 1:
        b["dependency_abnormal"] = scoring["dependency_abnormal"]
    if changes:
        b["recent_change"] = scoring["recent_change"]
    if signals and all(s.momentary and not s.threshold_breached and not s.anomalous for s in signals):
        b["momentary_only"] = scoring["momentary_only"]
    if services & maintenance:
        b["maintenance"] = scoring["maintenance"]
    return sum(b.values()), b


def correlate(signals: list[Signal], topo: Topology, ruleset: RuleSet,
              changes: list[ChangeEvent] | None = None, maintenance: set[str] | None = None,
              now: datetime | None = None, change_lookback: int = 1800) -> list[Candidate]:
    now = now or datetime.now(timezone.utc)
    maintenance = maintenance or set()
    recent_changes = [c for c in (changes or []) if c.timestamp >= now - timedelta(seconds=change_lookback)]
    abnormal = [s for s in signals if s.abnormal]
    # 瞬間尖峰也納入（用來扣分），但不單獨形成群組
    services = {s.service for s in abnormal}
    out: list[Candidate] = []
    for group in topo.connected_groups(services):
        g_signals = [s for s in abnormal if s.service in group]
        affected = pick_affected(group, g_signals, topo)
        scope = group | {affected} | topo.all_dependencies(affected)
        g_changes = [c for c in recent_changes if c.service in scope]
        score, breakdown = score_group(group, g_signals, g_changes, maintenance, ruleset.scoring)
        if score >= ruleset.scoring["incident"]:
            status = "incident"
        elif score >= ruleset.scoring["warning"]:
            status = "warning"
        else:
            status = "suppressed"
        if group <= maintenance:  # 全部異常服務都在維護中 → 不建立事件
            status = "suppressed"
        severity = "critical" if any(s.severity == "critical" for s in g_signals) else "warning"
        fp = hashlib.sha1(f"{affected}|{','.join(sorted(group))}".encode()).hexdigest()[:12]
        out.append(Candidate(services=group, signals=g_signals, changes=g_changes, score=score,
                             breakdown=breakdown, status=status, affected_service=affected,
                             severity=severity, fingerprint=fp))
    # 只有瞬間尖峰的服務：回報為 suppressed，方便觀察
    spikes = [s for s in signals if s.momentary and not s.abnormal and s.service not in services]
    for svc in sorted({s.service for s in spikes}):
        sv = [s for s in spikes if s.service == svc]
        score, breakdown = score_group({svc}, sv, [], maintenance, ruleset.scoring)
        out.append(Candidate(services={svc}, signals=sv, changes=[], score=score, breakdown=breakdown,
                             status="suppressed", affected_service=svc, severity="warning",
                             fingerprint=hashlib.sha1(f"spike|{svc}".encode()).hexdigest()[:12]))
    return sorted(out, key=lambda c: -c.score)
