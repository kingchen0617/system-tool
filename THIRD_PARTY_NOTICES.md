# 第三方元件授權聲明（Third-Party Notices）

system-tool 使用或搭配以下第三方元件。這些元件**不屬於** system-tool 的專有部分，依各自的授權條款提供。

> 本表依 2026-10 公開資訊整理。正式對外提供前，請用 SBOM 工具（例如 `syft`、`pip-licenses`）針對實際出貨的 image 重新產生完整清單，並請法務確認。

## A. 隨 rca-engine 打包的 Python 套件

| 元件 | 授權 | 用途 |
|---|---|---|
| Python | PSF License | 執行環境 |
| FastAPI | MIT | API |
| Starlette（FastAPI 相依） | BSD-3-Clause | API |
| Uvicorn | BSD-3-Clause | ASGI server |
| Pydantic | MIT | 資料驗證 |
| httpx | BSD-3-Clause | 查詢 Prometheus／Loki |
| PyYAML | MIT | 設定檔 |
| pytest（僅開發／測試） | MIT | 測試 |

**義務：** 散布時須附上上述授權全文及著作權聲明（MIT／BSD／Apache-2.0）。可在 image 中放入 `licenses/` 目錄，或用 `pip-licenses --with-license-file` 自動產生。

## B. 以獨立容器搭配使用（未修改、未打包進 system-tool）

| 元件 | 授權 | 注意事項 |
|---|---|---|
| Prometheus | Apache-2.0 | 寬鬆授權，可商用 |
| Grafana | **AGPL-3.0** | 見下方說明 |
| Grafana Loki | **AGPL-3.0** | 見下方說明 |
| Grafana Tempo（規劃中） | **AGPL-3.0** | 同上 |
| Ollama | MIT | 可商用 |
| Qwen3 模型權重（例如 qwen3:4b） | Apache-2.0 | 可商用；如改用其他模型，須另行確認該模型的授權 |

### 關於 AGPL-3.0（Grafana／Loki／Tempo）

- system-tool 的 RCA Engine 透過 **HTTP API** 查詢 Loki，與 Grafana／Loki **不連結（link）、不修改其原始碼**，並以官方 Docker image 作為獨立程式執行。這是降低 AGPL 影響的常見做法。
- **不要修改** Grafana／Loki 的原始碼後再提供給客戶或對外提供服務。若確實修改了，就必須依 AGPL 公開修改後的原始碼。
- 建議由客戶自行從官方來源拉取這些 image（`docker-compose.yml` 就是這樣做的），system-tool 的安裝包不重新散布它們的二進位檔。
- 「Grafana」「Loki」是 Grafana Labs 的商標。行銷時請寫成「支援／整合 Grafana」，不要暗示是官方產品。
- 實際的法律風險，請以律師意見為準。
