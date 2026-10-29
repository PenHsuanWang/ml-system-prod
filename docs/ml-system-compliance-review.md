# ML 設計 v0.2：現有平台基線符合性審查

| 項目 | 本輪實際範圍 |
| --- | --- |
| 日期 | 基線執行 2026-10-26；文件彙整 2026-10-27，Asia/Taipei |
| 指定基線 | `08490e84cc88d2ff7300cb97fd23e93ea0e33a1a` |
| 審查開始 HEAD | `629d23b57fc35c8a801405c595ee4c84d2df0d4f` |
| 方法 | git archive 指定 commit 至隔離目錄，使用現有 venv，assert server／bootstrap import 來源 |
| 來源比對 | 135 個程式、測試、pyproject 與 hash locks 檔案，基線／目前 bytes 完全相同 |
| 設計 | [ML-SAD-001 v0.2](ml-system-design.md)；ML 實作仍未新增 |

## 1. 實際檢查

| 檢查 | 結果／限制 |
| --- | --- |
| Import origins | 指向 archived commit 內的 server／bootstrap |
| Import Boundary | 通過 `scripts/check_boundaries.py` |
| Strict Mypy | 108 source files 通過 |
| Non-integration pytest | 440 passed、4 deselected，4.95 秒 |
| pip check | No broken requirements found；沒有重新解析／安裝依賴 |
| 依賴環境 | Python 3.13.11、Taskiq 0.12.6、taskiq-aio-pika 0.6.1、pytest 9.1.1、Mypy 2.4.0 |

使用的是現有 `.venv`，不是 fresh-install／新 image 驗證。已移除 APP_ 等產品測試環境影響，且以 archive 作 CWD／PYTHONPATH；詳細命令與來源 hash 見 [原始 report](evidence/ml-design-review-2026-10-26/20261026T154229Z-a10035/report.json)、[source manifest](evidence/ml-design-review-2026-10-26/20261026T154229Z-a10035/source-manifest.json)與 [JUnit](evidence/ml-design-review-2026-10-26/20261026T154229Z-a10035/baseline-unit.xml)。

## 2. 可核對的程式事實

| 設計敘述 | 實作依據／本輪結論 |
| --- | --- |
| App Factory／lifespan、role api／worker、ModuleCatalog | server.py／bootstrap/components.py；仍符合文件的現況敘述 |
| Application 純層、Infrastructure 不 import Application | scripts/check_boundaries.py；新 ML ports 可用 structural typing 保持此政策 |
| submit 每次 UUID、dispatch 重寫 queued | src/application/jobs/service.py／infrastructure/messaging/distributed.py；stable command 接縫尚待實作 |
| Redis 快照有 TTL | RedisJobStore；不是永久 TrainingJob repository |
| async handler、受限 thread 無法硬停止 | bootstrap/worker.py／services/concurrency.py；獨立 TrainingExecutor 尚待實作 |
| identity 依賴 MongoDB | Settings 驗證與 bootstrap/identity.py；PostgreSQL 不能只靠改 URI 取代 |

## 3. Taskiq ACK 的實際限制

本輪讀取已安裝 Taskiq 0.12.6 的 receiver。其順序包含 post_execute middleware，再進入 result-backend save 的 try／except，最後依 when_saved ACK；某些保存例外可能被 catch 後繼續執行。

本專案 SnapshotPersistenceMiddleware 在 post_execute 持續保存 RedisJobStore，取消會保留未 ACK delivery。既有 `tests/test_distributed.py` 使用真 Receiver＋InMemoryBroker、FakeStore 與 ack callback，測試保存先於 ACK、儲存失敗／恢復、取消、重送與終態。這些案例包含在本輪 440 tests。

它們沒有驗證 PostgreSQL TrainingJob。ML adapter 必須新增 [§13.3 App DB evidence gate](ml-system-design.md#133-ackbroker-與-redis)，特別防止一般 DB 例外被 generic failed-result 路徑吸收後提前 ACK。

Receiver source SHA-256：`b5a4a0cf9f73aadd15d938b2c5541b8d1d5435de24032ec60d50e282816e5b89`。此觀察只對本輪版本成立；升級 Taskiq 必須重跑 receiver-level 行為測試。

## 4. 未執行／尚不可宣告通過

- 本輪沒有執行真 RabbitMQ／Redis／Mongo integration，4 項以 marker 排除。
- 尚無 ML PostgreSQL repositories、Outbox／claim／ACK gate 的真 DB contract tests。
- 尚無 Trainer／Evaluator／MLflow／Artifact／ModelLoader／DeploymentSpec／Routing 的 ML E2E。
- 尚無 qualified runtime compatibility、Production load／SLO／restore 結果。
- 沒有 fresh dependency install、container build／scan 或遠端 CI 執行證據。

本輪結論是「既有平台基線現況已以指定 commit 核對與非整合測試確認」，不是「ML-SAD-001 v0.2 已由現有程式完整實現」。ML-T26～40 等新契約是後續實作的驗收要求。
