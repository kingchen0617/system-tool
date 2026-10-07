# system-tool 技術說明

> 給開發者與導入工程師看的詳細規格。入門請先看 [README](../README.md)，流程圖請看 [product-flowcharts.md](product-flowcharts.md)。

## 1. RCA 推理邏輯

- `depends_on` 表示「我呼叫誰」。下游故障會往上游傳遞，所以依賴都正常的異常服務最可能是 root cause。
- **多候選排序**：每個異常服務都會計算候選分數，最後依分數排序，不會只硬選一個。

  | 項目 | 說明 |
  |---|---|
  | downstream_health | 它的依賴全部正常 +3；有依賴也異常（較可能只是症狀）−3 |
  | propagation | 其他異常服務中，「依賴它」（能被它解釋）的比例 × 3 |
  | signal_strength | 異常訊號數、是否持續超標、z-score 大小 |
  | change | 這個服務最近有部署或設定變更 |

- **信心度主要看第一名領先第二名多少**：分數接近時，會列出所有接近的候選並標為 `unknown`。如果候選之間沒有依賴關係，會提示「可能是兩個獨立故障，或共同的網路／基礎設施問題」。
- 中間節點如果自身資源正常（例如 proxy CPU 正常但逾時暴增），會被判定為「症狀」，不是原因。
- 如果只有應用程式本身異常、依賴全部正常，而且 30 分鐘內有部署，就判定為部署回歸（regression）。
- 信心度分三級：≥85% 為 `likely`（高度可能），60–84% 為 `suspected`（疑似），<60% 為 `unknown`（證據不足，不會假裝知道）。

## 2. LLM 防幻覺：只推理，不創造事實

1. 收集到的每個事實（指標、log、變更、runbook、歷史事件）都會給一個 ID，例如 `sig_provider-latency`、`log_proxy_0`、`chg_0`。
2. LLM 只能回傳 `evidence_ids`，不能自己寫證據。證據內容由 Python 從已收集的資料重新組裝。
3. 下列情況都算驗證失敗：引用不存在的 ID、指認拓撲中不存在的服務、把有異常訊號的服務列進 `ruled_out`。驗證失敗會重試一次，仍失敗就改用規則式 RCA。
4. 如果 LLM 指認的元件沒有異常訊號，或它引用的證據都和這個元件無關，信心度會被壓到 0.5 以下。

LLM 回傳格式：

```json
{"suspected_component": "provider-api", "root_cause": "…", "confidence": 0.9,
 "evidence_ids": ["sig_provider-latency", "sig_proxy-cpu", "log_proxy_0"],
 "ruled_out": ["redis", "mariadb"], "recommended_actions": ["…"]}
```

---

## 3. 降低誤報的評分機制（`rules.yaml → scoring`）

每一項代表一個**獨立的證據**。訊號種類本身（例如「是錯誤率」）不會加分，避免重複計分。

| 條件 | 分數 |
|---|---|
| 固定門檻「持續」超標（整段 `for` 期間） | +2 |
| 動態基準異常（和門檻高度相關，只加 1） | +1 |
| critical 規則超過門檻 2 倍以上 | +2 |
| 兩種以上不同類型的訊號（latency／errors／saturation）同時異常 | +2 |
| 有依賴關係的多個服務同時異常 | +2 |
| 30 分鐘內有部署／設定變更 | +1 |
| 只是瞬間尖峰 | −2 |
| 維護中（全部異常服務都在維護 → 直接抑制） | −5 |

**≥5 為 Incident，會通知**；3–4 為 Warning，只記錄不通知；<3 會被抑制。

舉例：單一訊號輕微超標只有 2+1 = 3 分，是 Warning；API 5xx 衝到 40% 則是 2+1+2 = 5 分，會通知。

### 動態基準（穩健 z-score）

```
|←──────── baseline（window, 預設 60 分）────────→|←─ guard（10 分）─→|←─ recent（for, 5 分）─→| now
z = (recent 平均 − baseline 中位數) / (1.4826 × MAD)
```

- **guard band**：事故已經持續一段時間時，前段的異常資料不會被算進 baseline，避免 z-score 被拉低。
- **median + MAD**：比平均值加標準差更不受歷史尖峰影響。
- 同一事件在 cooldown 期間（預設 30 分鐘）內不重複通知。

---

## 4. API 完整清單

| Method | Path | 說明 |
|---|---|---|
| GET | `/health` | 引擎、LLM 狀態 |
| GET | `/services` | 各服務目前狀態（ok／abnormal／no_data） |
| GET | `/signals` | 最近一次偵測的所有訊號 |
| GET | `/incidents` | 事件列表 |
| GET | `/incidents/{id}` | 事件詳情（含 RCA、證據、工具呼叫稽核紀錄） |
| POST | `/rca/{id}` | 重新執行 RCA |
| POST | `/detect/run` | 立即執行一次偵測 |
| GET | `/test/scenarios` | 可用的模擬情境 |
| POST | `/test/incident` | 執行模擬情境 `{"scenario":"db-latency","notify":false,"use_llm":true}` |
| POST | `/changes` | 登錄部署或設定變更 |
| POST | `/maintenance` | 設定維護時段 `{"service":"mariadb","minutes":60}` |
| POST | `/incidents/{id}/feedback` | 工程師回饋 RCA 是否正確，累積評估資料集（Business 以上） |
| GET | `/license` | 授權狀態、生效方案、功能、監控中的服務 |
| POST | `/license` | 設定 License Key 並線上啟用 |
| POST | `/license/refresh` | 立即續驗 |
| POST | `/license/deactivate` | 停用本機（換主機前執行） |

`/changes`、`/maintenance` 需要 Professional 以上；目前方案不含該功能時回傳 HTTP 402。設定 `ENGINE_ADMIN_TOKEN` 後，`/license` 的 POST 操作需帶 `X-Admin-Token` header。

### RCA 輸出格式

```json
{
  "incident_id": "INC-20261006-0001",
  "status": "critical",
  "affected_service": "payment-api",
  "suspected_component": "provider-api",
  "root_cause": "…",
  "confidence": 0.85,
  "confidence_label": "likely",
  "reasoning_source": "llm | rule",
  "model": "qwen3:4b",
  "evidence": [{"type": "metric", "source": "prometheus", "description": "…"}],
  "ruled_out": [{"component": "mariadb", "reason": "所有監控指標正常…"}],
  "alternatives": [],
  "recommended_actions": [{"priority": 1, "action": "…"}],
  "owner": "payment-team",
  "generated_at": "2026-10-06T12:00:00Z"
}
```

---

## 5. 模擬情境與驗收

`examples/sample-incidents/` 目前有 11 個情境：

- **一般故障：** 外部服務逾時、DB 慢查詢、DB 連線耗盡、Redis 延遲、部署回歸、Proxy 資源耗盡。
- **v0.2 新增：**
  - 持續 15 分鐘的 DB 問題（測 baseline 不被污染）
  - 只有 API 5xx 但很嚴重（測單一嚴重訊號仍會通知）
  - Redis 和 MariaDB 同時異常（測多候選時會降低信心度）
- **不應通知：** CPU 瞬間尖峰（應抑制）、Queue 堆積（只列 Warning）。

情境檔可以用 `acceptable_root_causes` 列出可接受的答案，用 `max_confidence` 設定信心度上限。模稜兩可的情境如果給出過高的信心度，會被判定為錯誤。

```
python -m app.cli evaluate --no-llm
Top-1 準確率：9/9 = 100%
Top-3 準確率：9/9 = 100%
誤報：0/2
```

驗收目標：真實故障案例 Top-3 準確率 ≥80%、誤報率 <10%、RCA 在 60 秒內完成。之後請把**真實發生過的故障**也做成情境檔加進來，持續累積評估資料集。

---

## 6. 專案結構

```
system-tool/
├── docker-compose.yml / .env.example / Makefile
├── config/
│   ├── topology.yaml          # Service Graph
│   ├── rules.yaml             # 偵測規則 + 評分
│   └── notifications.yaml     # 告警路由
├── rca-engine/
│   ├── Dockerfile / requirements.txt
│   ├── app/
│   │   ├── main.py            # FastAPI + 背景偵測迴圈
│   │   ├── cli.py             # simulate / evaluate / detect
│   │   ├── engine.py          # 串接整個流程
│   │   ├── detection.py       # 門檻、z-score、關聯、評分
│   │   ├── topology.py        # 服務拓撲
│   │   ├── datasource.py      # Prometheus / Loki / 模擬資料
│   │   ├── tools.py           # Agent 白名單工具 + 稽核
│   │   ├── orchestrator.py    # 收集證據 → 推理
│   │   ├── rule_rca.py        # 規則式 RCA
│   │   ├── llm_rca.py         # Ollama / Qwen RCA
│   │   ├── knowledge.py       # Runbook 檢索（RAG v0）
│   │   ├── notifications.py   # Slack / LINE / Email
│   │   ├── store.py           # 事件儲存（JSON）
│   │   ├── licensing.py       # 授權用戶端（簽章驗證、線上啟用、方案限制）
│   │   ├── license_keys.py    # 授權方公鑰
│   │   ├── models.py          # Pydantic schema
│   │   └── config.py
│   └── tests/                 # test_rca.py、test_licensing.py
├── license-server/            # 授權伺服器 + 簽發工具（賣方使用，不出貨給客戶）
│   ├── app/{tokens,core,editions,admin,main}.py
│   └── docker-compose.yml
├── LICENSE / EULA.md / LICENSING.md / THIRD_PARTY_NOTICES.md
├── observability/             # Prometheus / Loki / Grafana 設定
└── examples/
    ├── runbooks/              # Markdown runbook（含 front matter）
    └── sample-incidents/      # 模擬情境
```
