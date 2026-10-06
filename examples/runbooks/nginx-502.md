---
id: nginx-502
services: [nginx]
kinds: [errors]
keywords: [nginx, 502, 504, upstream, php-fpm]
---
# Nginx 502 / 504

## 建議處置
1. 檢查 nginx error log 的 upstream 錯誤訊息
2. 檢查 PHP-FPM pool 是否滿載（pm.max_children）
3. 確認 upstream（payment-api）健康狀態
