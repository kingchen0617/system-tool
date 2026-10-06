# Changelog

## v0.3.0 — 2026-10-07｜Commercial Foundation

依與 ChatGPT 討論的商業模式，加入地端安裝 + 線上授權機制。

- **授權用戶端**（`rca-engine/app/licensing.py`）
  - 用 Ed25519 驗證 License Key，公鑰寫在程式裡（`license_keys.py`）。
  - 線上啟用並定期續驗：activation token 綁定本機，租期 7 天，到期後還有寬限期。
  - 離線授權（Enterprise）。
  - 7 種授權狀態；授權失效時降級為 Community，不會停止監控。
  - 防護：防止時間回撥、偵測啟用資料被複製到其他主機。
  - 本機稽核紀錄（`data/license-audit.jsonl`），使用量統計需客戶 opt-in 才會傳送。
- **方案限制**（`engine.py`）
  - 依方案限制受監控服務數，超出時依重要度挑選。
  - AI RCA、Slack／LINE／Email 通知、歷史事件檢索依授權開關。
  - `/changes`、`/maintenance`、`/feedback` 需要對應方案，否則回傳 HTTP 402。
- **API／CLI**
  - 新增 `GET/POST /license`、`/license/refresh`、`/license/deactivate`。
  - `/health` 和首頁顯示授權狀態。
  - 新增 `cli license status|activate|refresh|deactivate`。
  - 設定 `ENGINE_ADMIN_TOKEN` 後，修改授權的 API 需帶 `X-Admin-Token`。
- **授權伺服器**（`license-server/`，賣方使用）
  - 用 SQLite 記錄授權、啟用與稽核資料。
  - 客戶端 API：activate、deactivate。
  - 管理 API：簽發、列表、詳情、撤銷、稽核。
  - 管理 CLI：`keygen`、`issue`、`list`、`show`、`revoke`、`inspect`。
  - Docker Compose 部署。
- **法律文件**：`LICENSE`（專有授權）、`EULA.md`（中文草稿，需律師審閱）、`THIRD_PARTY_NOTICES.md`（含 Grafana／Loki AGPL 注意事項）、`LICENSING.md`。
- **測試**：rca-engine 28 個（新增 12 個端對端授權測試）＋ license-server 5 個。

## v0.2.0 — 2026-10-06

依 ChatGPT 對 v0.1 的 code review，修正 5 個重點問題（依優先順序）：

1. **LLM 只能引用證據，不能創造事實**（`llm_rca.py`、`orchestrator.py`）
   - 新增 `RCAContext.evidence_catalog()`，每個已收集的事實都有 ID，例如 `sig_<rule>`、`log_<svc>_<n>`、`chg_<n>`、`rb_<id>`、`hist_<id>`。
   - LLM 改成回傳 `evidence_ids`，證據內容由 Python 重新組裝。
   - 下列情況都算驗證失敗，會重試一次後改用規則式 RCA：
     - 引用不存在的 ID
     - 指認拓撲中不存在的服務
     - 把有異常訊號的服務列進 `ruled_out`
   - `ruled_out` 的排除理由由實際的正常指標產生。
2. **PromQL 修正**（`config/rules.yaml`）
   - `db-query-latency` 改名為 `db-slow-query-rate`，它量的是慢查詢的發生頻率，不是延遲。
   - Redis 延遲改成先各自 `sum` 再相除，避免 `cmd` label 對不上，指標名稱也改成 `redis_commands_total`。
   - Redis 記憶體在沒有設定 maxmemory 時回傳 no_data，不再算出不合理的百分比。
   - 註明 CPU 規則需要在 scrape target 加上 `service` label。
3. **多候選 RCA**（`rule_rca.py`）
   - 新增 `rank_candidates()`，候選分數 = downstream_health + propagation + signal_strength + change。
   - 信心度改成依「第一名領先第二名多少」計算。
   - 分數接近時會列出所有接近的候選並標為 unknown，也會判斷候選之間是否可能是獨立故障。
   - 證據中加入候選排名。
4. **評分去重**（`detection.py`）
   - 移除 `error_signal`、`latency_signal`：訊號種類本身不再加分。
   - 新增 `multi_signal_family`（兩種以上不同類型的訊號同時異常 +2）和 `severe_breach`（critical 規則超過門檻 2 倍 +2）。
   - `zscore_anomaly` 從 +2 降到 +1。
5. **穩健的動態基準**（`detection.py`）
   - baseline 改用 median + 1.4826×MAD。
   - baseline 與近期資料之間加入 guard band（預設 600 秒），避免持續中的事故污染 baseline。

其他：

- 新增 3 個模擬情境：`db-slow-burn`、`api-5xx-only-severe`、`multi-root-redis-db`。
- 情境檔支援 `acceptable_root_causes` 和 `max_confidence`。
- 測試從 10 個增加到 16 個。
- 修正信心度百分比與分級標籤在四捨五入邊界不一致的問題。

## v0.1.0 — 2026-10-06

- 第一版 MVP：偵測（固定門檻＋z-score）、拓撲關聯與評分、規則式 RCA、Ollama／Qwen RCA、Slack／LINE／Email 通知、FastAPI、CLI、Docker Compose。
