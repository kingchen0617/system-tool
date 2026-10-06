"""方案（Edition）定義：預設上限與功能。簽發授權時可以再個別覆寫。

⚠ rca-engine/app/licensing.py 裡有一份相同的 COMMUNITY 定義（無授權時的降級模式）；
   修改 community 時兩邊要一起改。
"""
from __future__ import annotations

from typing import Any

ALL_FEATURES = {
    "rule_rca": "規則式 RCA",
    "ai_rca": "AI RCA（Ollama / Qwen）",
    "notifications": "Slack / LINE / Email 通知",
    "change_tracking": "部署 / 變更關聯（POST /changes）",
    "maintenance": "維護時段（POST /maintenance）",
    "feedback_dataset": "RCA 回饋與評估資料集",
    "history_rag": "歷史事件檢索（RAG）",
    "cloud_llm": "雲端大模型混合推理",
    "aws_connector": "AWS Connector",
    "sso": "SSO 登入",
    "ha": "高可用部署",
    "offline_license": "離線授權（不需連線授權伺服器）",
}

EDITIONS: dict[str, dict[str, Any]] = {
    "community": {
        "max_services": 5, "max_nodes": 5, "max_activations": 1,
        "features": ["rule_rca"],
    },
    "professional": {
        "max_services": 50, "max_nodes": 25, "max_activations": 2,
        "features": ["rule_rca", "ai_rca", "notifications", "change_tracking", "maintenance"],
    },
    "business": {
        "max_services": 200, "max_nodes": 100, "max_activations": 3,
        "features": ["rule_rca", "ai_rca", "notifications", "change_tracking", "maintenance",
                     "feedback_dataset", "history_rag"],
    },
    "enterprise": {
        "max_services": 0, "max_nodes": 0, "max_activations": 10,  # 0 = 不限
        "features": sorted(ALL_FEATURES),
    },
}


def edition_defaults(edition: str) -> dict[str, Any]:
    if edition not in EDITIONS:
        raise ValueError(f"未知方案 {edition}，可用：{', '.join(EDITIONS)}")
    d = EDITIONS[edition]
    return {**d, "features": list(d["features"])}
