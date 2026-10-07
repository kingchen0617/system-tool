# system-tool 產品流程圖

> 圖表用 Mermaid 繪製，GitHub 會直接渲染。
>
> **一句話說明：** 系統出問題時，system-tool 告訴你「**為什麼**異常、真正故障在哪、證據是什麼、下一步怎麼處理」。
>
> 產品名稱：**AI Incident Investigation & RCA Platform**

## 目錄

1. [處理一個事故的流程](#1-處理一個事故的流程)
2. [目標架構（五層）](#2-目標架構五層)
3. [能力 ①：AI 調查迴圈（Agentic Investigation）](#3-能力-ai-調查迴圈agentic-investigation)
4. [能力 ②：假設引擎（Hypothesis Engine）](#4-能力-假設引擎hypothesis-engine)
5. [能力 ③：故障傳播圖（Failure Propagation Graph）](#5-能力-故障傳播圖failure-propagation-graph)
6. [能力 ④：設定層 RCA（Configuration RCA）](#6-能力-設定層-rcaconfiguration-rca)
7. [能力 ⑤：變更關聯（Change Intelligence）](#7-能力-變更關聯change-intelligence)
8. [能力 ⑥：RCA 學習迴圈](#8-能力-rca-學習迴圈)
9. [目前已實作的處理流程](#9-目前已實作的處理流程)
10. [Roadmap](#10-roadmap)

---

## 1. 處理一個事故的流程

system-tool 的工作是 **Understand → Diagnose → Resolve**：理解異常、診斷根因、給出處置建議。

```mermaid
flowchart TB
    S1["發現異常"] --> S2["建立 Incident"]
    S2 --> S3["理解 Service Graph"]
    S3 --> S4["追查依賴鏈"]
    S4 --> S5["交叉驗證<br/>Metrics / Logs / Traces / Changes"]
    S5 --> S6["建立多個 Root Cause 假設"]
    S6 --> S7["主動查證 / 排除"]
    S7 --> S8(["Root Cause + 信心度<br/>+ 證據 + 建議處置"])
```

---

## 2. 目標架構（五層）

```mermaid
flowchart TB
    DS["<b>DATA SOURCES</b><br/>Prometheus · Loki · ELK · OTel · CloudWatch<br/>Linux · AWS · K8s · GitHub · Config"]
    IE["<b>INCIDENT ENGINE</b><br/>Detection · Correlation · Dedup"]
    CG["<b>CAUSAL SERVICE GRAPH</b><br/>Service → Dependency → Failure Mode"]
    AG["<b>AI INVESTIGATION AGENT</b><br/>Observe → Hypothesis → Query Tools<br/>→ Evidence → Reject / Confirm → Repeat"]
    RC["<b>RCA ENGINE</b><br/>Root Cause · Confidence · Evidence<br/>Failure Propagation · Recommended Action"]
    KN["<b>INCIDENT KNOWLEDGE</b><br/>Human Feedback → Learning Dataset"]

    DS --> IE --> CG --> AG --> RC --> KN
    KN -.->|"歷史事件 / RAG<br/>讓下次更準"| AG
    RC -->|"通知負責人"| N(["Slack / LINE / Email"])
```

---

## 3. 能力 ①：AI 調查迴圈（Agentic Investigation）

AI 要會「調查」：自己決定下一步查什麼，而不是只讀一次資料就下結論。

- **目前：** 收集資料 → 一次把 context 丟給 Qwen → Qwen 判斷。
- **目標：** AI 自己決定「下一步要查什麼，才能證明或推翻我的假設」，並留下完整的調查紀錄（Investigation Trail）。

```mermaid
flowchart TD
    A(["Incident：depositApply timeout"]) --> B["查 payment-api P95"]
    B -->|"異常"| C["查依賴：Redis / MariaDB / Proxy"]
    C --> D{"Redis？"}
    D -->|"正常 ✓ 排除"| E{"MariaDB？"}
    E -->|"正常 ✓ 排除"| F{"Proxy CPU / RAM / FD？"}
    F -->|"正常 ✓ 排除資源不足"| G{"Proxy CONNECT latency？"}
    G -->|"異常 ⚠"| H{"Provider upstream latency？"}
    H -->|"異常 ⚠"| I{"其他 Provider？"}
    I -->|"正常"| J{"最近有 deployment？"}
    J -->|"無"| K(["結論：Provider A upstream degradation<br/>信心度 94%"])

    subgraph LOOP["每一步的內部迴圈"]
        direction LR
        L1["提出假設"] --> L2["選擇工具"] --> L3["查資料"] --> L4["更新假設"] --> L1
    end

    K --> TR["Investigation Trail<br/>（每一步的工具、參數、結果都留紀錄）"]
    X["Ollama 掛掉 / 輸出不合格"] -.->|"安全退路"| R["Rule RCA（不會因 AI 掛掉而失效）"]
```

---

## 4. 能力 ②：假設引擎（Hypothesis Engine）

不直接說「Root Cause = Proxy」，而是先列出多個假設，再用證據逐步更新機率。

```mermaid
flowchart LR
    H0["<b>初始假設</b><br/>A Provider upstream 72%<br/>B Proxy 資源耗盡 18%<br/>C 網路路徑 7%<br/>D 應用程式回歸 3%"]
    Q1["查 Proxy CPU<br/>→ 23%（正常）"]
    H1["A 78%<br/>B 8%"]
    Q2["查 FD 使用率<br/>→ 31%（正常）"]
    H2["A 84%<br/>B 3%"]
    Q3["查 upstream P95<br/>→ 42 秒（異常）"]
    H3(["<b>A 94%</b><br/>下 RCA 結論"])

    H0 --> Q1 --> H1 --> Q2 --> H2 --> Q3 --> H3
```

```mermaid
stateDiagram-v2
    state "提出假設" as Hypo
    state "查證" as Query
    state "更新機率" as Update
    state "結論" as Done
    state "證據不足" as Unknown
    [*] --> Hypo
    Hypo --> Query: 選擇最能區分假設的工具
    Query --> Update: 取得證據
    Update --> Query: 第一名未明顯領先
    Update --> Done: 第一名明顯領先
    Update --> Unknown: 工具用盡或逾時
    Done --> [*]
    Unknown --> [*]: 回報 unknown 與候選清單
```

---

## 5. 能力 ③：故障傳播圖（Failure Propagation Graph）

現在的拓撲只有 `depends_on`，也就是「A 依賴 B」。目標是加上 **failure_modes**，讓系統理解「B 用什麼方式故障時，A 會出現什麼症狀」。

```yaml
# 規劃中的 topology 擴充（v0.5）
payment-api:
  depends_on: [redis, mariadb, proxy]
  failure_modes:
    redis:   [timeout, connection_error]
    mariadb: [connection_exhaustion, slow_query, lock_wait]
    proxy:   [connect_timeout, tls_error, fd_exhaustion]
```

**Cause → Propagation → Symptom** 範例：

```mermaid
flowchart TD
    C1["<b>Cause</b><br/>Provider latency ↑"]:::cause
    P1["Proxy CONNECT timeout"]:::prop
    P2["Job retry"]:::prop
    P3["Queue backlog"]:::prop
    P4["Worker saturation"]:::prop
    S1["<b>Symptom</b><br/>depositApply timeout"]:::symptom

    C1 --> P1 --> P2 --> P3 --> P4 --> S1

    classDef cause fill:#fde2e1,stroke:#c62828,color:#000
    classDef prop fill:#fff4e0,stroke:#ef6c00,color:#000
    classDef symptom fill:#e3f2fd,stroke:#1565c0,color:#000
```

---

## 6. 能力 ④：設定層 RCA（Configuration RCA）

產品核心要求：**不能只說「Proxy timeout」，要分辨是資源不足、設定錯誤、軟體限制，還是上游問題。**

```mermaid
flowchart TD
    A(["症狀：Nginx 502 / Proxy timeout"]) --> B{"上游服務異常？<br/>（Provider / DB / Redis）"}
    B -->|"是"| U["<b>上游問題</b><br/>例：Provider P95 800ms → 42s"]
    B -->|"否"| C{"主機資源滿載？<br/>CPU / RAM / 頻寬"}
    C -->|"是"| R["<b>資源不足</b><br/>建議擴容 / 分流"]
    C -->|"否"| D{"軟體限制達上限？<br/>max_children / FD / workers"}
    D -->|"是"| G["<b>設定過低</b>"]
    D -->|"否"| E{"最近有部署 / 設定變更？"}
    E -->|"是"| F["<b>變更回歸</b><br/>建議 rollback"]
    E -->|"否"| X["unknown<br/>列出候選並請工程師確認"]

    G --> G1["例 1：PHP-FPM active = max_children 50<br/>CPU 35%、RAM 剩 12GB<br/>→ pm.max_children 50 → 100"]
    G --> G2["例 2：Proxy FD 64,820 / 65,535<br/>CPU、RAM、頻寬都正常<br/>→ 調高 max_filedescriptors"]
```

---

## 7. 能力 ⑤：變更關聯（Change Intelligence）

很多事故來自部署、設定、防火牆、DB schema、套件更新或基礎設施變更。system-tool 會把**變更**和**症狀**排成時間軸，判斷兩者的因果關係。

```mermaid
flowchart LR
    T0["14:00<br/>deployment v2.14"]:::change --> T1["14:02<br/>PHP exception ↑"] --> T2["14:03<br/>DB query ↑"] --> T3["14:04<br/>API P95 ↑"] --> T4["14:05<br/>5xx ↑"]
    T4 --> R(["Deployment Regression<br/>信心度 96%"]):::result

    subgraph SRC["變更來源"]
        direction TB
        G1["GitHub / GitLab / Jenkins<br/>（POST /changes，已支援）"]
        G2["CloudTrail / AWS Config"]
        G3["Terraform"]
    end
    SRC -.-> T0

    classDef change fill:#ede7f6,stroke:#5e35b1,color:#000
    classDef result fill:#fde2e1,stroke:#c62828,color:#000
```

---

## 8. 能力 ⑥：RCA 學習迴圈

每次事故結束後，請工程師確認 AI 判斷得對不對。累積下來的資料，會讓之後的判斷越來越準。

```mermaid
flowchart LR
    A["AI 判斷<br/>Root Cause + 證據"] --> B{"工程師確認"}
    B -->|"✓ 正確"| D[("RCA 學習資料集")]
    B -->|"✗ 錯誤"| C["填寫真正原因<br/>例：Squid workers 設定過低"]
    C --> E["處置<br/>workers 4 → 8"]
    E --> F{"是否恢復？"}
    F --> D
    D --> G["歷史事件 RAG<br/>相似事件比對"]
    D --> H["RCA 準確率評估<br/>Top-1 / Top-3 / 誤報率"]
    G --> A
    H --> A
```

資料集內容：Symptoms ＋ Topology ＋ Metrics ＋ Logs ＋ Configuration ＋ Changes ＋ AI Hypothesis ＋ Actual Root Cause ＋ Engineer Action ＋ Recovery Result。

> 目前已有 `POST /incidents/{id}/feedback` 收集回饋；評估工具是 `python -m app.cli evaluate`。

---

## 9. 目前已實作的處理流程

這張圖是**現在程式實際的運作方式**，方便和上面的目標架構對照。

```mermaid
flowchart TD
    subgraph ENG["rca-engine（每 60 秒）"]
        direction TB
        D1["Prometheus / Loki<br/>查詢規則序列"] --> D2["偵測<br/>固定門檻（持續 for 秒）<br/>＋ 穩健 z-score（median + MAD + guard band）"]
        D2 --> D3["依拓撲分組異常服務"]
        D3 --> D4{"多訊號評分"}
        D4 -->|"< 3"| X1["抑制"]
        D4 -->|"3–4"| X2["Warning（只記錄）"]
        D4 -->|"≥ 5"| D5["建立 Incident"]
        D5 --> D6["白名單工具收集證據<br/>（全部寫入稽核紀錄）"]
        D6 --> D7["規則式 RCA<br/>候選排名 + 信心度"]
        D7 --> D8{"Ollama 可用？"}
        D8 -->|"是"| D9["Qwen 推理<br/>只能引用證據 ID"]
        D9 -->|"驗證失敗 ×2"| D10
        D8 -->|"否"| D10["使用規則式結果"]
        D9 -->|"通過"| D11["RCA 結果"]
        D10 --> D11
    end

    D11 --> N["依負責人通知<br/>Slack / LINE / Email"]
    D11 --> FB["工程師回饋 → 資料集"]
```

---

## 10. Roadmap

```mermaid
flowchart LR
    V1["v0.1 ✅<br/>MVP<br/>偵測 + 規則 RCA + 通知"] --> V2["v0.2～v0.3 ✅<br/>證據 ID 防幻覺<br/>多候選 · 穩健 baseline"]
    V2 --> V4["v0.4<br/><b>Web Console + Linux Agent</b><br/>3 分鐘新增主機 · 自動探索服務<br/>自動推測依賴"]:::next
    V4 --> V5["v0.5<br/><b>Agentic Investigation</b><br/>假設引擎 · AI 自選工具<br/>Investigation Trail"]
    V5 --> V6["v0.6<br/><b>Causal / Failure Graph</b><br/>Configuration RCA<br/>Change Intelligence"]
    V6 --> V7["v0.7<br/><b>Incident Learning</b><br/>回饋資料集 · RAG<br/>準確率評估"]
    V7 --> V8["之後<br/>Windows / K8s / SNMP / AWS<br/>出貨強化（編譯、簽章 image）"]

    classDef next fill:#e8f5e9,stroke:#2e7d32,color:#000
```

v0.4 詳細規格見 [web-console-spec.md](web-console-spec.md)。

**Demo 目標：** 現場丟一個故障給系統，讓客戶看著 system-tool 一步一步查到根因。
