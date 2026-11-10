# Push 與 PR Actions 失敗修復

日期：2026-11-09。延續同一個 feature branch 與 Draft PR #1。

兩筆失敗有不同原因：

| Run | 失敗位置 | 原因 |
| --- | --- | --- |
| [Push 42510624432](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42510624432) | SQLite Contracts／slow I/O lease test | 200ms wall-clock lease 與固定 sleep 依賴背景執行緒及時排程；負載高時失去 lease，正式程式正確保留 PUBLISHING 供恢復 |
| [PR 42510630217](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42510630217) | Backend stack 啟動 | `minio/mc:latest` pull access denied；上一筆 run 也有 `minio/minio:latest` 相同問題 |

續租測試改用 module-local 受控 wall clock 與 Condition，阻塞 I/O 時等待三次真正
提交的 SQL heartbeat，再推進時間超過原始 lease；同時驗證競爭 worker 仍無法取得
claim。沒有修改正式 ownership／expiry 規則，也沒有以放寬 assertion 掩蓋過期。

MinIO Community Edition 已採 [source-only distribution](https://github.com/minio/minio#source-only-distribution)。
官方 Quay 的固定舊 release manifest 也不存在，因此不只替換 registry。
`Dockerfile.minio` 從固定的官方 source commits 建置真正的 server／mc：

- [MinIO security release](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)：
  `9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`。
- [mc release](https://github.com/minio/mc/releases/tag/RELEASE.2025-08-13T08-35-41Z)：
  `7394ce0dd2a80935aded936b09fa12cbb3cb8096`。

Go module checksum verification 保持啟用，保留 release metadata 與對應 LICENSE；
LICENSE 使用精確 module directory，避免同時匹配 dependency 的其他版本。
Compose 的 bucket 初始化、credentials、volume 與 S3／MLflow 契約保持原有行為。
這是從官方 source 建置實際 object store，不是 S3 測試替身。

另修復 Backend 驗收容器的後續問題：

- 安裝 `ebm` extra，提供測試必需的 SciPy。
- `PYTHONPYCACHEPREFIX` 指向可寫的驗收輸出目錄，使非 root 使用者可執行
  `compileall`，不修改 image source 的權限。
- Backend stack scope 明確排除有獨立環境的外部 Databricks live case 與主機
  Compose parser cases，並在 report 列出其獨立 qualification gate。Backend／
  RabbitMQ 必要案例保持選入；所選 suite 的任何 skipped case 仍拒絕驗收。
- CI 在失敗後保存 disposable stack status／service logs，以便定位啟動問題。

本地驗證：完整 ML／boundary／plugin／module 回歸 **918 passed、0 failed、4 skipped**；
四個 skipped case 仍需 Databricks、Backend 或 RabbitMQ／Redis 服務。
Runtime／recovery suite **21 passed**，慢 I/O 三個 phase 各以十個獨立 subprocess
執行，共 **30/30 passed**。Acceptance scope／skip guard／readonly compile 等相關
測試 **16 passed、零 skipped**。Ingestion Ruff／format／strict Mypy、Pyflakes 與
依賴邊界通過。

MinIO／mc source 編譯已完成，固定版本的 module directory 已實際核對；完整 image
包裝與 Backend stack 驗收仍須由本地 image 測試及更新後 Actions 結果確認。
不能將編譯、Compose parse 或 skipped case 當作完整 Backend 驗收成功。
真實 Databricks 仍待核准外部環境，PR 保持 Draft；不修改主分支、不執行 Merge。

後續驗證：兩個 source image 均已完成包裝，實際 bucket／S3 寫入讀回通過，測試
container／volume／network 已移除。更新後的 push／PR Contracts 與 Ingestion jobs
均通過，Backend 進到 MLflow 啟動後發現獨立 server image 安裝了 SQLAlchemy 2.1.8，
使 MLflow 3.6 無法 import `FallbackAsyncAdaptedQueuePool`。
`Dockerfile.mlflow` 現沿用主系統的 `SQLAlchemy>=2.0.44,<2.1` 契約，並在 build
階段 import Tracking／Registry SQL stores；本地 SQLAlchemy 2.0.57 的兩個 store
imports 通過。完整服務結果仍以修復後 Actions 為準。

完整服務測試另確認 MLflow 3.6 的 artifact proxy 需要標準
`MLFLOW_TRACKING_URI`；只有主系統的 `ML_TRACKING_URI` 時，SDK 仍可能使用
`file:///app/mlruns`，使完成訓練的 Job 留在可恢復的 REGISTER phase。
Compose 現設定相同的兩個 endpoint，Serving child 的受限白名單也保留此別名；
不設定全域 active run，不擴大一般 MLflow／DB／Broker／AWS／Databricks secrets
的繼承範圍。新的 Serving launch regression 覆蓋 endpoint 與 credential 邊界。
本地 explicit client 的真實 MLflow／S3 artifact 上傳、下載與 SHA256 核對通過。

Backend CI 的限定啟動清單原本沒有啟動 Redis，兩個必要 Receiver tests 因 DNS
解析不到 `redis` 而失敗。啟動清單與 acceptance dependency 現明確包含 Redis，
保留兩個真實 Receiver tests 與零 skipped 的驗收條件。

Serving 的受限環境也需要有效 home：自訂 UID 未出現在 image passwd database 時，
缺少 HOME 會使 MLflow import 的 `Path.home()` 失敗。每個 deployment 現在取得
execution directory 下私有 `0700` home，不繼承父程序 home 或其 credentials；
既有 home／symlink 會拒絕啟動。未知 UID、實際子程序 cache write、symlink 與
credential-filtering 回歸通過。

補齊以上設定後，以獨立的完整真實 Backend stack 執行三個目標整合案例：
**3 passed、零 skipped**，涵蓋訓練、MLflow／S3 registry、部署、推論、generation
rollback、JWT revocation，以及兩個真實 RabbitMQ／Redis Receiver 案例。
這是目標整合驗證；完整 suite／其他 qualification 的最終結果仍以更新後 CI 為準。
