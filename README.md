# system-tool：AI SRE／自動根因分析（RCA）平台 MVP

用 AI 做系統監控的第一版。它能做到三件事：

1. **自動判斷系統健康狀態**：固定門檻加上動態基準（z-score），並用多訊號評分來降低誤報。
2. **出問題時自動找根本原因**：沿著服務拓撲找出「最深的異常節點」，由本地 Ollama／Qwen 根據證據推理。沒有 LLM 時，自動改用規則式推理。
3. **明確指出是哪個系統出問題，並通知負責人**：依 owner 和嚴重度，送到 Slack／LINE／Email。

> 定位：**唯讀的 AI SRE**。它只查詢 Prometheus／Loki，不會重啟服務、修改設定，也不會登入主機。

---

## 架構

```
 Nginx / Laravel / Worker / Redis / MariaDB / Proxy / 外部服務商
                │ metrics / logs
                ▼
   Prometheus (metrics)      Loki (logs)        ← Grafana 視覺化
                │                 │
                └──────┬──────────┘
                       ▼
┌──────────────────── rca-engine（Python / FastAPI）────────────────────┐
│ 1. Detection   rules.yaml：固定門檻（需持續 for 秒）+ z-score 動態基準   │
│ 2. Correlation topology.yaml：把異常服務依依賴關係分組                  │
│ 3. Scoring     多訊號評分 ≥5 才算 Incident（瞬間尖峰、維護中會扣分）     │
│ 4. Agent Tools 白名單唯讀工具收集證據（全部寫入稽核紀錄）                │
│                query_prometheus / query_loki / get_topology /          │
│                get_dependencies / get_service_owner / search_runbook / │
│                search_incidents / get_recent_changes                   │
│ 5. Reasoning   Ollama/Qwen → JSON Schema 驗證 → 失敗重試 → 規則式 RCA   │
│ 6. Notify      依 owner + severity 路由到 Slack / LINE / Email         │
└────────────────────────────────────────────────────────────────────────┘
```

### RCA 推理邏輯

- `depends_on` 表示「我呼叫誰」。下游故障會往上游傳遞，所以**依賴都正常的那個異常服務，就是 root cause 候選**。
- 中間節點如果自身資源正常（例如 proxy CPU 正常但逾時暴增），會被判定為「症狀」，不是原因。
- 如果只有應用程式本身異常、依賴全部正常，而且 30 分鐘內有部署，就判定為部署回歸（regression）。
- 信心度分三級：≥85% 為 `likely`（高度可能），60–84% 為 `suspected`（疑似），<60% 為 `unknown`（證據不足，不會假裝知道）。
- 防幻覺設計：
  - LLM 指認的元件必須存在於拓撲中，否則驗證失敗，改用規則式結果。
  - 如果 LLM 指認的元件沒有任何異常證據，信心度會被強制壓到 0.5 以下。

---

## 快速開始

### A. 不需要 Docker：先用模擬情境體驗

```bash
cd rca-engine
pip install -r requirements.txt
python -m app.cli simulate provider-timeout --no-llm   # 單一情境
python -m app.cli evaluate --no-llm                    # 全部情境：Top-1/Top-3 準確率、誤報率
python -m pytest -q                                    # 單元測試
```

輸出範例：

```
🔴 [CRITICAL] INC-20261006-0001
受影響服務：payment-api
可疑元件：provider-api（信心度 85%，高度可能）
根因判斷：External Payment Provider（external）延遲/錯誤率異常，沿依賴鏈影響上游服務：proxy → payment-api、payment-job
證據：
  • provider-api provider-latency: 目前 39.00 (基準 0.9882, z=26.7) 門檻 > 5.00
  • provider-api provider-error-rate: 目前 0.4700 (基準 0.0135, z=14.8) 門檻 > 0.1000
  • proxy proxy-cpu 正常（25.20）→ 非 proxy 本身資源不足
  ...
已排除：mariadb、redis
建議處置：
  1. 確認服務商狀態頁，並聯繫服務商技術窗口
  2. 啟用備援服務商或暫時關閉該通道（circuit breaker）
  3. 降低重試次數與並行數，避免 retry storm 拖垮 worker
負責人：payment-team　（推理來源：rule）
```

### B. 完整環境（Docker Compose）

```bash
cp .env.example .env           # 填入 Slack / LINE / SMTP 等設定
docker compose up -d --build
docker compose exec ollama ollama pull qwen3:4b   # 第一次下載模型

# 用模擬情境跑完整流程（含 LLM 與通知）
curl -X POST localhost:8000/test/incident -H 'Content-Type: application/json' \
     -d '{"scenario":"provider-timeout","notify":true}'
```

| 服務 | 網址 |
|---|---|
| RCA Engine 首頁（事件列表） | http://localhost:8000 |
| API 文件（Swagger） | http://localhost:8000/docs |
| Grafana（admin / admin） | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

> 如果主機上已經裝了 Ollama（例如 Windows 本機的 Qwen3:4b），可以刪掉 compose 裡的 `ollama` 服務，並在 `.env` 設定 `OLLAMA_URL=http://host.docker.internal:11434`。

---

## 接上你的真實系統

1. **Prometheus**：在 `observability/prometheus/prometheus.yml` 加入 exporters，包括 node_exporter、mysqld_exporter、redis_exporter、nginx exporter，以及應用程式的 `/metrics`。記得用 `service` label 對應 topology 的 id。
2. **Loki**：用 Promtail／Grafana Alloy 收集 log，並加上 `service="<topology id>"` label。
3. **`config/topology.yaml`**：描述服務、負責人（owner）、依賴（depends_on）和重要度（criticality）。
4. **`config/rules.yaml`**：每條規則寫一個 PromQL（必須回傳單一序列），再設定門檻、持續時間和 z-score。
5. **`config/notifications.yaml`**：設定 owner 對應的通知管道和收件人。
6. **CI/CD**：部署完成後呼叫 `POST /changes`，RCA 才能關聯「最近變更」：

```bash
curl -X POST localhost:8000/changes -H 'Content-Type: application/json' \
     -d '{"service":"payment-api","type":"deployment","description":"release v2.14.0"}'
```

---

## API

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
| POST | `/incidents/{id}/feedback` | 工程師回饋 RCA 是否正確，累積評估資料集 |

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

## 降低誤報的評分機制（`rules.yaml → scoring`）

| 條件 | 分數 |
|---|---|
| 固定門檻「持續」超標（整段 `for` 期間） | +2 |
| z-score 動態基準異常 | +2 |
| 錯誤率類訊號 | +2 |
| 延遲類訊號 | +2 |
| 有依賴關係的多個服務同時異常 | +2 |
| 30 分鐘內有部署／設定變更 | +1 |
| 只是瞬間尖峰 | −2 |
| 維護中（全部異常服務都在維護 → 直接抑制） | −5 |

**≥5 為 Incident，會通知**；3–4 為 Warning，只記錄不通知；<3 會被抑制。同一事件在 cooldown 期間（預設 30 分鐘）內不重複通知。

---

## 模擬情境與驗收

`examples/sample-incidents/` 目前有 8 個情境：服務商逾時、DB 慢查詢、DB 連線耗盡、Redis 延遲、部署回歸、Proxy 資源耗盡、CPU 瞬間尖峰（應抑制）、Queue 堆積（只列 Warning）。

```
python -m app.cli evaluate --no-llm
Top-1 準確率：6/6 = 100%
Top-3 準確率：6/6 = 100%
誤報：0/2
```

驗收目標：真實故障案例 Top-3 準確率 ≥80%、誤報率 <10%、RCA 在 60 秒內完成。之後請把**真實發生過的故障**也做成情境檔加進來，持續累積評估資料集。

---

## 專案結構

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
│   │   ├── models.py          # Pydantic schema
│   │   └── config.py
│   └── tests/test_rca.py
├── observability/             # Prometheus / Loki / Grafana 設定
└── examples/
    ├── runbooks/              # Markdown runbook（含 front matter）
    └── sample-incidents/      # 模擬情境
```

---

## Roadmap

- [ ] Tempo／OpenTelemetry traces，加入 `query_tempo` 工具
- [ ] AWS connector：CloudWatch、CloudTrail、AWS Config、AWS Health
- [ ] 混合 LLM：本地 Qwen 信心度 <70% 時，交給雲端大模型做深度 RCA
- [ ] RAG 換成 PostgreSQL + pgvector，納入歷史事件的相似度檢索
- [ ] Web UI：事件時間軸、拓撲圖、回饋按鈕
- [ ] 經人工核准後執行修復動作（Human-approved Remediation）
