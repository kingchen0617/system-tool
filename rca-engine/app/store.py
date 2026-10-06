"""事件儲存：MVP 用記憶體 + JSON 檔（data/incidents.json），之後可換 PostgreSQL。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import ChangeEvent, Incident


class Store:
    def __init__(self, data_dir: Optional[Path] = None):
        self.lock = threading.RLock()
        self.incidents: dict[str, Incident] = {}
        self.changes: list[ChangeEvent] = []
        self.maintenance: dict[str, datetime] = {}  # service -> until
        self.path = Path(data_dir) / "incidents.json" if data_dir else None
        self._seq = 0
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    inc = Incident.model_validate(d)
                    self.incidents[inc.id] = inc
            except Exception as e:
                print(f"[store] 無法載入 {self.path}: {e}")

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [i.model_dump(mode="json") for i in self.incidents.values()]
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def new_id(self) -> str:
        with self.lock:
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            n = sum(1 for i in self.incidents if i.startswith(f"INC-{day}")) + 1
            return f"INC-{day}-{n:04d}"

    def open_by_fingerprint(self, fp: str, within: int = 1800) -> Optional[Incident]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within)
        for i in self.incidents.values():
            if i.fingerprint == fp and i.status in ("incident", "warning") and i.updated_at >= cutoff:
                return i
        return None

    def put(self, inc: Incident) -> None:
        with self.lock:
            self.incidents[inc.id] = inc
        self.save()

    def list(self, limit: int = 100) -> list[Incident]:
        return sorted(self.incidents.values(), key=lambda i: i.created_at, reverse=True)[:limit]

    def add_change(self, c: ChangeEvent) -> None:
        with self.lock:
            self.changes.append(c)
            self.changes = self.changes[-1000:]

    def maintenance_services(self) -> set[str]:
        now = datetime.now(timezone.utc)
        return {s for s, until in self.maintenance.items() if until > now}
