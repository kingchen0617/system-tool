# 授權機制說明（v0.3）

system-tool 採**地端安裝 + 線上授權**：客戶把系統裝在自己的環境，監控資料不外流；授權則定期向你（供應商）的授權伺服器啟用與續驗。Enterprise 客戶可以改用**離線授權**。

```
 客戶環境（地端）                                       你的環境
┌──────────────────────────────┐                ┌───────────────────────────┐
│ rca-engine                   │  HTTPS          │ license-server            │
│  └ License Client ───────────┼── activate ───▶ │  ├ 驗證 License Key 簽章   │
│     ├ 驗證 Ed25519 簽章（公鑰）│ ◀─ activation ─ │  ├ 檢查撤銷 / 到期 / 台數   │
│     ├ 租期 7 天 + 寬限 14 天   │    token        │  ├ 簽發 activation token    │
│     └ 依方案開關功能 / 上限    │                 │  └ SQLite + 稽核紀錄       │
└──────────────────────────────┘                │  私鑰只存在這裡 🔒          │
                                                 └───────────────────────────┘
```

## 方案（Edition）

| 方案 | 受監控服務 | 啟用台數 | 功能 |
|---|---|---|---|
| **Community**（無授權／授權失效時） | 5 | – | 規則式 RCA、console 通知 |
| **Professional** | 50 | 2 | ＋ AI RCA（Ollama／Qwen）、Slack／LINE／Email 通知、變更關聯、維護時段 |
| **Business** | 200 | 3 | ＋ RCA 回饋資料集、歷史事件檢索 |
| **Enterprise** | 不限 | 10 | ＋ 雲端 LLM、AWS Connector、SSO、HA、**離線授權** |

- 上限和功能都寫在 License Key 裡，簽發時可以個別調整，例如 `--max-services 80`、`--extra-feature aws_connector`。
- 預設值在 `license-server/app/editions.py`。
- 超過服務數上限時，依重要度（criticality）和拓撲順序挑選要監控的服務，其餘不監控，並在首頁和 `/license` 顯示警告。
- 模擬情境（`/test/incident`、`cli evaluate`）不受服務數上限限制，方便客戶試用。

## 授權狀態

| 狀態 | 意思 | 生效方案 |
|---|---|---|
| `valid` | 正常 | 授權方案 |
| `grace` | 授權已到期，或超過租期仍連不上授權伺服器，處於寬限期 | 授權方案（並顯示警告） |
| `expired` | 寬限期也過了 | Community |
| `revoked` | 授權已被你撤銷 | Community |
| `unactivated` | 有 License Key，但還沒完成線上啟用，或啟用台數已滿 | Community |
| `invalid` | 簽章錯誤、啟用資料被複製到其他主機，或系統時間被往回調 | Community |
| `unlicensed` | 沒有設定 License Key | Community |

授權失效時**只會降級，不會停止監控或刪除資料**，避免客戶的生產環境因授權問題失去監控。

## 防護設計

- **Ed25519 數位簽章**：License Key 和 activation token 都由私鑰簽章，客戶端只有公鑰（`rca-engine/app/license_keys.py`），無法自行產生或修改授權。公鑰刻意不開放用環境變數覆寫。
- **綁定主機**：activation token 綁定 `instance_id` 加上主機指紋（machine-id 的雜湊值）。把啟用資料複製到其他主機會失效。
- **啟用台數**：由授權伺服器控管。30 天沒有續驗的啟用會自動釋放名額；換主機時可先執行停用（`POST /license/deactivate`）。
- **撤銷**：在授權伺服器撤銷後，客戶下次續驗（最多 12 小時）就會降級。
- **防時間回撥**：本機記錄看過的最晚時間。系統時間往回調超過 24 小時，授權就判定無效。
- **稽核紀錄**：
  - 授權伺服器的 `audit` 表：簽發、啟用、續驗、拒絕、撤銷。
  - 客戶端的 `data/license-audit.jsonl`：每次續驗的結果。
- **隱私**：續驗只傳送授權編號、instance ID、主機指紋雜湊、主機名稱和版本，**不傳任何監控資料**。只有客戶設定 `TELEMETRY_OPT_IN=true` 時，才會附上服務數、事件數等使用量。

> ⚠ **限制：** rca-engine 目前以 Python 原始碼出貨，有心人可以直接改程式碼繞過檢查。數位簽章能防的是「偽造授權」，防不了「修改程式」。真正的約束來自合約（`EULA.md` 第 3 條禁止規避），加上技術手段提高門檻。正式出貨前建議：
> 1. 只提供 Docker image，不提供原始碼 repo。
> 2. 用 Nuitka 或 Cython 把 `licensing.py`、`engine.py` 等核心模組編譯成二進位。
> 3. 對 image 做簽章（cosign）。

---

## 賣方操作：架設授權伺服器

```bash
cd license-server
cp .env.example .env          # 設定 ADMIN_TOKEN（長隨機字串）

# 1) 產生簽章金鑰（只做一次！私鑰請離線備份）
docker compose run --rm license-server python -m app.admin keygen
#    → 把印出的公鑰貼進 rca-engine/app/license_keys.py 的 VENDOR_PUBLIC_KEYS，再重新 build 客戶版

# 2) 啟動（前面請放 Nginx / Cloudflare 提供 HTTPS，例如 https://license.你的網域）
docker compose up -d --build
```

### 簽發與管理授權

```bash
# CLI（在 license-server 容器內，或本機 cd license-server 後執行）
python -m app.admin issue --customer "ABC 科技" --edition professional --days 365
python -m app.admin issue --customer "某銀行" --edition enterprise --days 365 --offline -o bank.lic
python -m app.admin issue --customer "XYZ" --edition business --max-services 300 --extra-feature aws_connector
python -m app.admin list
python -m app.admin show ST-PRO-2026-00001      # 含啟用主機與稽核紀錄
python -m app.admin revoke ST-PRO-2026-00001 --reason "未續約"
python -m app.admin inspect bank.lic             # 解開並驗證任何 token
```

也可以用管理 API（需要 Header `Authorization: Bearer <ADMIN_TOKEN>`）：`POST /admin/licenses`、`GET /admin/licenses`、`GET /admin/licenses/{id}`、`POST /admin/licenses/{id}/revoke`、`GET /admin/audit`。

### 金鑰管理

- 私鑰 `secrets/license_private.pem` 遺失 → 無法再簽發授權；外洩 → 任何人都能偽造授權。請加密備份並限制存取。
- **換金鑰的步驟：**
  1. 產生新的金鑰。
  2. 把新的公鑰**加入** `VENDOR_PUBLIC_KEYS`（舊的先保留），然後出新版。
  3. 等客戶都升級到新版後，改用新金鑰簽發授權。
  4. 最後才從清單移除舊的公鑰。

## 客戶操作：啟用授權

```bash
# 方法 1：寫在 .env
LICENSE_KEY=ST1.xxxxx...
LICENSE_SERVER_URL=https://license.你的網域

# 方法 2：系統啟動後再設定
curl -X POST localhost:8000/license -H 'Content-Type: application/json' \
     -H 'X-Admin-Token: <ENGINE_ADMIN_TOKEN>' -d '{"license_key":"ST1.xxxxx..."}'

# 方法 3：CLI
docker compose exec rca-engine python -m app.cli license activate ST1.xxxxx...

# 查看狀態
curl localhost:8000/license
docker compose exec rca-engine python -m app.cli license status
```

**離線授權：** 把 `.lic` 檔的內容放進 `data/license.key`（或設定 `LICENSE_KEY`）即可，不需要設定 `LICENSE_SERVER_URL`。

**換主機：** 先在舊主機執行 `POST /license/deactivate`，或 `python -m app.cli license deactivate` 釋放名額，再到新主機啟用。
