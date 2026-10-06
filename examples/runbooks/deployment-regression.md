---
id: deployment-regression
services: [payment-api, payment-job, nginx]
kinds: [errors]
keywords: [deploy, deployment, release, rollback, regression]
---
# 部署後回歸（Regression）

## 判斷重點
- 錯誤率在部署後數分鐘內上升，而所有依賴服務（DB、Redis、Proxy、服務商）都正常。

## 建議處置
1. 立即評估 rollback 到上一個版本
2. 比對該版本變更內容與錯誤堆疊
3. 修正後補上對應的自動化測試
