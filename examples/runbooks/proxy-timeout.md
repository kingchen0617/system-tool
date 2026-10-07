---
id: proxy-timeout
services: [proxy]
kinds: [errors, saturation]
keywords: [proxy, squid, timeout, connect, filedescriptors]
---
# Outbound Proxy（Squid）連線逾時

## 判斷重點
- 先看 proxy 自身資源（CPU、記憶體、file descriptor）。資源正常而逾時暴增 → 多半是上游外部服務問題（見 provider-outage）。
- 資源飽和 + `running out of filedescriptors` → proxy 本身瓶頸。

## 建議處置
1. 檢查 squid workers 數量與 max_filedescriptors（例：workers 2 → 4、max_filedescriptors 65535 → 131072）
2. 檢查 proxy 主機 CPU / 連線數，必要時暫時擴充第二台 proxy 分流
3. 確認 DNS 解析與到上游的網路延遲
