---
id: redis-latency
services: [redis]
kinds: [latency, saturation]
keywords: [redis, slowlog, bigkey, memory]
---
# Redis 延遲升高

## 建議處置
1. 執行 SLOWLOG GET 128 找出慢指令
2. 用 redis-cli --bigkeys 檢查大 key，避免 KEYS * 等阻塞指令
3. 檢查記憶體使用率與 eviction 狀況
