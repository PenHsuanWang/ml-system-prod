# 通用藍圖設計採納與功能狀態

更新日期：2026-10-24。現行設計保留原始架構圖的分層、package／module 命名及組裝原則，將產品專屬語意移出範本規範。原始文件與架構圖完整保存於 [歷史參考](references/README.md)，用於追溯來源；正式基線為 [software-design.md](software-design.md)、[module-spec.md](module-spec.md) 及現行架構圖。

## 保留的結構與取捨

| 面向 | 保留方式與理由 | 代價／界線 |
| --- | --- | --- |
| 啟動組裝 | 外緣 `bootstrap` 明確建構、選擇與注入實作，`create_app()` 使用 Application Factory 模式。 | 組裝不等於 request-time 中介層；container 不應成為任意查找依賴的 service locator。 |
| 分層命名 | 保留九個 `src` 頂層 package 與入口檔，層內以同名 capability 追蹤跨層功能。 | 同一能力分散於多層，須靠公開契約、命名規則與靜態檢查維護。 |
| 執行關係 | API 與 worker 直接重用 application 使用案例；`services` 提供技術性 runtime 支援。 | 每程序各自持有資源；不把程序內物件視為分散式共享狀態。 |
| 策略與 adapter | `SolutionKernel` 保持中性版本化計算契約，plugins 實作策略，infrastructure 實作 ports。 | 契約名稱不代表已內建演算法；可信 Python 插件不具有 sandbox 隔離。 |
| 共用支援 | `config`、純工具與中性 DTO 可供多層按需使用。 | 共用 package 不得反向依賴應用或替具體產品保存全域狀態。 |
| 應用擴充 | 不建立預設產品識別、設定階層、資料模型或流程；保留 `hierarchy.py` 等明確擴充位置。 | 目錄存在不代表功能已完成；首個應用須定義自己的契約與測試。 |

## 現行實作與擴充狀態

| 能力 | 現行實作 | 尚需由應用或後續平台工作定義 |
| --- | --- | --- |
| lifespan／並行 | 具型別初始化、依賴排序、回滾、反序清理、async 任務與受控 thread executor。 | 通用 process／GPU executor、硬性中止阻塞程式。 |
| HTTP／使用案例 | 路由、transport schema、health、API／worker 共用 service。 | 具體產品 endpoint 與輸入／輸出模型。 |
| 任務調派 | memory 或 Taskiq／RabbitMQ、Redis job 快照、snapshot-before-ack、任務名稱白名單。 | outbox、跨儲存交易、自動業務重試、永久結果與 exactly-once 保證。 |
| 資源協調 | memory 或 Redis capacity lease、owner token、續租、fencing token。 | 對外部寫入的 fencing 執行、持久冪等與復原協議。 |
| 身分／資料隔離 | 本地帳號、Argon2、JWT／Mongo session、permission、tenant／owner policy。 | 完整 RBAC／ABAC、企業 IAM、逐 kernel entitlement、密碼復原與完整稽核。 |
| 資料庫／匯出 | MongoDB 身分與 session 儲存、repository ports、受控 CSV／JSONL 匯出。 | 產品資料 schema、查詢、保存年限與一致性要求。 |
| Solution Kernel | 契約、版本驗證、授權、JSON payload 大小檢查、容量管理與明確插件載入。 | 演算法、通用的逐 kernel schema registry、模型 artifact 驗證或動態插件認證。 |
| 流程／設定 | `TaskRegistry` 依名稱解析 handler；`KernelJobHandler` 重新授權後執行 kernel。 | 設定合併階層、策略選擇、完整多步驟 pipeline；`hierarchy.py` 目前僅為擴充點。 |
| 可觀測性 | API logging、health／readiness 與既有測試證據。 | 完整 metrics／tracing exporter、worker readiness／heartbeat 及目標部署的 SLO／HA 驗收。 |

## 輸入驗證責任

目前 queue 路徑解碼傳輸 envelope、解析已註冊 handler，再交由 application 使用案例執行；這不代表各應用的 payload 已具有完整 schema 驗證。現行 kernel API 接受中性 JSON payload，`KernelService` 檢查名稱、版本、授權與資料大小，沒有逐 kernel 的型別化輸入／輸出 registry。

新增功能時，API schema 應驗證 HTTP transport；application handler 應驗證自身使用案例輸入，domain 應維持資料不變量及政策。這是擴充規範，並非宣稱本次新增了尚未實作的 schema 引擎。通用訊息 adapter 不應內建任何產品資料模型。

## 交付與驗證界線

預設應用清單及演算法為空；可選身分模組仍屬通用平台能力。實體 package、公開契約、型別、分層邊界、程序生命週期及 Docker／RabbitMQ 整合的實際結果見 [validation.md](validation.md)。工程驗證不等同任何產品或目標環境已獲生產核准。原始資料與歷史驗證證據保留原文，不以新規範改寫歷史結果。

舊新路徑見 [package-migration.md](package-migration.md)，決策的取代關係見 [ADRs](adr/README.md)，術語與命名界線見 [glossary.md](glossary.md)。
