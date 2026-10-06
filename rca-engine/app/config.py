"""環境變數設定。所有設定都可以用 .env / docker-compose environment 覆寫。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


_BASE = Path(__file__).resolve().parents[2]  # repo root（本機執行時）


@dataclass
class Settings:
    prometheus_url: str = field(default_factory=lambda: os.getenv("PROMETHEUS_URL", "http://localhost:9090"))
    loki_url: str = field(default_factory=lambda: os.getenv("LOKI_URL", "http://localhost:3100"))

    llm_enabled: bool = field(default_factory=lambda: _bool(os.getenv("LLM_ENABLED"), True))
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen3:4b"))
    ollama_timeout: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "90")))
    # qwen3 等「思考型」模型建議關閉 thinking 以加快速度；不支援的模型請留空
    ollama_think: str = field(default_factory=lambda: os.getenv("OLLAMA_THINK", ""))

    config_dir: Path = field(default_factory=lambda: Path(os.getenv("CONFIG_DIR", str(_BASE / "config"))))
    runbook_dir: Path = field(default_factory=lambda: Path(os.getenv("RUNBOOK_DIR", str(_BASE / "examples" / "runbooks"))))
    scenario_dir: Path = field(default_factory=lambda: Path(os.getenv("SCENARIO_DIR", str(_BASE / "examples" / "sample-incidents"))))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(_BASE / "data"))))

    detection_enabled: bool = field(default_factory=lambda: _bool(os.getenv("DETECTION_ENABLED"), True))
    detection_interval: int = field(default_factory=lambda: int(os.getenv("DETECTION_INTERVAL", "60")))
    change_lookback: int = field(default_factory=lambda: int(os.getenv("CHANGE_LOOKBACK_SECONDS", "1800")))

    slack_webhook_url: str = field(default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", ""))
    line_channel_access_token: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.getenv("SMTP_FROM", "system-tool@localhost"))
    smtp_tls: bool = field(default_factory=lambda: _bool(os.getenv("SMTP_TLS"), True))
    public_url: str = field(default_factory=lambda: os.getenv("PUBLIC_URL", "http://localhost:8000"))


settings = Settings()
