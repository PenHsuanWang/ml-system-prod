# Universal Data Ingress 任務完整性複查

複查日期：2026-11-06（Asia/Taipei）。結論：**尚未全部完成，不能通過原任務 Definition of Done。**

本次依原始 Engineering Implementation Task 的範圍，檢查目前工作樹，而非只檢查已提交的 HEAD。File → immutable Raw Snapshot 的主流程、持久 metadata、Registry／OCP 與關鍵復原測試已存在；仍有四項可重現的程式缺陷，以及文件／交付缺口。這次複查沒有修正 production code、提交或推送。

## 1. 必須修正的問題

| 優先級 | 發現 | 影響與證據 | 建議修正／新增驗收 |
| --- | --- | --- | --- |
| P1 | 正式 Management API factory 無法註冊 readiness route | [bootstrap/ml_api.py:109](../../bootstrap/ml_api.py#L109) 的 route 使用 `dict[str, bool] | JSONResponse` 回傳型別，未停用自動 response model。FastAPI 0.124.0 在註冊時拋出 `FastAPIError: Invalid args for response field`。這段程式不受 Ingress enable flag 保護，因此影響原本 Management API 啟動。 | 明確設定合法 response model／停用此 route 的推導；以正式 factory、正常 lifespan 測試啟動，涵蓋 Ingress 開啟與關閉。 |
| P2 | Publication／reconciliation 階段沒有 lease 續期 | [supervisor.py:127](../../src/application/ingestion/supervisor.py#L127) 的 `_publish()` 呼叫 lookup／publish／驗證時不 heartbeat。[ingestion_jobs.py:270](../../src/infrastructure/ingestion/persistence/ingestion_jobs.py#L270) 會正確拒絕過期 owner，導致已發布資料無法記錄 SQL 完成。 | 為發布與對帳提供受控 keepalive／可執行的時間界線；測試驗證耗時超過原 lease 後仍可持續前進，並維持 stale-owner fencing。 |
| P2 | 發布前 `SnapshotConflict` 被錯誤地當成可對帳結果 | [supervisor.py:94](../../src/application/ingestion/supervisor.py#L94) 無條件呼叫 `reconcile_later`。在 `prepare()` 故障注入此錯誤時，尚無 durable intent，Repository 再拋 `IngestionError`，工作留在 `VALIDATING`。相鄰的通用 `Conflict` catch 也未區分業務錯誤與失去 ownership。 | 依 durable intent、目前 owner／attempt／lease 和 phase 分流：發布前 fail／retry，發布後 reconcile，失效 owner 不寫入；補上各分支測試。 |
| P2 | 有界 cleanup 沒有跨次執行的掃描進度 | [raw_snapshot_writer.py:562](../../src/infrastructure/ingestion/storage/raw_snapshot_writer.py#L562) 每次從頭列舉，5,000 entries 上限連空 job parent 都計入；[discard():665](../../src/infrastructure/ingestion/storage/raw_snapshot_writer.py#L665) 不刪空 parent。較後方 orphan 永遠掃不到。Publication temp 掃描也將 final snapshots 計入上限。 | 引入公平分頁／可恢復 cursor，並適當移除空 parent；驗證超過 5,000 歷史目錄時，多次 sweep 最終可處理全部合格 orphan。 |

第一項以相同 route 宣告獨立重現，未冒稱在本機成功執行整個 production factory。本機 Python 3.10 另有既有 `enum.StrEnum` 匯入限制；route 註冊錯誤本身已由實際 FastAPI 執行證明。現有 [HTTP 測試](../../tests/ml/ingestion/test_http_e2e.py) 使用 `services=` 注入分支，在 readiness route 宣告前已返回，因此綠燈無法排除這項退化。

兩個 durability probe 使用獨立暫存 SQLite、真正 File acquisition 與 Raw Store；只在 probe 中注入特定錯誤／延遲。Cleanup probe 使用真正 filesystem 目錄。完整程式與輸出保存在 [本次證據](../evidence/data-ingress-audit-2026-11-06/result.json)：

- [Readiness probe](../evidence/data-ingress-audit-2026-11-06/readiness-repro.py.txt)／[輸出](../evidence/data-ingress-audit-2026-11-06/readiness-repro.log)：重現 FastAPI route 註冊失敗。
- [Durability probes](../evidence/data-ingress-audit-2026-11-06/durability-repro.py.txt)／[輸出](../evidence/data-ingress-audit-2026-11-06/durability-repro.log)：intent 前衝突停在 `VALIDATING`；合法 0.2 秒 lease 配合 0.3 秒 lookup，在三次完成嘗試後耗盡 reconciliation budget，snapshot 已存在但 SQL 尚未完成。預設 30 秒 lease 在驗證超過 30 秒時有同類問題。
- [Cleanup probe](../evidence/data-ingress-audit-2026-11-06/cleanup-repro.py.txt)／[輸出](../evidence/data-ingress-audit-2026-11-06/cleanup-repro.log)：5,100 個 job 目錄下，連續三次 sweep 刪除數為 `[0, 0, 0]`，過期 staging 仍存在。

這些 probe 的 exit code 0 表示「缺陷成功重現」，不是功能驗收通過。尚未把這些情境加入 production regression suite。

## 2. 文件與交付缺口

- [實作報告](data-ingress-implementation-report.md) 原內容只有 baseline／初步範圍與環境說明，最後承諾待補最終結果；原任務要求的實際檔案清單、完整測試證據表、狀態分類、後續 PR 與最終 Git 狀態尚未交付。這份複查報告記錄現況，不代表原實作報告已補齊。
- [developer-guide.md:1212](../developer-guide.md#L1212) 與 [操作指南:297](../data-ingress-implementation.md#L297) 要求執行 `python scripts/check_ingestion.py`，實際沒有該檔案。應交付入口或改成真實可執行命令。
- [implementation-status.md](../implementation-status.md) 尚未記錄本輪 Ingress 能力與驗證；現有 468 測試是有標明日期的舊證據。
- [操作指南:234](../data-ingress-implementation.md#L234) 說 `.publish-*` cleanup 尚未實作，但目前程式與測試已有此功能；指南也未說明已存在的 operator `ingestion_cli reconcile` 操作。須同步實際行為與已知限制。
- 既有 CI 會透過 `pytest tests/ml` 發現新 Ingress tests，但 Mypy 命令目前只列舊 core／EBM slice；建議將本輪已執行的 Ingress strict typing 範圍加入持續驗證，避免只留下手動紀錄。

## 3. 原任務 Definition of Done 逐項結果

| 項次 | 原驗收條件 | 結果 | 證據／限制 |
| --- | --- | --- | --- |
| 1 | 核准 File Adapter 可取得真實檔案 | 已實作／本機通過 | JSONL File tests、HTTP admission + 獨立 worker subprocess |
| 2 | 由模型無關 Application Service 協調 | 已實作 | `IngestionService`／`IngestionSupervisor`，沒有模型特徵轉換 |
| 3 | 驗證並發布 immutable Raw Snapshot | 已實作／本機通過 | bounded staging、digest、Linux no-replace publication |
| 4 | 持久 Catalog 可取得 metadata | 已實作／本機通過 | 獨立 raw catalog 與 SQL terminal transaction |
| 5 | EBM／MLP Dataset 契約保持不變 | 本機回歸通過 | 未修改既有 Dataset／Trainer 契約；原 ML suites 包含於 635 項通過結果 |
| 6 | 第二 Adapter 不修改 Service 即可加入 | 已實作／本機通過 | File、Memory、Generated 的共用 contract／完整 service workflow |
| 7 | Domain／Application 無 concrete SDK 依賴 | 通過 | dependency boundaries；純 DTO／Protocol |
| 8 | Bootstrap 擁有 concrete 建構／註冊 | 已實作 | explicit immutable approved registry |
| 9 | 安全拒絕未知／未授權來源 | 本機通過 | source、capability、tenant／grant tests；worker 執行時重驗 |
| 10 | 重複請求冪等 | 本機通過 | SQL key scope、fingerprint、concurrent submission／delivery tests |
| 11 | 中斷發布不暴露 partial Snapshot | 本機通過 | private staging／publication temp、atomic visibility、驗證 tests；未資格驗證 power-loss／NFS |
| 12 | 不確定完成有測試過的對帳路徑 | 部分完成 | 指定 SQL finish failure 案例通過；publication lease 與 phase-error handling 仍有缺陷 |
| 13 | 不破壞既有 Training／Serving | 未通過 | 數值與工作流回歸通過，但正式 Management API factory 的啟動退化未修正 |
| 14 | 執行並回報 contract／regression tests | 部分完成 | 本次有本機證據；supported runtime、PostgreSQL／RabbitMQ、完整 template bootstrap 尚未驗證 |
| 15 | 文件準確反映實作 | 未完成 | 報告、狀態、驗證入口與操作說明仍有上述缺口 |

指定的關鍵復原案例並未缺漏：[test_recovery.py:74](../../tests/ml/ingestion/test_recovery.py#L74) 使用真實 SQLite trigger 阻擋 `PUBLISHED` SQL update；下一個 worker 解析原 snapshot，保留 identity／digest，來源變更後也不重新取得。這項測試有執行並通過；它沒有覆蓋慢速 publication lease 情境。

Databricks／Kafka 本輪只要求 extension contract 與文件，**沒有 operational adapter 是正確的範圍界線**。這兩者及 Dataset Builder 的後續邊界已有文件，實際整合仍為未實作／未驗證；不能將 Raw Snapshot 視為已建立 EBM FeatureSnapshot。

## 4. 本次實際執行的檢查

環境：Python 3.10.12、Torch 2.0.1、NumPy 1.26.4、FastAPI 0.124.0、Pydantic 2.8.2、SQLAlchemy 2.0.44、Pytest 8.3.2、Mypy 1.11.1、Ruff 0.17.3。這是既有快取環境，低於專案 Python >=3.11 與 Mypy >=1.15 要求；不能當作 Python 3.12／Torch 2.6 目標 image 的驗收。

| 檢查 | 範圍／環境 | 實際結果 |
| --- | --- | --- |
| Ingress Pytest | SQLite、真實 File／Raw Store、ASGI、獨立 worker child process | **167 passed**，10.43 秒 |
| ML + boundary Pytest | `tests/ml tests/test_boundaries.py`，既有 host selector workaround | **635 passed、3 skipped、1 warning**，41.17 秒；[JUnit](../evidence/data-ingress-audit-2026-11-06/ml-junit.xml) |
| `python scripts/check_boundaries.py` | 既有九層依賴規則 | PASS |
| Strict Mypy | 修改的 production modules，包含 ingress、bootstrap、migration；使用既有 `--follow-imports=skip` 慣例 | 28 source files PASS；舊版工具／host 限制如上 |
| `python scripts/check_ebm_typing.py` | 既有 EBM slice | 28 source files PASS；同上限制 |
| Pyflakes | `bootstrap src tests/ml` | PASS |
| Scoped Ruff check | 新 Ingress slice 與修改的 Python production files／tests | PASS |
| New-slice Ruff format check | 新增 Ingress Python／migration／tests | 37 files already formatted |
| SQLite migration | 獨立暫存 DB，upgrade head → downgrade 0005 → re-upgrade head | PASS |
| `ruff check .` | 整個工作樹 | FAIL：1,896 diagnostics，其中 Notebook 1,549、其餘 347；HEAD archive 基線已有 351。新 Ingress slice 無 diagnostics，未修改既有 Notebook |
| `ruff format --check .` | 整個工作樹 | FAIL：102 files；HEAD archive 已有 99。工作樹另包含原有未追蹤 Notebook／提案 Markdown，兩個修改的 production files 已完成格式化 |
| Plugin／module regression | `tests/test_plugin_loader.py tests/test_modules.py` | **NOT VERIFIED**：收集失敗，Python 3.10 缺 `enum.StrEnum`，host cache 缺 `taskiq` |
| PostgreSQL／RabbitMQ／目標 runtime | 本次無指定測試 URLs／隔離專案 stack | **NOT VERIFIED**；未拿既有其他服務的資料庫或 Kafka 當測試環境 |
| Readiness／durability／cleanup probes | 真實框架或獨立暫存 SQLite／filesystem | 四項缺陷均成功重現；詳見上方證據 |

本機完整 ML 命令：

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYTHONPATH=docs/evidence/ml-cpu-2026-10-29:/tmp/ml-ebm-deps:. \
python -m pytest tests/ml tests/test_boundaries.py -q \
  -p pytest_asyncio.plugin -p host_selector_plugin \
  -o faulthandler_timeout=30 --junitxml=/tmp/ingress-audit-ml-tests.xml
```

Mypy 命令：

```sh
PYTHONPATH=/tmp/ml-ebm-deps:. python -m mypy --follow-imports=skip \
  src/domain/ml.py src/domain/ingestion src/application/ingestion \
  src/infrastructure/ingestion src/api/ingestion \
  bootstrap/ingestion_wiring.py bootstrap/ingestion_cli.py bootstrap/ml_api.py \
  migrations/env.py migrations/versions/0006_ingestion.py
```

Probe 重跑（在根目錄，需已安裝依賴；本機另使用 `/tmp/ml-ebm-deps` 快取）：

```sh
PYTHONPATH=. python docs/evidence/data-ingress-audit-2026-11-06/readiness-repro.py.txt
PYTHONPATH=. python docs/evidence/data-ingress-audit-2026-11-06/durability-repro.py.txt
PYTHONPATH=. python docs/evidence/data-ingress-audit-2026-11-06/cleanup-repro.py.txt
```

## 5. 結案前的工作順序與 Git 狀態

1. 修正正式 Management API 啟動退化，補非注入 factory smoke test。
2. 修正 publication lease、phase-aware conflict handling 與 cleanup 公平掃描，將本次 probes 轉成必要 regression tests。
3. 補齊實作報告、操作命令與狀態；交付有效的 typing／quality entrypoint。
4. 在支援的 runtime 執行完整 bootstrap、PostgreSQL／既有 RabbitMQ 回歸，清楚區分 baseline lint 問題與本輪退化，再重新判定 DoD。

不需把 Databricks、Kafka 或 Dataset Builder 的正式開發塞入上述修正，才能完成本輪 File vertical slice 的範圍。

複查 HEAD 仍為 `edc140061f7cac7d0b6881f35dded4a2a92a8d6c`，branch `master`。Ingress code、migration、tests 與部分文件仍在工作樹，尚未提交。原有 Notebook 與 proposal documents 保留；ADR-015 仍是 Proposed。本次只新增此 audit 與 evidence，並在原實作報告追加複查連結；沒有變更 production code、重設工作樹、提交或推送。
