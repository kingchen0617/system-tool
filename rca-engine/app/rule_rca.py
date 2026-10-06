"""規則式 RCA（無 LLM 時的退路，也是 LLM 的「基準假設」）。

核心演算法：沿著 Service Graph 找「最深的異常節點」
  - 一個異常服務，若它的所有（遞移）依賴都正常 → 它是 root cause 候選
  - 上游服務的異常視為「被傳遞的症狀」
  - 只有受影響服務本身異常、依賴都正常 → 看最近是否有部署/設定變更（regression）
"""
from __future__ import annotations

from .models import Action, Evidence, RCAResult, RuledOut, Signal, confidence_label, fmt
from .orchestrator import RCAContext

KIND_ZH = {"latency": "延遲", "errors": "錯誤率", "saturation": "資源飽和", "traffic": "流量", "availability": "可用性"}

TYPE_ACTIONS: dict[str, list[str]] = {
    "database": ["檢查 slow query log 與執行計畫（EXPLAIN）", "檢查連線數與鎖等待（SHOW PROCESSLIST）", "必要時調整連線池或補索引"],
    "cache": ["檢查 Redis SLOWLOG 與記憶體使用", "確認是否有大 key 或阻塞指令（KEYS、大量 DEL）"],
    "external": ["確認外部服務商狀態頁或聯繫服務商", "啟用備援服務商或降級（circuit breaker）", "降低重試次數，避免 retry storm"],
    "network": ["檢查 proxy 連線數、worker 數與 file descriptor 上限", "檢查 proxy 到上游的網路與 DNS"],
    "application": ["檢查最近部署，必要時 rollback", "檢查應用程式錯誤 log 與例外堆疊"],
    "worker": ["檢查 queue backlog 與 worker 數量", "檢查失敗 job 的錯誤訊息"],
    "gateway": ["檢查 upstream 健康狀態與 nginx error log"],
}


def _sig_strength(sigs: list[Signal]) -> float:
    z = max((abs(s.zscore or 0) for s in sigs), default=0)
    return len(sigs) * 2 + sum(1 for s in sigs if s.kind in ("errors", "latency")) + min(z, 50) / 10


def rule_rca(ctx: RCAContext) -> RCAResult:
    topo, inc = ctx.topo, ctx.incident
    abnormal = ctx.abnormal
    evidence: list[Evidence] = []
    actions: list[str] = []

    if not abnormal:
        return RCAResult(incident_id=inc.id, status="warning", affected_service=ctx.affected,
                         suspected_component="unknown", root_cause="unknown：目前沒有足夠的異常證據",
                         confidence=0.2, confidence_label="unknown", reasoning_source="rule",
                         owner=topo.owner(ctx.affected))

    # 1) 最深的異常節點
    roots = [s for s in abnormal if not (topo.all_dependencies(s) & set(abnormal))]
    roots.sort(key=lambda s: -_sig_strength(abnormal[s]))
    root = roots[0]
    alternatives = roots[1:]
    root_type = (topo.get(root) or {}).get("type", "application")
    root_name = (topo.get(root) or {}).get("name", root)
    kinds = sorted({KIND_ZH.get(s.kind, s.kind) for s in abnormal[root]})

    conf = 0.5
    conf += 0.15 if len(roots) == 1 else -0.1
    if len(abnormal[root]) >= 2:
        conf += 0.1

    # 2) 傳遞鏈：上游受影響服務
    chain = [s for s in abnormal if s != root and root in topo.all_dependencies(s)]
    if root != ctx.affected and root in topo.all_dependencies(ctx.affected):
        conf += 0.1

    # 3) 證據：root 訊號 → 中間節點「資源正常」反證 → log → 上游症狀
    for s in abnormal[root]:
        evidence.append(Evidence(type="log" if s.source == "loki" else "metric", source=s.source,
                                 description=s.describe()))
    chain.sort(key=lambda s: len(topo.all_dependencies(s)))
    symptoms: list[Evidence] = []
    for svc in chain:
        for s in abnormal[svc]:
            symptoms.append(Evidence(type="log" if s.source == "loki" else "metric", source=s.source,
                                     description=f"[症狀] {s.describe()}"))
        # 中間節點自身資源正常 → 證明它只是「傳遞症狀」，不是資源不足
        for s in ctx.signals_for(svc):
            if s.kind == "saturation" and not s.abnormal:
                evidence.append(Evidence(type="metric", source=s.source,
                                         description=f"{svc} {s.rule_id} 正常（{fmt(s.current)}）→ 非 {svc} 本身資源不足"))
    for svc in [root, *chain]:
        lines = ctx.logs.get(svc) or []
        if lines:
            if svc == root:
                conf += 0.05
            evidence.append(Evidence(type="log", source="loki",
                                     description=f"{svc} 近 15 分鐘錯誤 log {len(lines)} 筆，例：{lines[0][:160]}"))
    evidence.extend(symptoms)

    # 4) Root cause 敘述
    changes_on_root = [c for c in ctx.changes if c.service == root]
    if changes_on_root:
        conf += 0.15 if root == ctx.affected else 0.05
        c = changes_on_root[0]
        evidence.append(Evidence(type="change", source="changes",
                                 description=f"{c.service} {c.type} @ {c.timestamp.isoformat(timespec='minutes')}：{c.description}"))

    if root == ctx.affected and changes_on_root:
        c = changes_on_root[0]
        cause = (f"{root_name} 在 {c.timestamp.isoformat(timespec='minutes')} 有{c.type}變更（{c.description}），"
                 f"其依賴服務皆正常，推測為此次變更造成的回歸（regression）")
        actions.append("評估 rollback 最近一次部署/設定變更")
    elif root == ctx.affected:
        cause = f"{root_name} 本身{'/'.join(kinds)}異常，其依賴服務皆正常；可能是應用程式本身、資源或流量問題"
        conf -= 0.05
    else:
        levels: dict[int, list[str]] = {}
        for svc in chain:
            levels.setdefault(len(topo.all_dependencies(svc)), []).append(svc)
        via = " → ".join("、".join(sorted(v)) for _, v in sorted(levels.items()))
        cause = f"{root_name}（{root_type}）{'/'.join(kinds)}異常，沿依賴鏈影響上游服務" + (f"：{via}" if via else "")

    # 依賴中有沒監控到的服務 → 不能完全排除
    blind = [s for s in ctx.unmonitored if s in topo.all_dependencies(root) or s == root]
    if blind:
        conf -= 0.1
        evidence.append(Evidence(type="topology", source="topology",
                                 description=f"注意：{', '.join(blind)} 沒有監控資料，無法排除"))

    # 5) 排除項目
    ruled_out = [
        RuledOut(component=svc, reason="所有監控指標正常：" + ", ".join(f"{s.rule_id}={fmt(s.current)}" for s in sigs))
        for svc, sigs in ctx.normal.items()
        if svc in topo.all_dependencies(ctx.affected) or svc in topo.all_dependencies(root)
    ]

    # 6) 建議處置：runbook（優先比對 root 服務）+ 類型預設
    rbs = sorted(ctx.runbooks, key=lambda r: 0 if root in r.services else 1)
    for rb in rbs[:2]:
        if root in rb.services or not rb.services:
            actions.extend(rb.actions[:3])
            evidence.append(Evidence(type="runbook", source="runbook", description=f"符合 runbook：{rb.id}（{rb.title}）"))
    if not any(root in rb.services for rb in rbs):  # 沒有專屬 runbook → 用類型預設處置
        actions.extend(TYPE_ACTIONS.get(root_type, []))
    for h in ctx.history:
        if h.rca and h.rca.suspected_component == root:
            conf += 0.05
            evidence.append(Evidence(type="history", source="incidents",
                                     description=f"與歷史事件 {h.id} 相似：{h.rca.root_cause[:100]}"))
            break
    seen: set[str] = set()
    uniq = [a for a in actions if not (a in seen or seen.add(a))]

    conf = max(0.05, min(conf, 0.95))
    label = confidence_label(conf)
    if label == "unknown":
        cause = f"unknown（證據不足）— 最可疑：{root}。" + cause

    return RCAResult(
        incident_id=inc.id, status=inc.severity, affected_service=ctx.affected, suspected_component=root,
        root_cause=cause, confidence=conf, confidence_label=label, reasoning_source="rule",
        evidence=evidence, ruled_out=ruled_out, alternatives=alternatives,
        recommended_actions=[Action(priority=i + 1, action=a) for i, a in enumerate(uniq[:6])],
        owner=topo.owner(root),
    )
