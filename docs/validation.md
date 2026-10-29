# 驗證紀錄（更新：2026-10-27）

## ML 設計 v0.2 的基線符合性核對（2026-10-27 彙整）

針對 ML 設計引用的 commit `08490e84cc88d2ff7300cb97fd23e93ea0e33a1a`，已在 git archive 隔離副本執行平台 import origins、boundary、strict Mypy、非整合單元測試與 pip check，並比對目前程式／locks 的 bytes。結果與完整限制集中於 [ML 基線符合性審查](ml-system-compliance-review.md)及[本輪 evidence](evidence/ml-design-review-2026-10-26/README.md)。

本輪不包含 PostgreSQL／MLflow／新 ACK gate／Spec Delivery／Serving E2E，也沒有新 image 或遠端 CI 結果。下方既有驗證快照保留原樣。

## CI／pre-commit 增補驗證（2026-10-26）

本次以 `0ac0bda3035d17e90cd01670a3045a4271ce871c` 為基底，新增本機 hooks 與 CI 共用品質入口。修改位於本機工作樹，尚未 commit／push。以下增補結果不取代先前 Docker／broker 交付快照。

| 項目 | 實際結果 |
| --- | --- |
| 共用品質檢查 | pre-commit 4.7.0／pre-commit-hooks 6.0.0，12 個 hooks；所有追蹤檔與新增設定／文件皆檢查通過。未匹配檔案的 hooks 依規則略過。 |
| Hook 安裝與拒絕行為 | 本機 `.git/hooks/pre-commit` 已安裝；隔離 Git 副本的實際 staged hook 執行通過。13 項情境涵蓋無效 YAML、合併標記、合成私鑰標記、禁止本機 env、允許範例、自動修正及 CI gate。未建立測試 commit。 |
| CI required gate | 直接執行 workflow 中的 gate 腳本；success 回傳 0，failure／cancelled／skipped 各回傳 1。 |
| Workflow 語法 | 三份 workflows 通過固定 digest 的 actionlint 1.7.12。 |
| macOS Python 3.13.11 | 440 passed、4 integration deselected，合併 line／branch coverage **86.47%**，85% 門檻通過。 |
| Linux Python 3.11 | 固定 digest 映像內新建 venv、hash 安裝新增 dev lock，pip check、pre-commit config、Ruff、格式、strict Mypy、boundary 通過；440 passed、4 integration deselected，coverage **86.60%**。 |
| 套件／手冊 | wheel／sdist 非 editable 安裝與 Twine strict 通過；同步與背景 guide 範例的 boundary／Ruff／格式／Mypy／測試通過。 |
| 依賴安全 | 依 CI 相同 require-hashes／strict 參數，稽核 dev／build 的 120 筆條目（含重複），零已知漏洞。 |
| Secret | 目前變更的隔離來源快照通過 Gitleaks directory 掃描；此為工作樹快照掃描，Git 歷史掃描仍保留於 CI。 |
| 保留項目 | runtime／build locks、正式 Python 邏輯與既有 references／evidence 未改寫；新增工程證據獨立保存。 |

[增補證據與指令](evidence/ci-precommit-2026-10-26/README.md)、[團隊安裝／操作規範](ci-precommit.md)。本次未重建 Docker image 或重跑 broker 整合，相關結果沿用下方先前交付快照；未宣稱 GitHub Actions 已執行或 branch protection 已設定。Required checks 應由管理員選取 `CI required`、`python-security`、`secrets`、`image-and-broker`。

## v1.3 工程交付驗收（先前快照）

此先前快照的交付對象為使用通用範本的開發團隊。以下結果對應當時指定專案的交付工作樹；基底 commit 為 `31248de22da57fc604d1481465a7cc71dd1f6801`，該快照驗證當時的新修改尚未 commit／push。完整來源與 artifact 雜湊另存證據，不能只用舊 commit 宣稱新版本已驗收。

| 項目 | 實際結果與範圍 |
| --- | --- |
| 最終完整 pytest | **444 passed，0 skipped，10.40 秒**；host Python 3.13.11，RabbitMQ／Redis／MongoDB 為隔離真容器，包含雙程序整合。 |
| Python 3.13 單元與 coverage | 440 passed、4 integration deselected；整體 **86.47%**，85% 門檻通過。 |
| Linux Python 3.11 | 2026-10-24 以乾淨 hash 安裝環境執行：440 passed、4 integration deselected；整體 **86.60%**。其後產品 Python 邏輯未改；最後 image 工具移除及文件另有驗證。 |
| Coverage 定義 | 啟用 branch 量測，數字為 coverage.py 合併 line／branch 指標，不是純 branch 百分比。來源分母保留 integration adapters；tests／scripts／work／build 等不計。 |
| 品質檢查 | Ruff、格式、依賴邊界、strict Mypy 108 source files 與 pip check 通過。workflow 另經 actionlint；結果與命令保存在檢查日誌。 |
| Wheel／sdist | Python 3.11／3.13 分離乾淨 build／runtime venv，真實非 editable 安裝；checkout 外 isolated import、外部 socket 阻擋、disabled lifespan／health／cleanup 通過。最終 3.13 wheel／sdist 通過 Twine strict。 |
| 開發手冊 | 22 個 Python 區塊原樣提取，同步／背景兩份新副本的 boundary、Ruff、格式、Mypy 與行為測試全通過；同步 18、背景 29 案例包含共同測試，不加總為獨立平台案例。 |
| 新能力採用 | 文件中性 items 能力於隔離副本完成真 image／API／worker／MongoDB／RabbitMQ／Redis：建立與讀取、背景讀取、同 tenant 他人及跨 tenant 拒絕、job 結果隔離通過。正式專案未註冊該能力。 |
| 映像整合 | 非 root、原始 CMD、無 pip／ensurepip、乾淨 OpenAPI、readiness、SIGTERM；雙 API／雙 Taskiq 完成 32 筆 JWT 授權工作；API restart 後 session／snapshot 保存、logout 撤銷通過。 |
| Secret 掃描 | Git 歷史與目前交付工作樹均通過 Gitleaks；例外限定已核對的 SHA／參數轉發／官方 Python 公開 GPG 指紋，未略過整份原碼或報告。 |
| Dependency／SAST | hash locks 稽核 111 筆 dev／build 條目（可能含重複），零已知漏洞；Bandit medium severity／confidence gate 通過。 |
| Image 安全 | Trivy 0.76.1，無可修復的 HIGH／CRITICAL；完整 JSON 及 CycloneDX SBOM 保存。掃描仍有基底 Debian 未提供修正版項目：44 HIGH、58 MEDIUM、61 LOW、1 UNKNOWN，不能解讀為零漏洞。 |
| Source／文件 | Python／文件／SVG／設定命名已核對；原始參考與舊 evidence 未改寫。現行設計 PBP-SAD-001 v1.3、手冊 DG-001 v1.1。 |

最終 image 為 `local/backend-validation:4993264301f9`，ID `sha256:40dbbc13c30674dc418e7443f147dce7ab42fed24f7ec6ed3344cae10c86b2d0`，Linux arm64、Python 3.13.17。Docker 29.4.0，RabbitMQ 4.4.0、Redis 7.4.8、MongoDB 8.3.13。該 run 的 containers／network／volumes 已清理，image 保留。

[本次證據導讀](evidence/handover-2026-10-26/README.md) 保存實際命令、日誌、報告、受驗產物與雜湊。[交付狀態](handover.md) 及 [工程重跑規範](engineering-baseline.md) 說明交付與正式部署界線。GitHub 遠端 CI／branch protection、registry／CD、產品目標環境壓測／HA／恢復與生產核准沒有在本次宣告完成；API readiness 不證明 worker heartbeat，request/job correlation 不等同 tracing。

## 2026-10-24 歷史驗證紀錄

以下原紀錄保留當時版本與未執行範圍。其「本輪／先前」均指當時 run，不取代上方 v1.3 結果。

下列命令從本專案根目錄執行，以載入 `pyproject.toml` 的 async 測試設定。host 測試環境使用 Python 3.13.11；Docker image 內為 Python 3.13.17／Linux arm64；依賴版本記錄於 `requirements.lock`。主要套件為 FastAPI 0.161.1、Uvicorn 0.54.0、Taskiq 0.12.6、taskiq-aio-pika 0.6.1、redis-py 8.1.0、PyMongo 4.19.0、pwdlib 0.3.1 與 PyJWT 2.15.1。

## v1.2 通用藍圖修訂（2026-10-24）

本輪只修訂正式文件、術語、SVG、三個 Python 檔的 docstring／註解，沒有新增執行邏輯、API、設定或訊息契約。已核對 123 個版本控制中的 Python 檔：移除 docstring 後的 AST 與修訂前一致；Ruff／格式與依賴邊界檢查通過；正式文件的產業假設掃描、Markdown 連結／fences、SVG XML 解析及渲染目視檢查通過。原始參考 `.md`／`.svg` 與既有 `docs/evidence/` 全部維持逐位元組一致。

本輪沒有重跑完整 pytest 或重新建置 Docker image。下列完整整合結果屬於修訂前已保存的 run；其 image ID、image-source-manifest 與日誌描述當時實際建置來源，並非 v1.2 文件的逐位元組映像驗證。原始證據不因文件版本更新而改寫。

## 先前已執行的工程驗證


- **先前完整 Docker 整合 run 的 host pytest：406 passed，0 skipped，10.68 秒**。由 `scripts/verify_docker.py --host-tests` 明確啟用 `RUN_SYSTEM_INTEGRATION=1`，在 host 開發環境執行，broker／MongoDB 指向隔離的真實容器。原先略過的雙 API／雙 Taskiq 測試已通過。未設定 opt-in 開關的一般 pytest 仍會略過該項。
- **正式 Docker image 與容器內整合已通過**：Docker 29.4.0，RabbitMQ 4.4.0、Redis 7.4.8、MongoDB 8.3.13。映像為非 root UID 999；原始 CMD、乾淨 OpenAPI、readiness 與 graceful shutdown 通過。另一組同 image 容器唯讀掛載測試 fixture，兩個 API PID 9/10、兩個 Taskiq PID 8/9 完成 32 筆 JWT 授權 kernel 工作；quorum queue 有兩個 consumer，最後 ready/unacknowledged 均 0；API restart 後 Mongo session／Redis snapshot 可查，logout 撤銷有效。
- 最終 image：`local/backend-validation:605e70e128ef`，ID `sha256:7e2d2d15be864e087569b2f3af80e9922999f328c52f2e0e7be7d16670b3aed9`。本次 project 的 containers、network、volumes 已清除，image 保留。[最終報告與日誌](evidence/docker-2026-10-24/README.md)、[可重跑操作手冊](docker-validation.md)。
- 空白預設 API、輸入驗證、設定互斥、secret 錯誤遮蔽、預設 JWT 保護、匿名入口明確選入、啟動失敗回滾及資源釋放順序。
- 新 API/health 邊界 14 個案例；readiness 技術檢查與 HTTP adapter 分離、route group 掛載、既有 JWT 政策。
- 可信 plugin loader 19 個案例：明確註冊、唯讀 mapping、namespace／contract／version／spec／kind、同步 execute 拒絕、async callable 支援，以及 API／worker 初始化失敗反序回滾。
- 模組 manifest、依賴拓撲／循環檢查、命名空間、公開 facade、API／worker 初始化失敗回滾。
- AST 依賴邊界檢查：161 個案例，涵蓋新 src 各層、bootstrap／entrypoints、相對／絕對 import、TYPE_CHECKING、API DI 例外、禁止 SDK 滲入內層及拒絕舊 backend package。
- 本機任務並行限制、背壓、隔離、逾時、取消、TTL、容量淘汰及 JSON 結果契約測試。
- 真實 Taskiq receiver 搭配 broker／storage 替身，驗證終態寫入先於 ack、Redis 故障恢復、取消不確認及重送處理。
- 隔離的真實 Redis 程序：共享快照、TTL、兩個 coordinator 的共享容量、續租超過原期限、fencing 與 owner-safe release。
- Bounded thread executor：入場／等待容量、contextvars、取消與重複取消、關閉取消及實體工作完成前不提前釋放資源。
- Kernel：thread 容量不足透傳為 HTTP 503 的回歸測試、空 registry、明確版本、權限、並行容量、lease 失效、timeout／取消／關閉、嚴格 JSON、背景任務重新授權及 job 租戶／擁有者隔離。演算法只使用測試專用技術 fixture。
- 匯出：空來源、權限、欄位白名單、敏感欄位遮蔽、CSV 公式防護、串流／筆數／單列大小、Mongo 查詢強制 tenant／owner 範圍。
- **真實 MongoDB 8.2.3 整合**：以獨立臨時資料路徑及 loopback 埠啟動，全程開啟 `--auth`，建立一次性資料庫帳號；驗證 Argon2／JWT 登入、受控註冊、錯誤簽章拒絕、跨租戶隔離、即時 permission、停權、session 登出、原子登入限制與受控資料匯出。結束後清除測試 DB 並關閉實例。
- 重構後真實 Uvicorn HTTP smoke：readiness 200、預設 OpenAPI 僅有兩個健康端點、X-Request-ID、src.server／src.api.http JSON 日誌；SIGTERM 後確認 application shutdown complete。
- `ruff check .`、`ruff format --check .`、`mypy src bootstrap scripts server.py worker.py manage.py`（102 個 source files）、`scripts/check_boundaries.py` 及 `pip check`。
- 離線 wheel 建置：`pip wheel --no-index --no-deps --no-build-isolation`（本機 bundled setuptools/wheel 作建置工具）。在專案外 CWD 與 Python `-I` 載入 wheel 解壓內容，server／worker／manage／domain strategy／plugin loader 均來自安裝內容；封鎖 socket 後 imports 與預設 create_app 仍成功。wheel 不含舊 backend、tests、.env、key/pem 或 pyc。
- 使用者原始 SVG 與設計文件逐位元組保存並記錄 SHA-256；原圖及更新 SVG 均經 XML／rsvg 解析與實際渲染目視檢查；本專案 Markdown 連結檢查無失效目標（原稿保留原樣）。
- 本次取捨、層級映射、狀態／timeout／retention／session 與已實作／待實作的描述經獨立程式對照，見 `design-review.md`、`software-design.md` 及 ADR。
- `docker compose config --quiet`、CI YAML 語法與 MongoDB integration service 設定；檢查時產生的 secrets 不輸出至日誌。
- TypeScript client 的 Node 原生型別移除／載入與 smoke：登入不附帶 Bearer、同源授權、拒絕跨來源／redirect、401 清除 token。未執行完整 TypeScript 編譯。

尚未執行：

- 正式 `compose.yaml` 本次完成 config 檢查；實際隔離整合使用 `compose.validation.yaml`，不是目標生產拓撲／HA 驗收。
- GitHub Actions；CI 已定義 Python 3.11／3.13 檢查、MongoDB 整合及 RabbitMQ／Redis 多程序整合工作。目前本機 Python 實測版本為 3.13。
- 完整前端應用、使用者管理 UI 與瀏覽器端端到端測試；交付內容是可整合的 JWT client。
- 生產環境的壓力測試、故障演練與部署驗收。

Docker 驗證另新增獨立 Compose、測試 fixture 與可重跑 runner；修正日誌 stdout/stderr 合併檢查、API 重啟後動態 host port 更新，以及清除／憑證遮蔽的錯誤路徑，新增四個安全回歸測試。正式應用 wiring 未載入 probe kernel，image 不包含 tests。

先前依 SVG 實際搬移 package／module，拆分 API／技術 service／application 責任，新增明確 plugin 契約驗證與中性支援入口，並更新 setuptools、Dockerfile、CI、logger roots 與文件。既有 IAM／jobs／Mongo／exports／kernel 行為保留；正式業務 initializer 與演算法仍為空，沒有新增預設產品場景。

這份紀錄描述已完成的驗證，不代表所有部署環境或未來依賴版本都具有相同行為。
