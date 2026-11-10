# CI 容器與 EBM 後續修復

日期：2026-11-09。延續同一個 Draft PR #1，不修改 `master`，不執行合併。

Ingestion 容器原本以非 root 使用者在 `/app` 建立 Ruff cache，因權限不足而使
品質檢查失敗。`ingestion-test` stage 現將 Ruff 與 Mypy cache 放在 `mlapp` 的 home，
保留 UID 10001 及原有檢查指令，不放寬目錄權限或停用檢查。

EBM JSON reader 原本依賴 `json.loads` 的 `RecursionError` 拒絕過深輸入，
在 Python 3.12 不能穩定成立。共用 parser 現在對已受 bytes 上限保護的解析結果
進行迭代檢查，限制最多 64 層 object／array（包含根 object），再交給契約驗證。
這項限制涵蓋 manifest、檔案及不可變 bytes；保留 duplicate keys、非有限數值、
UTF-8／16／32 及字串 escape 的既有處理。

另為三個 NumPy array 補上具體 dtype，並將 NPY public helpers 的四處失效
`type: ignore` 改為明確的 callable 邊界型別。NumPy 1.26 的 helper 沒有完整型別
註記，因此這個邊界同時支援 1.x 與 2.x，沒有縮小依賴範圍或增加 suppression。
訓練、特徵及模型演算法保持原有計算。

驗證結果：

- Python 3.12.16、Torch 2.6.0 CPU、NumPy 2.5.4 下，ML／boundary／plugin／module
  回歸為 **915 passed、0 failed、4 skipped**，使用正常 event loop。
- Artifact＋Dataset 測試 **111 passed**，包括 64／65 層 array、object、混合結構邊界，
  既有 2,000 層失敗案例，以及 encoded／escaped strings。
- EBM strict Mypy **28 source files 通過**；NumPy 1.26.4 的真實 stubs 透過隔離 overlay
  與停用 incremental cache 另行驗證，同樣 28 source files 通過。這不代表已完成
  NumPy 1.26 的 Python 3.12 runtime 驗收。
- Ingestion scoped Ruff／format／Mypy（36 source files）、核心 Mypy（34 source files）、
  Pyflakes 及依賴邊界通過。
- 實際 `ingestion-test` image 以預設 UID 10001 執行原有品質檢查通過；搭配全新隔離
  PostgreSQL 17 執行 migration、health 及 Ingestion tests，**372 passed、零 skipped**。
  這個 scope 明確排除 Compose CLI parser cases 與需要真實 Warehouse 的 case。
  測試自己建立的 PostgreSQL container／network 均已移除。
- 合成資料 CPU training／evaluation／bundle，以及獨立程序的 bundle verify 通過。

完整回歸的四個 skipped cases 需要真實 Databricks、Backend stack 或 RabbitMQ／Redis；
本機結果不替代這些服務驗收。修復程式版本 `32d35aa` 的 GitHub Actions 已通過
Ingestion container、SQLite Contracts 及 PostgreSQL Contracts（包含 EBM typing、
回歸及 SQLite job 的獨立 CPU smoke／bundle load）。
後續 Backend stack job 在啟動服務時因 `minio/minio:latest` pull access denied 而失敗，
未執行 Backend 驗收，整體 workflow 尚未通過；沒有替換未核准 image 或跳過 gate。
詳見 [GitHub Actions](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42510103965)。
沒有核准 Databricks fixture，因此仍未完成真實 Databricks 驗收；PR 保持 Draft。

機器摘要與測試輸出見 [修復證據](../evidence/ci-ebm-fixes-2026-11-09/result.json)。
2026-11-08 的證據保留為修復前紀錄，不改寫當時失敗或 skipped 的結果。
