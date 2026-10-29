> Historical pre-change assessment at commit 31248de. Current implementation status: [handover](../handover.md) and [engineering baseline](../engineering-baseline.md). The following record is preserved as reviewed.

# 通用 Python Backend 工程建設建議審查與採納路線圖

| 項目 | 內容 |
| --- | --- |
| 日期 | 2026-10-24（Asia/Taipei） |
| 審查基準 | `/Users/pwang/Developer/python-backend-server-template`，commit `31248de22da57fc604d1481465a7cc71dd1f6801`；開始審查時工作樹乾淨 |
| 現行文件 | PBP-SAD-001 v1.2、Developer Guide DG-001 v1.0 |
| 輸入 | 使用者提供的《Python Backend Server Template — 基礎工程建設總體規劃》；SHA-256：`750245903ee792fbedd5218397b144be11776394af58bf74a6563f59e1297640` |
| 性質 | 建議採納方案與實作排程依據；不是新增實作、發佈核准或生產驗收紀錄 |
| 本次變更 | 只新增本審查交付檔；未修改專案程式、設定、CI 或既有交付文件 |

## 1. 審查結論

建議採納「保留模組化分層，先補工程基礎，再依需求擴充部署」的主方向。現行架構已足以承載不同團隊的應用，下一步不需要再次搬移 package、重建 dispatcher，或全面拆分微服務。

需要先修正建議書中的現況描述：模組／插件契約、依賴邊界測試、啟動失敗回滾，以及 Developer Guide 的 owner 權限問題，均已有實作或修正。真正的缺口主要在能力可選性、CI 自動證據、安裝／建置可重現性，以及 worker 觀測與目標環境驗收。

此外，「交付給開發團隊作為範本」與「特定產品已可正式上線」應分別驗收。現有開發手冊與歷史整合證據可作為前者的基礎，不能替代後者的部署、容量、資料恢復與故障驗收。

## 2. 建議與實作的差異

下表的檔案位置均以審查基準目錄為根目錄；行號對應上述 commit。

| 建議 | 實際現況與證據 | 採納方式 |
| --- | --- | --- |
| 保留分層、明確 DI、API／worker 分離 | 已有 `bootstrap/` 與九個 `src/` 分層；`bootstrap/wiring.py:9` 明確選入能力，API／worker 各自管理 lifespan。 | 保留。沿用 Ports & Adapters、Application Service、Composition Root、constructor injection；按需求使用 Repository／Strategy／Facade／Command Registry。 |
| 補模組／插件契約與 lifecycle 測試 | `tests/test_modules.py:100,248,288` 已涵蓋依賴順序與失敗回滾；`tests/test_plugin_loader.py:49,59,128` 已檢查識別、契約版本與失敗清理；`tests/test_boundaries.py` 已檢查實體分層。 | 更新現況為「已有，持續維護及補未覆蓋分支」。不要另建一套相同機制。 |
| 修正 Developer Guide 的 owner 授權 | `docs/developer-guide.md:253–257` 預設限定登入者，只有精確的 `items.read_all` 放寬 owner；`:319–322` 查詢仍保留 tenant；`:986–1002` 有同 tenant 他人及跨 tenant 測試。 | 建議書 §7.1 的未解問題描述已過時。保留最終範例及審核證據，將防回歸測試納入後續範本驗證。 |
| 強化 CI 品質門檻 | `.github/workflows/ci.yml:26–31` 已有 Ruff、格式、strict Mypy、邊界與 unit tests；`:33–90` 已有真 RabbitMQ／Redis／MongoDB integration。 | 增補 coverage、artifact 安裝、供應鏈與 image 驗證。現有 integration 已在 PR 觸發，不應為配合新分類而降至 main 才執行。 |
| 強化 dependency locking | `requirements.lock` 為精確版本 constraints，無 hashes；`pyproject.toml:2` 的 build backend 只有版本下限。 | 分別規範 runtime、development、build 的解析與更新方法；針對宣告支援的 Python／OS／架構驗證。先保留 pip，無須同時引入第二套鎖定流程。 |
| 建立 wheel／sdist 驗證 | `docs/validation.md:31` 已記錄離線 wheel build、在專案外載入解壓內容及封鎖 socket 的 import smoke。 | 仍需 CI 從乾淨 venv 真正 `pip install` 建出的 wheel，並驗證 sdist 可再建 wheel；既有 smoke 不能直接等同此新 gate。 |
| 強化 Docker baseline | `Dockerfile:9,17` 已用非 root；已有 `.dockerignore`、獨立 validation Compose 與 `scripts/verify_docker.py`。歷史真容器結果保存在 `docs/evidence/`。 | 增加自動 build／startup／scan、release digest 與建置來源證據。保留 development／validation 的區分。 |
| 讓外部服務按需求啟用 | development 預設 memory，Mongo／identity 可關閉；但 production 一律要求 distributed，memory 一律限制單 API worker。 | **真實的通用性缺口，應優先修正。** 必須定義「背景工作未啟用」的語意，不能只刪設定驗證。詳見第 3 節。 |
| 讓 infrastructure SDK 成為可選依賴 | `pyproject.toml:16–21` 目前將 Taskiq、Redis、Mongo、身份 SDK 列為必裝；`src/services/resource_manager.py:6–7` 等入口有 eager imports。 | 與「不用連線外部服務」分開處理。若要支援最小安裝，再設計 extras／條件 import 及安裝矩陣；只移動依賴宣告會破壞啟動。 |
| 強化 worker 與 job 觀測 | API 有 JSON logging／request ID；worker startup 未呼叫同一 logging 初始化，一般 task envelope 缺少統一 context／schema version；kernel 專用路徑已有部分 request ID 傳遞。 | 先統一 API／worker context 與相容訊息格式，再按需加入 tracing／metrics exporter。不能說目前完全沒有 correlation。 |
| 強化 job reliability | 已有終態先保存再 ack、儲存故障恢復、bounded executor、lease／fencing；一般 handler 例外會記 failed 後確認，並非通用業務 retry。 | 先寫清重送、逾時、取消、副作用與結果保存契約。持久冪等／outbox 依需求設計，不一律開啟自動重試。 |
| 全新專案 adoption test | 手冊同步／背景範例已在隔離副本驗證；尚未完成該新能力的真 Mongo／RabbitMQ 容器端到端交付。 | 增加完整範本使用者旅程，並將手冊提取驗證自動化；保留中性範例，不預載正式業務。 |

## 3. 優先處理：純 REST production 的外部依賴

`src/config/settings.py:62–67` 有兩個無條件規則：memory 只能一個 API worker；production 必須 distributed。`server.py:58–78` 無條件建立並啟動 dispatcher，`bootstrap/wiring.py:21–37` 目前只有 memory／distributed 兩種選擇。

因此，即使不開 job API、kernel、exports、identity 或 Mongo，production 仍會被導向 RabbitMQ／Redis。這與建議書最後「純 REST API 可只啟動 FastAPI」的目標不一致。

本次在 Python 3.13.11 移除子程序中的 `APP_*` 環境變數、停用 `.env` 載入並明確關閉上述能力，取得以下設定驗證結果：

| 環境 | task backend | API workers | Settings 結果 |
| --- | --- | --- | --- |
| development | memory | 1 | 接受 |
| production | memory | 1 | 拒絕：production requires the distributed task backend |
| development | memory | 2 | 拒絕：memory task backend requires one worker |
| production | distributed | 1 | 接受設定；本次沒有啟動 lifespan 或驗證外部連線 |

原始結果見 [production-profile-check.json](production-profile-check.json)。

後續實作應先定義背景工作停用模式，再同步處理：

1. Settings、API factory／CLI、dispatcher lifecycle 與 readiness：停用時不建立或探測 RabbitMQ／Redis，允許無共享 job 狀態的多 API worker。
2. Runtime／ModuleContext 的 job 注入與註冊：停用時誤用背景工作必須明確拒絕，不能默默接收後遺失任務；有 task／job 需求的能力應於啟動驗證。
3. worker 入口：在停用模式下給出清楚設定錯誤；distributed 模式保留既有共享儲存與授權限制。
4. 回歸矩陣：純 API、單程序 memory、distributed API／worker，以及各能力不相容組合、啟動回滾與關閉順序。

只刪除 production 或 worker 數驗證，會使多程序 memory job 狀態不共享的原始問題重現，不是完整修正。

## 4. 建議採納的排程與完成條件

P0 表示在廣泛複製為正式團隊基線前補齊；P1 表示依啟用能力及上線風險驗收；P2 由實際部署或產品需求觸發。下列是建議排程，均未在本次宣告完成。

| 順序 | 優先級 | 有界工作包 | 完成條件 |
| --- | --- | --- | --- |
| 1 | P0 | 通用能力可選性 | 完成第 3 節的停用語意及測試；純 REST production 無 broker／Redis 可啟動，多 API worker 不啟動程序內 job queue。 |
| 2 | P0 | Package／dependency baseline | 有唯一鎖定產生／更新流程；覆蓋 build 與 runtime；wheel／sdist 可建，乾淨 venv 安裝 wheel 後從專案外 CWD 執行 import／create_app；產物排除秘密與測試資料。 |
| 3 | P0 | CI coverage 與既有契約持續驗證 | 保留現有 Ruff／Mypy／boundary／unit／integration；產生 coverage 報告與 artifact，按實測設定門檻；授權、隔離、rollback、dispatch 重要分支需有行為證據。 |
| 4 | P0 | Security／container release gate | 依賴、secret、靜態安全、image 掃描具明確觸發與失敗政策；容器 build／非 root／健康檢查／SIGTERM smoke 自動化；release 記錄 commit、base image 與成品 digest、依賴清單／SBOM。 |
| 5 | P1；廣泛交付前完成最小旅程 | Template adoption 與維護規範 | 從乾淨副本新增中性能力，跑測試、build、Docker 整合；手冊範例同步驗證；新增 contribution／版本與升級政策。無須為驗證建立遠端 repository。 |
| 6 | P1 | Worker／job context 與契約治理 | API／worker 統一 service／environment／request／job context；訊息 schema 演進策略可處理舊 queue 內容；新增／修改契約有相容性測試。 |
| 7 | P1 | 啟用能力的可靠性與維運 | job timeout／backpressure／drain／crash／重送契約可驗收；區分 API readiness、lease renewal 與 worker heartbeat；具需要的 metrics、告警及操作手冊。 |
| 8 | P1；目標平台確認後 | CD 與部署驗收 | 定義 registry、部署平台、權限與責任人；同一成品 digest 推廣、staging smoke、版本回退；資料 migration／恢復另行驗收。 |
| 9 | P2 | 服務抽取與進階部署 | 只有明確獨立發佈、資源／故障隔離、擴容或資料 ownership 需求時，才導入服務拆分、Kubernetes、GPU serving 或複雜 workflow。 |

第一輪程式修改建議只處理第 1 包；第二輪補 packaging／coverage gate。Security 與 container gate 是 P0 的後續必備工作，不應因拆包而無限延後。每包分別提供變更、回歸與證據，避免同時改 namespace、套件管理工具、runtime 與部署，造成失敗來源難以辨識。

## 5. 對原建議的必要調整

### 保留 package 與開發模式

維持使用者 SVG 已確立的 `bootstrap/`、`src/{api,services,application,domain,plugins,infrastructure,config,utils,shared_models}`。不因一般 Python 慣例立即改為另一個 namespace。

現階段以 Template Repository 為交付模型；Internal Library／Hybrid 是後續發佈與升級策略的決策，不能預先當成唯一終點。即使先維持模板複製，也需要說明平台修補如何回流各產品，以及各產品如何追蹤採用的基線版本。

「新增能力不修改核心」應具體定義為：不修改共通 runtime 執行邏輯；允許增加能力各層檔案、`bootstrap/<capability>.py`、`bootstrap/wiring.py` 明確註冊，以及必要依賴／部署設定。這些本來就是擴充契約的一部分。若連 wiring 都禁止修改，會與現行設計矛盾。

### 精準定義可重現安裝與建置

Constraints 限制版本選擇；hash verification 驗證下載的 artifact，兩者不是相同保證。隔離 build 的工具版本也要納入流程，不能只憑檔名 `requirements.lock` 或 `pip -c` 就宣告完整鎖定。[pip constraints 說明](https://pip.pypa.io/en/stable/user_guide/#constraints-files)、[可重複安裝說明](https://pip.pypa.io/en/stable/topics/repeatable-installs/)。

完成條件應列出受測 Python／平台、工具版本、更新方式及 artifact 來源；不將「可重現解析／可追溯建置」直接寫成所有環境 bit-for-bit 相同的 image。`pyproject.toml` 目前宣告 Python `>=3.11`，CI 定義 3.11／3.13；不要把建議書舉例的 3.12 寫成已測試，也不要將未來所有 Python 版本視為已驗收。

License／著作權與內部分發權利由專案權利人決定；不能在補 metadata 時自行選定授權。

### 以結果規範 Docker 與 CI

多階段建置有助於將建置工具與 runtime 分離；應保留為改善手段，驗收則檢查成品內容、權限、依賴、大小與可追溯性。現行 Dockerfile 沒有 compiler 安裝步驟，單純「改成兩階段」不應取代實際成品驗證。[Docker 建置建議](https://docs.docker.com/build/building/best-practices/)。

Coverage 先建立實測基線、檢查關鍵缺漏，再訂門檻；不任填 80% 或以百分比取代授權與副作用測試。Security 檢查應在受影響的 PR／release 前發現問題，並定期重新掃描；例外須有範圍、原因、負責人及期限。現有 integration 保留於 PR，較昂貴的完整 Docker／長時間故障測試才另訂頻率。

Workflow 檔案有定義，不代表遠端已執行成功或 branch protection 已強制要求。此次沒有檢查 GitHub 執行紀錄或遠端保護設定。

### 先做基本觀測，再選監控部署

先補 API／worker 一致的結構化欄位及一般 message context；request ID 只是關聯識別，不等同完整 trace。若導入 OpenTelemetry，跨程序需明確 inject／extract context，並以可替換 exporter 接部署環境。Python 的 traces／metrics 已列為 stable，logs 元件仍列為 development；現有 JSON logging 可保留，不必為導入 tracing 同時替換整套 logging。[OpenTelemetry Python 狀態](https://opentelemetry.io/docs/languages/python/)、[Context propagation](https://opentelemetry.io/docs/languages/python/propagation/)。

`src/services/health.py:12–22` 的 API readiness 檢查目前程序依賴，不能證明 worker 正在消費；capacity lease 的續租也不是 worker heartbeat。此二者需分別定義驗收及操作指標。

### 可靠性由契約與風險驅動

沿用實作的 job 狀態名稱 `queued/running/succeeded/failed/cancelled`（`src/domain/jobs/models.py:10–15`），不要在文件另換成未實作的 `pending`。需自動重試時，先分清儲存恢復、delivery 重送與業務操作重試，尤其不能對未知副作用一律 retry。

可先以文件描述不同可靠性需求；沒有實際需求前，不必建立新的通用 `ReliabilityProfile` framework、distributed transaction 或 deployment adapter class hierarchy。CD 的差異可先由 workflow／部署設定承擔；選定平台後才決定是否需要程式抽象。

## 6. 後續文件落點與同步責任

| 文件／規範 | 建議更新內容 |
| --- | --- |
| `docs/adr/README.md` | 記錄背景工作可停用、模板交付模式與驗證／發佈政策；清楚標示 proposed／accepted 狀態。 |
| `docs/software-design.md`、`docs/module-spec.md` | 實際完成可選性及 context 修改後，同步 startup／shutdown、job 注入、health 與訊息契約。 |
| `docs/developer-guide.md` | 保留最終授權範例、既有落點與模式；增加乾淨專案 adoption 步驟及新 gate 指令。 |
| `docs/validation.md`、`docs/evidence/` | 新 run 另存，附 commit／指令／環境／產物／結果／未測限制；保留舊證據，不將歷史成功套到新版本。 |
| 新增工程操作說明與 `CONTRIBUTING.md` | 環境安裝、鎖定更新、CI 檢查、契約審查、版本與基線升級流程；組織決定後再設定 CODEOWNERS／required checks。 |
| 部署與操作文件 | 目標平台確認後補 registry、secrets、最小權限、rollback、監控及資料恢復；不得將本機 Compose 直接宣告為生產配置。 |

不需要為配合目標樹立刻搬動全部 docs／tests，或建立空的 `deploy/`、workflow 與模組。先讓實際命令、責任與完成條件可追蹤，再按文件量分類。

## 7. 本次審查範圍

本次已讀完整建議書、核對目前設定／CI／Docker／runtime／測試原碼與交付文件，並以兩項獨立唯讀審查交叉核對工程與 runtime 差異。另執行第 3 節四組 Settings 驗證；這是設定行為的重現，沒有對外連線。

本次沒有重跑完整 pytest、建立新 wheel／image、啟動 Docker／RabbitMQ／MongoDB、執行漏洞掃描、遠端 CI 或部署。既有平台 Docker 整合與手冊隔離驗證仍以專案保存的歷史紀錄為準；新規劃的 gates 尚未實作。
