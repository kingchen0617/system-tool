---
id: db-latency
services: [mariadb]
kinds: [latency, saturation]
keywords: [mariadb, mysql, slow, query, connections, lock]
---
# MariaDB 延遲 / 連線數過高

## 建議處置
1. 查看 slow query log，找出最慢的 SQL 並 EXPLAIN
2. SHOW PROCESSLIST 檢查鎖等待與長時間交易
3. 連線數耗盡時：檢查應用程式連線池設定與是否有連線洩漏，必要時暫時調高 max_connections
