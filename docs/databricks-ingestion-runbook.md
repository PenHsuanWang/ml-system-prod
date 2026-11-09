# Databricks Batch Ingestion 操作手冊

更新：2026-11-08。狀態：已實作，真實 Warehouse 驗收待執行；PR 必須維持 Draft。
設計決策見 [ADR-016](adr/016-databricks-batch-ingestion.md)，驗收結果見
[本輪交付報告](reviews/ingestion-stabilization-databricks-2026-11-08.md)。

## 執行邊界

`databricks-sql@1` 使用官方 SQL Connector 4.2.6 與 SDK OAuth M2M。
它只讀取核准 Unity Catalog Delta table 的固定版本，產出 private staging／
immutable JSONL Raw Snapshot；不建立 ML DatasetVersion、不呼叫 Trainer、
不進行 Spectrogram／Normalizer／split／MLflow 操作。

API 只提交 durable Job；另一個 SQL polling worker 取得資料。Application 的
`BatchSourceAdapter.acquire()`、Registry 與 Supervisor 保持來源無關，沒有
Databricks 類型分支。SDK／credentials／cursor 僅存在 Infrastructure。

## 核准設定與憑證

使用僅具 Warehouse 使用權與核准 table SELECT 權限的 Service Principal。
由 operator 管理兩份 JSON；HTTP 不接受 endpoint、SQL、token 或 import path。
檔案必須為核准的 absolute regular files，不使用 symlink。保留每個版本的
設定及其 digest，供歷史 lineage／Dataset Builder 使用；不要覆寫後丟失舊版本。

Connections JSON：

```json
{
  "production-warehouse": {
    "server_hostname": "dbc-approved.cloud.databricks.com",
    "http_path": "/sql/1.0/warehouses/0123456789abcdef",
    "client_id": "approved-service-principal-id",
    "client_secret_file": "/run/secrets/databricks_client_secret"
  }
}
```

Sources JSON：

```json
{
  "approved-events-v1": {
    "connection_ref": "production-warehouse",
    "table": ["main", "approved", "events"],
    "delta_version": 42,
    "projection": [
      {"name": "event_id", "type": "integer"},
      {"name": "observed_at", "type": "timestamp"},
      {"name": "value", "type": "decimal"}
    ],
    "predicates": [{"column": "event_id", "operator": "ge", "value": 1}]
  }
}
```

Identifier 僅接受受控 ASCII SQL identifier；table 必須是 catalog／schema／table
三段。每個 template 最多 128 個唯一 projection、32 個 predicates。運算子為
`eq/ge/gt/le/lt/is_null`；predicate column 必須位於 projection。值使用 native
bound parameters。查詢固定使用 `VERSION AS OF`、完整 projection 排序與
`LIMIT max_records + 1`；多出的那一筆用於拒絕超限，不會靜默截斷。

OAuth secret 存在 worker 可讀、其他使用者不可讀的 regular file，最多 4096 bytes，
例如 owner-only 0400／0600。Container 的 `ML_UID/ML_GID` 必須與檔案 owner
及 shared runtime volume 權限相容。secret 只掛載 worker，Management 不需要
讀取它。Connections、Sources、secret 與 approved input 都放在 shared `./var`
之外，避免 readonly mount 同時透過其他 writable alias 被改寫。

Public endpoint／client ID／query schema 參與 template digest，secret 內容與
secret-file 路徑不參與。輪替 secret 不變更 source version；變更 table、Delta
版本、projection、predicate 或 public connection identity，必須重新核准。
既有 Source Definition 的 digest 不匹配時會 fail closed，不會改讀新版資料。

## 啟用、註冊與執行

```sh
export ML_INGESTION_ENABLED=true
export ML_DATABRICKS_ENABLED=true
export ML_DATABRICKS_CONNECTIONS_FILE=/absolute/approved/connections.json
export ML_DATABRICKS_SOURCES_FILE=/absolute/approved/sources.json
export ML_RAW_SNAPSHOT_ROOT=/absolute/raw-snapshots
# ML_DATABASE_URL 使用核准的資料庫設定；不要將真實密碼提交到版本控制。
python -m bootstrap.ml_cli migrate
python -m bootstrap.ingestion_cli health
python -m bootstrap.ingestion_cli templates
```

`templates` 只輸出核准 reference／digest。將輸出 digest 填入完整 SourceDefinition：

```json
{
  "source_id": "approved-events",
  "version": "v1",
  "tenant_id": "tenant-a",
  "adapter_type": "databricks-sql",
  "adapter_version": "1",
  "configuration_json": "{\"template_ref\":\"approved-events-v1\",\"template_digest\":\"<digest-from-templates-command>\",\"serialization_version\":\"dbx-jsonl-v1\"}",
  "limits": {"max_bytes": 16777216, "max_records": 100000, "max_line_bytes": 1048576, "timeout_seconds": 30},
  "data_format": "jsonl",
  "schema_version": "raw-json-object-v1",
  "allowed_actor_ids": ["approved-actor"]
}
```

```sh
python -m bootstrap.ingestion_cli register-source --definition-path /absolute/approved/source.json --tenant-id tenant-a
python -m bootstrap.ingestion_cli work
```

沿既有 Bearer／`ml:ingest` 權限提交 `POST /api/v1/ingestion/jobs`，帶
Idempotency-Key 與 `{"source_id":"approved-events","source_version":"v1"}`。
202 代表已接受工作；只有 result endpoint 返回已提交的 `PUBLISHED` metadata
才代表成功。Raw payload 的 downstream reader 仍需驗證 manifest／content digest。

## 格式與有界資源

`dbx-jsonl-v1` 每筆為一個 object，projection 順序固定，保留 Null；boolean／
integer 嚴格分型，float 必須有限，Decimal 以有界的精確 fixed-point 字串保存，
date 為 ISO date，timestamp 為 UTC microseconds `Z`（naive timestamp 明定視為 UTC），
binary 為 base64。未知型別不使用 `default=str`。即使值全為 Null，SQL metadata
型別與核准 schema 不一致也拒絕。跨型別／格式演進需新的 serialization version。
空查詢結果沿用既有 Raw Store 的非空政策，會拒絕而不發布空 Snapshot。

SDK 使用 `fetchmany()`、最多 128 rows／batch、受控 result buffer、停用 cloud
fetch 與 telemetry。records／bytes／line size／timeout 超限使整次擷取失敗。
SDK 在獨立 spawned process；parent 以 bounded byte messages 與單格 queue
接收，partial IPC frame 不阻斷 deadline／cancel／lease checks。逾時、取消或失去
ownership 時會回收子程序；原始 SDK 錯誤、stdout／stderr 不寫入公開回應或 logs。

Driver socket timeout、SQL `STATEMENT_TIMEOUT` 與 parent deadline 各自有作用。
強制終止 client 不代表已收到 remote query cancellation acknowledgement；查詢
是唯讀且固定版本，未知結果不發布，重試可能重讀同一版本，不宣稱 query exactly-once。

## 失敗、對帳與維運

- Acquisition failure 使用有界重試；認證／來源／schema 錯誤安全拒絕。
- Worker 擷取前重驗 Source eligibility、tenant／actor grant、configuration digest；
  deployment template drift 也會拒絕。停用用 `disable-source`，恢復用 `enable-source`。
- Supervisor 在 blocking I/O 期間續租，在 publication／finish 邊界重新驗證 owner。
  Pre-intent conflict 不會進入 reconciliation；stale worker 不覆寫新的 owner 或終態。
- Intent 保存後取消回 409；若 filesystem 成功但 SQL finish 失敗，保留原 snapshot
  identity／manifest／staging，下一個 worker 對帳既有成果，不重新擷取來源。
- 自動 reconciliation budget 耗盡後，先檢查存放區與已存在的 snapshot，再用
  `python -m bootstrap.ingestion_cli reconcile --tenant-id tenant-a --job-id <id>`
  重啟同一候選的對帳。不可刪除 uncertain evidence 來強迫換一份資料。
- `cleanup --minimum-age-seconds 86400` 保留所有 nonterminal jobs，清理舊 private
  staging／publication copies。每個 namespace 每次掃描 5000 entries，cursor 以
 原子 checkpoint 與 lock 持久保存；多次程序啟動會繼續前進，final snapshots 不刪除。
- Raw store 的 publication／cleanup 需要 Linux `renameat2`／glibc `getdents64`。
  不將本機／container 測試外推到 NFS、跨主機 clock skew 或 power-loss durability。
- DB heartbeat 呼叫仍受資料庫／driver timeout 設定與 container supervisor 管理；
  正式上線需配置並驗證 connect／statement／lock timeout 與網路隔離故障行為。

`health` 驗證設定／storage 初始化／SQL schema，**不查詢 Warehouse、不驗證遠端
憑證**。`/metrics` 需 `ml:operate`，新增 `ml_ingestion_jobs{state}`、
`ml_ingestion_attempts{state}`、`ml_ingestion_reconciliation_exhausted` 與
`ml_ingestion_oldest_pending_age_seconds`；沒有 tenant、source、job 或 query labels。
監控 FAILED／長時間 pending／reconciliation exhausted，搭配受權限保護的 Job error code。

## Container 與驗收

複製 `.env.ml.ingestion.example` 為本機未追蹤設定，再沿既有 `.env.ml` 啟動：

```sh
docker compose --env-file .env.ml --env-file .env.ml.ingestion --profile ingestion -f compose.ml.yaml -f compose.ml.ingestion.yaml -f compose.ml.databricks.yaml up --build -d management ingestion-worker
python scripts/check_boundaries.py
python scripts/check_ingestion.py
python -m pytest tests/ml/ingestion -q
```

真實 qualification 使用專用 PostgreSQL 測試 DB（fixture 只建立／刪除自己的 UUID
schema）、核准且非空的小型 Delta fixture。設定 `ML_TEST_DATABASE_URL`、上方兩個
config files、`ML_TEST_DATABRICKS_TEMPLATE_REF`、`ML_TEST_DATABRICKS_EXPECTED_RECORDS`
及獨立核准的 `ML_TEST_DATABRICKS_EXPECTED_SHA256`，再執行：

```sh
python -m pytest tests/ml/ingestion/test_databricks_live.py -q --junitxml=databricks-live.xml
```

CI 有獨立的 ingestion-container PostgreSQL gate；live gate 透過手動 dispatch、
`databricks-qualification` protected environment 與完整 fixture／secret 設定執行，
要求零 skipped。缺少環境時維持 Draft，不能用 SDK 替身或 skipped test 宣稱驗收成功。
Backend stack 驗收明確分開這個外部 Warehouse gate；其成功不代表 Databricks
已完成 qualification。Backend report 會列出獨立驗收範圍與原因。
Kafka、incremental extraction、Parquet publisher、remote Spark Jobs、Dataset Builder
及 Raw-to-Feature／Training orchestration 均不在本次 MVP 範圍。
