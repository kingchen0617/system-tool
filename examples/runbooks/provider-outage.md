---
id: provider-outage
services: [provider-api]
kinds: [latency, errors]
keywords: [provider, upstream, timeout, curl, "28"]
---
# 外部服務商（金流）延遲或中斷

## 判斷重點
- 只有特定 upstream 延遲上升，其他 upstream、proxy 資源、DB、Redis 都正常。
- 應用程式 log 出現 `cURL error 28: Operation timed out`。

## 建議處置
1. 確認服務商狀態頁，並聯繫服務商技術窗口
2. 啟用備援服務商或暫時關閉該通道（circuit breaker）
3. 降低重試次數與並行數，避免 retry storm 拖垮 worker
