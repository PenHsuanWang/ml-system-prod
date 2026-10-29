# Package、模組命名與功能規範

本規範定義通用 Python Backend 的實體分層與 package 命名，對應 [軟體架構設計規格](software-design.md) v1.3 與專案內更新版架構圖。`bootstrap/` 負責啟動組裝，`src/` 下的 `api`、`services`、`application`、`domain`、`plugins`、`infrastructure` 為同層 package；`config`、`utils`、`shared_models` 是按需使用的支援 package。新增應用沿這些層增加對應模組，不再建立 `backend/modules/<name>/` 或複製一套 feature module 目錄。

規範中的 `<capability>`、`<task_name>` 等是待替換佔位名稱。範本提供身份、資料權限、任務、運算與匯出的共通契約；應用的模型、政策、流程與資料來源由新增能力明確定義。`create_app()` 使用軟體工程的 Application Factory Pattern，負責建立 FastAPI application；此處 factory 只指程式建構方式。術語定義見 [術語與範圍](glossary.md)。

## 實體目錄與責任

```text
server.py                         # ASGI／FastAPI 執行入口
worker.py                         # Taskiq 背景工作入口
bootstrap/
  config.py                       # 載入部署設定
  container.py                    # 程序 runtime 容器與共通使用案例建構
  plugin_loader.py                # 明確核准的插件組裝
  wiring.py                       # 選用能力、任務與 adapter 組裝
  components.py                   # 啟動描述與依賴排序
  identity.py                     # 本地身份能力組裝
  worker.py                       # worker 啟動／關閉組裝
src/
  api/                            # inbound transport 邊界
  services/                       # 技術性 runtime 支援
  application/                    # 使用案例與流程協調
  domain/                         # 純核心模型、政策與 ports
  plugins/                        # 演算法策略實作
  infrastructure/                 # outbound adapters
  config/                         # 通用設定與設定型別
  utils/                          # 純共用工具擴充位置
  shared_models/                  # 中性跨邊界型別擴充位置
```

`bootstrap/` 下的輔助檔案服務於組裝，不能因此變成 application 的依賴。每個程序獨立建立容器、連線池、模型與 executor；Python 全域變數不構成跨程序共用狀態。`server.py` 與 `worker.py` 可以使用同一組 application service，worker 不透過內部 HTTP 呼叫 API。

### `bootstrap/`：Composition Root

| 模組 | 功能規範 |
| --- | --- |
| `config.py` | `load_settings(**overrides)` 建立並驗證 `Settings`，實際由入口使用；再將設定傳入建構式，import 本身不開連線。 |
| `container.py` | 建構 kernel／export 等通用使用案例並定義程序 Runtime；供入口與 HTTP DI 邊界使用，不傳入 domain／application 當 service locator。 |
| `plugin_loader.py` | `PluginBinding`／`load_approved_plugins()` 驗證並匯集明確選入的可信 kernel；由 `components.py` 實際使用。不接受 HTTP、資料庫或 queue 中的任意 import 字串／pickle。 |
| `wiring.py` | 明確選擇能力與 dispatcher，建立依賴圖，將具體 adapter 注入 application。 |
| `components.py` | 定義啟動描述、公開服務、依賴與註冊內容；檢查重名、缺少依賴與循環，按拓撲順序啟動並反向關閉。 |
| `identity.py` | 組裝本地身份 repository、密碼與 JWT adapter、application facade 與 API router。 |
| `worker.py` | 組裝 Taskiq worker 資源、處理 startup rollback 與 shutdown；不包含業務 handler 邏輯。 |

組裝是初始化行為，不是每個 request 必經的一個服務層。使用案例應直接持有所需的窄介面；不在執行時回頭呼叫 wiring 取得 adapter。

### `src/api/`：Inbound Control

| SVG 模組 | 功能規範 |
| --- | --- |
| `routers.py` | 共通路由組裝／公開入口；實際能力路由可放在 `<capability>/`。處理 HTTP 語意，委派 application。 |
| `schemas.py` | 共用 transport schema 入口；能力專屬 request／response 留在該能力的 API package。HTTP schema 不替代 domain entity。 |
| `dependencies.py` | 在 FastAPI 邊界取得 runtime，轉成具型別的 application service／port；限制容器取用位置。 |
| `auth.py` | HTTP 身份驗證／permission 相依的公開入口；驗證規則與資料 policy 仍由 application／domain 保護。 |
| `exceptions.py` | 統一將受控錯誤轉換為狀態碼、安全錯誤內容與 request ID；不輸出密碼、token 或 adapter 原始例外。 |

router 不建立 DB／broker client、不直接 query repository、不選擇具體 plugin，也不編排跨步驟業務。已登入不等於具有操作權限；tenant／owner 篩選必須落實在資料存取與使用案例邊界。

### `src/services/`：Platform Runtime Services

| SVG 模組 | 功能規範 |
| --- | --- |
| `lifecycle.py` | `register_execution_cleanup()` 向 `AsyncExitStack` 登記關閉順序：dispatcher → active operations → physical threads；`server.py`／`bootstrap.worker` 擁有實際 lifespan 與資源。 |
| `resource_manager.py` | 管理共用 MongoDB、thread runner、容量協調等資源的建立／關閉；具體 adapter 位於 infrastructure，部署選擇與注入位於 bootstrap。 |
| `health.py` | 不含 HTTP 的 readiness 檢查；反映啟用的必要依賴。`api/health.py` 定義 liveness／readiness HTTP 路由，由 `api/routers.py` 掛載，檢查不觸發業務工作。 |
| `observability.py` | API／worker 共用結構化記錄與 request／job context；不記錄秘密及完整業務 payload。日誌不是不可竄改稽核帳本。 |
| `concurrency.py` | 有界 thread 執行、程序內 admission／drain 與取消語意；不承諾強制停止 Python thread。 |

此層的 `services` 指技術性 runtime 能力；應用政策、流程與資料選擇規則由 application／domain 持有。`services` 與 `application` 是同層；application 可以經 domain port 使用需要的 runtime 支援，不必經過固定的 `api → services → application` 呼叫鏈。

### `src/application/`：Use Cases

| SVG 模組 | 功能規範 |
| --- | --- |
| `use_cases.py` | 公開 `JobService`、`IdentityService`、`KernelService`、`ExportService` 與 `KernelJobHandler`；能力專屬使用案例放在 `<capability>/`，透過建構式注入 domain ports。 |
| `registry.py` | `TaskRegistry` 的明確註冊與名稱查找；註冊表不載入任意程式，也不依應用資料自動選擇策略。 |
| `hierarchy.py` | 可選且尚未實作的設定解析擴充點；若應用需要解析器，須先定義輸入識別、優先序、衝突、授權與版本契約。範本不預設 scope、層級或合併規則，新增應用無須實作此模組。 |
| `orchestration.py` | `KernelJobHandler` 將背景工作委派給 kernel 使用案例，重新解析有效身份；其他應用協調按需加入，經 ports 注入。此模組未定義固定的多步驟流程。 |

application 管理使用案例、輸入驗證、動作授權、錯誤與完成語意；domain 管理純政策與不變條件。application 不 import API、bootstrap、具體 plugins 或 infrastructure。多個入口應重用使用案例，而非複製規則。

若新增多步驟流程，必須依其操作定義資源所有權／續租、輸入有效性、步驟順序、結果保存、重送／冪等、部分失敗補償與 cleanup。只把方法串起來，不構成可交付的可靠性契約。

### `src/domain/`：Pure Core

| SVG 模組 | 功能規範 |
| --- | --- |
| `models.py` | 聚合公開 `Principal`、job／kernel／export 模型；實際定義留在各能力 package，不移到 shared_models 逃避 ownership。 |
| `strategy.py` | 定義通用可插拔運算契約 `SolutionKernel` Protocol；實作位於 plugins 或適當 adapter。其他 strategy／hook 依實際需求增加。 |
| `ports.py` | 聚合公開 `IdentityRepository`、`PrincipalAuthenticator`、`ThreadRunner`、`JobDispatcher`、`TaskHandler`、`TaskResolver`、`CapacityCoordinator`、`ExportSource`。只表達需求，不建立連線或選擇技術。 |

domain 可使用標準函式庫、自己的純核心契約及中性的 `shared_models`；不依賴 FastAPI、Pydantic Settings、MongoDB／Redis client、Taskiq、plugins、services、application、infrastructure 或 bootstrap。跨能力資料契約應保持明確、穩定且無副作用；不得靠 domain 轉接至外層實作。

### `src/plugins/`：Pluggable Solutions

| package／module | 功能規範 |
| --- | --- |
| `inference/` | 空擴充 package；可放置依 `SolutionKernel` 契約加入的推論實作，目前沒有模型、訓練或推論演算法。 |
| `heuristics/` | 空擴充 package；可放置依 `SolutionKernel` 契約加入的規則／啟發式實作，目前沒有內建規則。 |
| `interceptors/` | 空擴充 package；新增 hook 前須定義介面、呼叫順序與錯誤語意，目前沒有共通 interceptor 契約或執行鏈。 |

插件實作 domain strategy，使用 domain 資料型別；application 持有抽象，由 bootstrap 注入實作。需要 DB、HTTP、artifact 或模型服務時，透過窄 port 注入，不在策略內自行建立外部 client。演算法依賴的科學套件需審查其副作用、native threads、授權及部署相容性。import 檢查不提供 sandbox。

插件採受信任程式碼的明確選入，無熱更新、第三方套件下載、artifact 驗簽或不受信任程式隔離服務。模型載入／卸載須納入 lifespan；每個 API／worker 程序都可能載入一份模型，配置時須計入記憶體與 GPU 容量。

可信 binding 的具體 API 位於 `bootstrap.plugin_loader`：

- `PluginBinding(name, implementation, contract_version="solution-kernel/v1")`：`name` 固定為 `<capability>.<local_name>`，兩段均符合 `^[a-z][a-z0-9_]*$`；implementation 遵守 `src.domain.strategy.SolutionKernel`。
- `load_approved_plugins(bindings)`：接收 binding iterable，回傳唯讀的名稱 → kernel mapping；拒絕重複名稱、不相容 contract、非 `KernelSpec` metadata、無效版本／kind，以及不可呼叫／同步的 `execute`（只接受 async function 或 async `__call__`）。
- `contract_version` 是 Python 策略契約識別；`KernelSpec.version` 是該 kernel 的實作版本，兩者不可混用。實作版本為 1–64 個字元，以英數開頭，後續允許英數、`.`、`_`、`-`。

`ModuleCatalog` 匯集 `ModuleExports.kernels` 時，實際呼叫上述 validator；應用通常只需輸出本地 kernel 名稱，由組裝加上 namespace。所謂 approved 是 composition root 明確選入程式物件，沒有驗證外部簽章、工件內容、輸出語意或外部核准程序。async 宣告與 metadata 檢查不保證函式內不阻塞、取消、版本語意及結果契約；這些仍需測試與審查。

### `src/infrastructure/`：Outbound Adapters

| package／module | 功能規範 |
| --- | --- |
| `persistence/` | MongoDB resource、identity repository、export source 等資料 adapter；強制 tenant／owner policy，統一轉換專屬資料型別與錯誤。 |
| `messaging/` | disabled（拒絕提交且不啟動背景 task）／memory／Taskiq dispatcher、RabbitMQ publisher、Redis job snapshots；實作 domain ports，不決定業務工作流程。 |
| `locking/` | memory／Redis 容量 lease；owner token、續租與 fencing 是技術協調，外部受保護操作仍須驗證 token，lease 本身不提供 exactly-once。 |
| `downstream/` | 空擴充 package；可放置經明確 port 注入的外部服務 adapter，目前沒有內建外部服務整合或結果發布流程。 |
| `security.py` | 密碼雜湊與 JWT 等本地身份技術 adapter；金鑰由部署設定注入。 |

adapter 可依 domain ports，不反向依賴 API／application／plugins／bootstrap。跨儲存交易、持久冪等與發布原子性須依使用案例明確設計；Redis TTL 工作快照不等同永久結果或 outbox。

### 獨立支援 package

| package／module | 功能規範 |
| --- | --- |
| `src/config/` | `settings.py` 提供通用部署設定，實際使用 `environment.py` 的 `Environment` 與 `constants.py` 的非秘密技術預設值。由 `bootstrap.config`／外層讀取，domain 不透過環境變數隱藏取得設定。 |
| `src/utils/` | `formatting.csv_cell()` 轉換／保護 CSV 欄位，`time_helpers.utc_now_iso()` 格式化 UTC，`validation.is_valid_request_id()` 驗證 request ID；均已有呼叫位置，不持有 runtime、不開連線、不協調業務。 |
| `src/shared_models/` | `types.py` 定義 `Payload`、`JSONValue`、`AsyncCloseable`、`ThreadDrainer`；`schemas.py` 的 `ErrorDetail` 與 `dto.py` 的 `ErrorEnvelope` 由 API 錯誤轉換使用。`context.py` 以標準函式庫 ContextVar 提供 request／job 識別與可還原的 binding，不持有 client 或資源；application 取得關聯 metadata，adapter 在每個 job 執行後還原。型別中性，不放具 ownership 的 entity 或外層 client。 |

SVG 的候選檔名不要求每個空白檔案都加入實作。需要時才建立有明確責任的模組；空 package／擴充點不代表該功能已完成。

## 命名與依賴規則

Port 依真正消費層決定 ownership：Domain 自己需要的業務抽象放 domain；Application workflow 的 executor、publisher、tracker、runtime 等 Protocol 可放 `application/<capability>/ports.py`。Infrastructure／Plugins 仍不得 import application，可依公開純 DTO 實作同形方法，再由 bootstrap 型別檢查 structural conformance。現有 identity/jobs 等 Domain Ports 保留，不要求為新設計遷移舊契約。ML 的正式對照見 [Port Ownership](ml-system-design.md#55-正式-port-ownership)。

- package／Python 模組使用 `snake_case`；class 使用 `PascalCase`；Protocol 依角色命名，不以 `Manager` 包辦無關責任。
- 同一能力跨層採一致名稱，例如 `src/domain/<capability>/`、`src/application/<capability>/`、`src/api/<capability>/`。具體 outbound adapter 按技術分類至 infrastructure，策略按類型放 plugins。
- 不保留 `backend.*` 的相容性轉接 package。程式、測試、設定與文件應使用新的 `src.*`／`bootstrap.*` 路徑。
- import 方向向內：API 使用 application 與 domain；application 使用 domain；plugins／infrastructure 實作 domain 契約；bootstrap 可以組裝外層與內層。
- API 不直接 import infrastructure／plugins；plugins 與 infrastructure 不直接互相依賴；domain 不透過 facade 或 re-export 引入外層。
- `config`／`utils`／`shared_models` 按需使用，不形成固定呼叫鏈，也不能充當繞過依賴規則的中介。
- 在 package 根 `__init__.py` 避免大量重出口、全域建構與 import 時 I/O；公開入口明確列出，避免循環 import。

實際 AST 守門規則以 `scripts/check_boundaries.py` 為準；它驗證靜態 import，不能證明物件 ownership、動態 import、反射、外部副作用或所有 SOLID 特性。

## 跨層能力的註冊與生命週期

啟動描述是一個能力在各層的組裝資訊，不是另一套 feature module 實體目錄。建立 adapter、使用案例、router 與 handler 的組裝程式集中在 `bootstrap/`。描述中的 `name` 是唯一 namespace；`jobs`、`identity`、`kernels`、`exports` 保留給內建共通能力。

`bootstrap.components` 保留 `ModuleDefinition`、`ModuleContext`、`ModuleExports`、`ModuleCatalog` 這組 Python 契約名稱；此處的 module 是跨層功能啟動單位。`bootstrap.wiring.build_modules()` 明確選入描述；不掃描 package，也不自動 import 未核准實作。

| `ModuleDefinition` 欄位 | 語意 |
| --- | --- |
| `name` | 唯一 `snake_case` namespace，符合 `^[a-z][a-z0-9_]*$`。 |
| `kind` | 預設 `"application"`；內建共通能力使用 `"platform"`。 |
| `allow_anonymous` | 預設 False；identity 啟用時應用 router 套用身份驗證。匿名入口必須明確選擇。 |
| `requires` | 需要的啟動單位名稱，決定初始化順序；缺漏或循環在開連線前失敗。 |
| `routers` | 相對路由的 tuple，由 API 組裝掛載 `/api/v1/<name>`。 |
| `lifespan` | async context manager，取得 context 並 yield exports。 |

| `ModuleExports` 欄位 | 語意 |
| --- | --- |
| `services` | 可選公開 facade；定義使用者需要的窄型別，而非公開具體 adapter。 |
| `tasks` | 本地 task name → async handler；完整名稱為 `<name>.<task_name>`。 |
| `kernels` | 本地 kernel name → `SolutionKernel`；可信 binding 由 `bootstrap.plugin_loader` 驗證／匯集。 |
| `datasets` | 本地 source name → `ExportSource`。 |

以下初始化骨架放在 `bootstrap/` 中明確命名的組裝模組。它不建立預設應用行為；實際 service、adapter 與 router 依各層位置 import：

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports


@asynccontextmanager
async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
    # 建立並注入需要的 domain ports；於此登記資源 cleanup。
    yield ModuleExports()


module = ModuleDefinition(name="capability", lifespan=lifespan)
```

在 `bootstrap.wiring.build_modules()` 明確加入此 `module`，保留既有可選共通能力。測試可用 `create_app(settings, modules=(definition,))` 或 `build_worker_broker(settings, modules=(definition,))` 注入，不修改正式註冊清單。

註冊內容包括需要的依賴、router、async lifespan，以及產出的公開服務、任務、kernel、匯出來源。依賴先啟動，consumer 後啟動；任何後續初始化失敗，都須退出已成功取得的資源。每個 initializer 應使用 `AsyncExitStack` 或等效機制，在可能失敗之前登記 cleanup。

啟動 context 中的設定、HTTP client、角色、依賴與資源，只供 bootstrap 使用。從 context 取得 `threads`、`leases`、`mongodb` 或其他 facade 後，僅把使用案例需要的窄介面傳入建構式。不要把完整 context、resources mapping 或 Runtime 傳入 application／domain。

`ModuleExports.services` 與 `ModuleContext.dependencies` 是程序內組裝契約，不是跨網路介面。能力跨 process／deployment 後，必須改用具版本的 HTTP 或 message wire contract，並由 consumer-owned Protocol 隔離 local／remote adapter；ownership、相容性、冪等與發布規則見[微服務契約設計與演進指南](microservice-contracts.md)。gRPC／Protobuf 不在該指南目前範圍內，採用前須另訂 schema 相容規範。

API 與 worker 各自組裝程序資源；同一部署必須使用相同 handler namespace、payload 契約與 kernel 版本。初始化過程不可提交工作，dispatcher 與其他依賴此時可能尚未 ready。migration、全域排程器與一次性管理任務應使用獨立管理入口，不依賴每個程序的 lifespan 自動執行一次。

## 任務、kernel 與匯出的契約

### 背景任務

handler 是 async callable，接收與回傳 JSON object。輸入／結果不得含 client、ORM session、例外、datetime 或不可序列化物件。worker 解碼任務 envelope 並依已註冊名稱查找 handler；這不等於已驗證應用 payload。新增 handler 必須定義自身 payload schema 並完成語意驗證，再委派同一 application use case。namespace 使用 `<capability>.<task_name>`；客戶端不得直接指定任意函式。

受保護工作攜帶已驗證的 tenant／owner 識別，worker 在執行時重新取得有效身份與權限；`tenant_id` 表示身份與資料隔離範圍，不自動對應任何應用實體，應用識別需另訂模型與授權映射。訊息中不放 JWT、密碼或其他可重複使用的秘密。handler 可能重送，具有外部副作用時必須定義持久冪等識別。快照 TTL、terminal-state 去重與先寫終態再 ack，不提供跨 Redis／RabbitMQ 原子交易或永久 exactly-once。

handler 不吞掉 `asyncio.CancelledError`。阻塞 I/O 使用有界 thread runner；長時間 CPU／GPU 或需要強制終止的工作，依需求移至專用 process／remote adapter。取消 caller 不能強制停止已執行的 Python thread。

### Solution kernel

策略實作遵守 `src.domain.strategy.SolutionKernel` 契約：提供 `spec`，以及 `async execute(payload, context)`。`KernelSpec` 包含明確 version、kind、resource group 與可選 artifact digest；輸入／輸出為 JSON object，kernel 負責版本化 schema 的語意驗證。每個註冊 namespace 目前只有一個版本；digest 是 metadata，不代表已驗證模型檔。

kernel 執行由 application 進行動作授權、版本比對、輸入／輸出容量限制、timeout 與 lease 協調。context 提供有效 principal、request ID 與 fencing token。外部受保護 adapter 必須落實 fencing；Redis failover、舊持有者繼續執行與強隔離需求，仍須部署及使用案例另訂契約。

### 匯出來源

來源明確宣告欄位與 `rows(principal, limit=...)` async iterator，adapter 將日期／ObjectId 等轉為 JSON 相容值。repository 強制 tenant，沒有跨 owner permission 時同時強制 owner。HTTP 不接受任意 collection、query 或欄位。

共通 export 使用案例負責權限、欄位白名單、秘密欄位防護、CSV formula 防護、CSV／JSONL 與筆數限制。串流中途失敗可能留下不完整下載，客戶端必須丟棄該檔案；本版沒有永久匯出工件或 durable export job 服務。

## 新增一項應用的順序

實作步驟與範例見 [業務團隊開發手冊](developer-guide.md)；本規範定義分層與契約，操作手冊補足跨團隊接入流程。

1. 在 `src/domain/<capability>/` 定義模型、錯誤、政策與所需 ports；界定契約版本及資料 ownership。
2. 在 `src/application/<capability>/` 實作使用案例；只依 domain 契約，將身份、timeout、重送／冪等與完成語意寫清楚。
3. 在 `src/infrastructure/` 適當分類實作外部 ports；若有演算法，在 `src/plugins/` 實作 domain strategy。以建構式接收資源，不在 import 時開連線。
4. 在 `src/api/<capability>/` 建立 schemas、router 與具型別 DI。需要背景工作時增加 async handler，兩個入口委派同一使用案例。
5. 在 `bootstrap/` 建立明確的初始化／組裝描述，將 adapter 與策略注入使用案例，登記 router／tasks／kernels／datasets 與依賴。
6. 在 `bootstrap/wiring.py` 選入該能力；保留既有身份等共通能力的選用邏輯。同步更新 API 與 worker 的部署設定。
7. 驗證實際使用案例、契約、身份／tenant／owner 隔離、transport 邊界、adapter 錯誤、startup rollback 與 shutdown；涉及副作用時驗證重送及部分失敗。

跨層各能力 package 是同一應用的協作實作；不把整個應用放進 `src/services/`，也不要求 router 經 runtime services 轉送請求。測試 fixture 留在 tests，不在 plugins／domain 加入預設演算法或範例資料。

## 合併前檢查

```bash
python scripts/check_boundaries.py
ruff check .
ruff format --check .
mypy src bootstrap server.py worker.py manage.py
pytest
```

以上是本地檢查入口；完整已執行證據與未執行項目以 [驗證紀錄](validation.md) 為準。部署驗收仍需真實 broker／DB、程序數、資源配額、故障恢復與新增應用的使用案例測試；單元測試通過不代表已取得生產核准。
