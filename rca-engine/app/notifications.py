"""通知：Slack Incoming Webhook / LINE Messaging API（push）/ Email SMTP / console。

依 config/notifications.yaml 的 owner + severity 路由；同一事件在 cooldown 內不重複通知。
通知內容只放「摘要 + 證據重點」，不貼完整 log。
（注意：LINE Notify 已停止服務，這裡使用 LINE Messaging API。）
"""
from __future__ import annotations

import json
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .models import Incident

ICON = {"critical": "🔴", "warning": "🟠"}
LABEL_ZH = {"likely": "高度可能", "suspected": "疑似", "unknown": "未知／需調查"}


def format_message(inc: Incident, public_url: str = "") -> str:
    r = inc.rca
    lines = [f"{ICON.get(inc.severity, '⚪')} [{inc.severity.upper()}] {inc.id}",
             f"受影響服務：{inc.affected_service}"]
    if r:
        lines += [
            f"可疑元件：{r.suspected_component}（信心度 {int(r.confidence * 100)}%，{LABEL_ZH[r.confidence_label]}）",
            f"根因判斷：{r.root_cause}",
            "證據：",
            *[f"  • {e.description}" for e in r.evidence[:5]],
        ]
        if r.ruled_out:
            lines.append("已排除：" + "、".join(x.component for x in r.ruled_out))
        if r.recommended_actions:
            lines.append("建議處置：")
            lines += [f"  {a.priority}. {a.action}" for a in r.recommended_actions[:3]]
        lines.append(f"負責人：{r.owner}　（推理來源：{r.reasoning_source}{' / ' + r.model if r.model else ''}）")
    if public_url:
        lines.append(f"詳情：{public_url.rstrip('/')}/incidents/{inc.id}")
    return "\n".join(lines)


class Notifier:
    def __init__(self, path: Path, settings: Settings):
        data: dict[str, Any] = {}
        if Path(path).exists():
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.routes = data.get("routes", [])
        self.default = (data.get("default") or {}).get("channels", ["console"])
        self.contacts = data.get("contacts", {})
        self.cooldown = int(data.get("cooldown_seconds", 1800))
        self.s = settings
        self.allowed_channels: set[str] | None = None   # None = 不限（由授權決定）

    def channels_for(self, owner: str, severity: str) -> list[str]:
        for r in self.routes:
            if r.get("owner") == owner and severity in r.get("severity", []):
                return r.get("channels", [])
        return self.default

    def notify(self, inc: Incident, force: bool = False) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        if not force and inc.notified_at and now - inc.notified_at < timedelta(seconds=self.cooldown):
            return {"skipped": "cooldown"}
        owners = list(dict.fromkeys([inc.rca.owner if inc.rca else "unknown"]))
        text = format_message(inc, self.s.public_url)
        results: dict[str, str] = {}
        for owner in owners:
            for ch in self.channels_for(owner, inc.severity):
                if self.allowed_channels is not None and ch not in self.allowed_channels:
                    results[f"{owner}:{ch}"] = "skipped (目前方案不含 notifications 功能)"
                    continue
                try:
                    results[f"{owner}:{ch}"] = getattr(self, f"_send_{ch}")(owner, text, inc)
                except AttributeError:
                    results[f"{owner}:{ch}"] = "unknown channel"
                except Exception as e:
                    results[f"{owner}:{ch}"] = f"error: {e}"
        inc.notified_at = now
        return results

    # ---- channels ----
    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> int:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def _send_console(self, owner: str, text: str, inc: Incident) -> str:
        print(f"\n===== NOTIFY → {owner} =====\n{text}\n============================\n")
        return "ok"

    def _send_slack(self, owner: str, text: str, inc: Incident) -> str:
        if not self.s.slack_webhook_url:
            return "skipped (SLACK_WEBHOOK_URL 未設定)"
        return f"ok ({self._post_json(self.s.slack_webhook_url, {'text': text})})"

    def _send_line(self, owner: str, text: str, inc: Incident) -> str:
        token = self.s.line_channel_access_token
        targets = (self.contacts.get(owner) or {}).get("line_to", [])
        if not token or not targets:
            return "skipped (LINE token 或 line_to 未設定)"
        for to in targets:
            self._post_json("https://api.line.me/v2/bot/message/push",
                            {"to": to, "messages": [{"type": "text", "text": text[:4900]}]},
                            {"Authorization": f"Bearer {token}"})
        return f"ok ({len(targets)})"

    def _send_email(self, owner: str, text: str, inc: Incident) -> str:
        to = (self.contacts.get(owner) or {}).get("email", [])
        if not self.s.smtp_host or not to:
            return "skipped (SMTP_HOST 或收件人未設定)"
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = f"[{inc.severity.upper()}] {inc.id} {inc.affected_service}"
        msg["From"] = self.s.smtp_from
        msg["To"] = ", ".join(to)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            if self.s.smtp_tls:
                smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)
        return f"ok ({len(to)})"
