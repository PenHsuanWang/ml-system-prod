# 通用平台術語與責任界線

本表為 PBP-SAD-001 v1.3 的術語基線。正式文件以軟體能力、資源與契約描述平台，不限定產業或產品場景。原始參考資料中的場景詞保留來源語意，不能據此推導平台功能。

| 術語 | 本專案定義 | 實際位置／界線 |
| --- | --- | --- |
| Application Factory Pattern | 應用建立函式模式；呼叫函式以建立已配置的 ASGI application。 | `server.create_app()` 回傳 FastAPI application 並定義 lifespan；程序啟動後才進入 lifespan 建立連線等資源。文件保留英文全名或用「應用建立函式」，避免單獨稱為「工廠」。 |
| Uvicorn `--factory` | 告知 Uvicorn 將指定 import target 視為應用建立函式。 | `uvicorn server:create_app --factory`；`server.py` 亦以 `factory=True` 呼叫 Uvicorn。它不是產品的設定階層或組織結構。 |
| Field `default_factory` | 為欄位建立預設值的 callable，通常用來避免共享可變預設物件。 | dataclass 與 Pydantic 欄位使用；與 Application Factory 是不同層級的建立機制。保留 Python 原名。 |
| Composition Root | 建立實作、選擇 adapter、注入抽象與管理啟動組裝的外緣。 | `bootstrap/` 與入口；不是每次 HTTP request 必經的服務層。 |
| Platform Capability | 可選的共用技術能力與政策。 | lifecycle、HTTP、安全、任務、資料存取、匯出與運算契約；現有 `identity/jobs/exports/kernels` API 名稱維持不變。 |
| Application Module／Capability | 按需求增加、跨層協作的功能。 | 同一 `<capability>` 名稱分布在需要的 domain/application/api 等層；由 bootstrap initializer 明確選入。不要求每個應用建立所有層或空介面。 |
| Module Definition | 一項能力的啟動、依賴、路由與註冊描述。 | `ModuleDefinition`、`ModuleContext`、`ModuleExports`、`ModuleCatalog` 位於 `bootstrap/components.py`；不是另一個固定 feature-package 層。 |
| Domain Contract | 不依外層框架的模型、政策、策略或 port。 | `src/domain/`；範本先提供共用能力契約，未來應用自行加入產品 entity 與不變條件。沒有預載產品模型。 |
| Application Use Case | 協調 domain 契約並執行一項應用操作的入口。 | `src/application/`；應用定義自己的輸入語意、權限、交易、流程及失敗契約。 |
| Platform Runtime Services | 技術性 runtime 支援。 | `src/services/` 的 lifecycle/resource/health/logging/concurrency；不決定產品規則，也不是 API 與 application 之間的固定中介層。 |
| Tenant | 授權及資料隔離邊界。 | `tenant_id` 不自動對應任何產品 entity、外部識別或設定繼承。實際映射與其權限政策由應用明確定義。 |
| Solution Kernel | 可選、具版本的運算策略契約。 | `SolutionKernel.spec` 與 `async execute(payload, context)`；不要求使用 AI、模型或規則，也沒有預載解法。`kind` 是技術 metadata。 |
| Plugin | 明確選入、實作 domain 策略的受信任 Python 程式。 | `inference/heuristics/interceptors` 為空擴充 package；loader 驗證 binding 與 async contract，不提供 discovery、artifact 驗簽或 sandbox。 |
| Configuration Resolution／Hierarchy | 只有應用需求成立後才定義的設定解析與可選繼承政策。 | `application/hierarchy.py` 尚無 resolver；不預設 scope、precedence、merge、conflict 或 store。 |
| Workflow／Orchestration | 應用按自身契約定義的步驟、錯誤及補償。 | 平台只提供現有 KernelJobHandler 等共用委派；不指定所有產品的固定處理順序。 |
| Capacity Lease | 受限資源的並行容量協調。 | memory／Redis coordinator；不自動提供應用資源互斥、exactly-once 或副作用原子性。 |
| Task Envelope／Payload | envelope 承載 job metadata 與 payload；payload 是特定 handler 的輸入。 | worker adapter 解碼 envelope 並查找 handler；各 handler／use case 須定義自己的 schema、語意及授權。型別註記不是通用 runtime validator。 |
| Process／Request／Job Scope | 不同資源與上下文的生命週期。 | API 與 Taskiq 子程序各自建構 runtime；跨程序狀態透過外部服務協調，不透過 Python 全域物件共享。 |

目前實作與可選擴充點以 [設計規格](software-design.md) 及 [模組規範](module-spec.md) 為準。歷史設計決策見 [ADRs](adr/README.md)，來源材料見 [references](references/README.md)。
