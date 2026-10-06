"""知識庫（RAG 第一版）：Runbook 與歷史事件的關鍵字檢索。

MVP 先用關鍵字 + 欄位比對，不需要向量資料庫。
之後可換成 PostgreSQL + pgvector，介面（search()）不變。

Runbook 檔案格式（Markdown + YAML front matter）：
---
id: proxy-timeout
services: [proxy, provider-api]
kinds: [errors, latency]
keywords: [timeout, CONNECT, squid]
---
# 標題
...
## 建議處置
1. ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Runbook:
    id: str
    title: str
    services: list[str]
    kinds: list[str]
    keywords: list[str]
    body: str
    actions: list[str] = field(default_factory=list)
    path: str = ""

    def summary(self, n: int = 400) -> str:
        return self.body[:n]


def _parse(path: Path) -> Runbook:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        body = m.group(2)
    title = next((l.lstrip("# ").strip() for l in body.splitlines() if l.startswith("#")), path.stem)
    actions: list[str] = []
    sec = re.search(r"##\s*(建議處置|Actions?|處置)[^\n]*\n(.*?)(\n## |\Z)", body, re.S)
    if sec:
        for line in sec.group(2).splitlines():
            line = re.sub(r"^\s*(\d+\.|[-*])\s*", "", line).strip()
            if line:
                actions.append(line)
    return Runbook(id=meta.get("id", path.stem), title=title, services=[str(x) for x in meta.get("services", [])],
                   kinds=meta.get("kinds", []), keywords=[str(k).lower() for k in meta.get("keywords", [])],
                   body=body.strip(), actions=actions, path=str(path))


class KnowledgeBase:
    def __init__(self, runbook_dir: Path):
        self.runbooks: list[Runbook] = []
        if runbook_dir and Path(runbook_dir).exists():
            for p in sorted(Path(runbook_dir).glob("*.md")):
                try:
                    self.runbooks.append(_parse(p))
                except Exception as e:
                    print(f"[knowledge] skip {p}: {e}")

    def search(self, query: str = "", services: list[str] | None = None,
               kinds: list[str] | None = None, limit: int = 5) -> list[Runbook]:
        q = [w for w in re.split(r"[\s,]+", query.lower()) if w]
        scored = []
        for rb in self.runbooks:
            s = 0
            s += 5 * len(set(services or []) & set(rb.services))
            s += 2 * len(set(kinds or []) & set(rb.kinds))
            hay = (rb.title + " " + rb.body).lower()
            s += sum(3 for w in q if w in rb.keywords) + sum(1 for w in q if w in hay)
            if s > 0:
                scored.append((s, rb))
        scored.sort(key=lambda x: -x[0])
        return [rb for _, rb in scored[:limit]]
