# ML runtime operations

For first installation, external MLflow configuration, private role files, migration and release Go/No-Go, start with the [Day-1 deployment runbook](deployment/day1-deployment-runbook.md). This document covers Day-2 ownership, recovery and coordinated backup/restore. The reference `compose.ml.yaml` is a development stack; it is not the external production profile.

Run from the repository root. Apply `python -m bootstrap.ml_cli migrate` before upgraded roles start. Startup never creates tables.

In the production profile, execute approved maintenance through its secret-loading role entrypoint (`docker compose ... run --rm --no-deps ...`); a bare `exec` does not reload private role configuration. Preserve the Controller's host/process identity and shared execution/network assumptions when deciding whether a maintenance command may reconcile a live generation.

Actual defaults, environment keys and profile overrides are in the
[configuration reference](configuration-reference.md). Lifecycle JSON examples start in
the [README](../README.md); EBM schemas/policy and Ingestion have their own linked guides.

## API 與 client 契約

Management／Gateway／Serving 使用目前 Identity session 的 Bearer token。帳號不會在
啟動時自動建立；`manage.py create-admin` 的 dot permissions 對映為以下 colon scopes。
權限不授予跨 tenant 存取；API 不接受 caller 自選 actor／tenant／import path。

| 操作 | 實際授權／結果 |
| --- | --- |
| Training submit | `ml:train`；202；必填 `Idempotency-Key`，1–128 visible ASCII characters |
| Job／attempt 查詢 | 同 tenant 的 owner，或 `ml:jobs:read_all`；不符合時 404 |
| Training cancel | 上述可見性加 `ml:cancel`；202；已終態為 409 |
| Dataset metadata | `ml:train` 或 `ml:dataset:read`；版本格式 `dataset_id@version` |
| Model／versions／evaluation 查詢 | `ml:model:read` |
| 以 binding ID 人工 Approval | `ml:model:approve`；201；必須有同 binding／manifest 的 `APPROVED_BY_POLICY` evaluation |
| 以 name/version 人工 Approval | 上述 approve 加 `ml:model:read`（會先列出版本） |
| Deployment create／read／rollback | `ml:deploy`；create／rollback 為 202，須輪詢 observed state |
| Gateway／Serving prediction | `ml:predict`；model input schema、endpoint 與 route identity 都須相符 |
| Management metrics | `ml:operate`；200 text exposition |

Training 冪等 scope 為 tenant + actor + `training.submit.v1` + key。有效期內相同
normalized intent 返回同一 Job，不同 intent 為 `IDEMPOTENCY_CONFLICT`。預設有效期
86400 秒；過期後相同 key 可以建立新 Job。Ingestion 使用獨立 durable replay 契約，
詳見 [Ingestion API](data-ingress-implementation.md#5-http-使用與授權)。
明確重試使用新 key 與 top-level `retry_of_job_id`，只接受可見的終態原 Job；
不能把 broker redelivery 當作重試 API。

公開 Job states 為 ACCEPTED、QUEUED、RUNNING、RETRY_WAIT、CANCEL_REQUESTED、
NEEDS_RECONCILIATION、SUCCEEDED、FAILED、CANCELLED，僅最後三者為終態。
`phase`、`revision`、`attempt_id`、`error_code` 是對帳線索。未 dispatch 的取消可直接
CANCELLED；已啟動者必須先確認實體停止，202 不代表已停止。HTTP Deployment 的
`desired_state=ACTIVE` 是意圖欄位，不能取代 observed ACTIVE、SQL route 與 runtime
identity 檢查；K8s Pod Ready 也不代表獲准切流。

ML domain errors 使用 flat `code`、`message`、`request_id`、`retryable`；422 schema
拒絕不回顯輸入。401／403／404 分別處理身份、權限與不可見資源，409 表示 intent／
狀態衝突，429 `CAPACITY_EXCEEDED`，503 `DEPENDENCY_UNAVAILABLE`，Serving timeout
為 504 `PREDICTION_DEADLINE`。Transport authentication／body-limit 回應可能不同：
Management 的 missing Bearer 為標準 HTTP `detail`，413 `PAYLOAD_TOO_LARGE` 沒有
`request_id`；client 不應假設所有錯誤都有同一 JSON shape。

Management body 上限 64 KiB，Gateway／Serving 8 MiB；MLP 每次 1–128 筆兩維
features，EBM 每次 1–16 筆 `[7,4,9,31]` precomputed spectrogram，固定 signal／Pad
順序。Serving 實體計算容量為 2，timeout 後計算仍占 slot 直到結束。429／503／504
不保證立即重送成功；先查狀態，Training 以原 key 在有效期內對帳，避免重複意圖。

## Ownership and recovery

Management readiness checks App DB and Mongo Identity. Serving requires a trusted DeploymentSpec, runtime/code match, complete artifact verification and golden inference. A loaded model does not query MLflow per prediction; current authorization still applies.

Inspect durable Job/attempt state before acting. A stale heartbeat is not death proof. The executor saves a stable launch record before spawning. Local execution records host, boot ID, PID and start ticks; Pod Training also freezes Pod UID, container ID, execution/attempt and original launch identity, with a scoped guardian stop receipt. Missing identity remains UNKNOWN and never triggers a second fit. Recycled PIDs are never signaled.

`python -m bootstrap.ml_cli control --once` scans stale executions and desired deployments. Automatic recovery is bounded to five claims; `RECONCILIATION_BUDGET_EXHAUSTED` requires investigation. Unknown execution remains nonterminal. Do not edit SQL to fake success/cancellation. Operator intervention must first establish physical executor state; no unsafe force-success endpoint exists.

## Broker and tracking failures

Outbox retries retain command identity, back off with jitter, and become DEAD after 20 publication attempts. Alert on age, DEAD publication and uncertain attempts. Repair topology before reviewed replay. Returned/nacked/ambiguous publication is never marked successful.

Worker ACK requires durable terminal evidence and a Redis technical snapshot. Redis is not the Job authority. ACK timeout/channel closure may cause redelivery; claims and final evidence prevent another fit. Queue, compute, cancellation and total finalization deadlines are distinct.

Fit success followed by tracking/registration failure preserves the bundle and checkpoint. Recovery finalizes that result and reconciles the registration origin; it does not retrain. Quality rejection retains the report and prevents approval.

## Deployment failures

Keep the old healthy generation until the candidate passes verification and warm-up. Fence unknown activation by advancing the authoritative routing revision before failure cleanup; observing the old route alone is insufficient. Rollback creates a fresh generation referencing existing immutable approval/model evidence. Retiring runtimes drain; absent stop proof they stay RETIRING.

The local/VM adapter requires a controlled shared execution filesystem/network namespace. The implemented opt-in K8s adapters add native Serving and Pod-aware Training; their operational rules are in the [K8s runbooks](README.md#部署回歸與維運). Multi-node CSI, partition fencing and production operations still require site qualification.

The reference pool reserves two serving slots, including ACTIVE, pending and unconfirmed retirement processes. FAILED candidates with unreleased resources remain in the controller's cleanup scan. A restart can resume every pre-activation phase. Do not release a slot manually while its process state is unknown.

## Observability

Use `X-Request-ID` and W3C `traceparent` for correlation. The SQL Outbox persists only allowlisted trace headers and the validated request ID. Trace spans cover HTTP, publication, worker execution, prediction and rollout reconciliation; payloads, credentials, baggage and exception text are excluded. Configure an OTLP HTTP collector with `OTEL_EXPORTER_OTLP_ENDPOINT` and the observability extra. Collector delivery, trace sampling and alert thresholds need target-environment validation.

Management `/metrics` requires a Bearer actor with `ml:operate`; it returns aggregate
SQL gauges across tenants, with no tenant/job/source labels. Restrict the scraper's
account and access accordingly. These are current-state counts, not event counters:

| Metric | Meaning |
| --- | --- |
| `ml_training_jobs{state}` | Number of stored Training jobs per present state; zero states may be absent |
| `ml_outbox_pending` | All Outbox rows whose state is not PUBLISHED, including DEAD |
| `ml_ingestion_jobs{state}`, `ml_ingestion_attempts{state}` | Ingestion state counts, only when Management enables Ingestion |
| `ml_ingestion_reconciliation_exhausted` | NEEDS_RECONCILIATION jobs with exhausted automatic budget |
| `ml_ingestion_oldest_pending_age_seconds` | Age since creation of the oldest nonterminal Ingestion job; zero if none |

Management readiness checks SQL schema and Mongo; Gateway readiness checks SQL and
Mongo, not every model. Serving readiness confirms its loaded immutable identity,
but does not re-probe Mongo/MLflow on each readiness request. Ingestion CLI health
checks local configuration/storage/schema, not remote Warehouse credentials.
There is no exported Training duration, Outbox-age or prediction latency histogram
in the current ML SQL reader. Alert rules, expiry checks, dashboards and SLO
measurement remain Production Operations work; do not infer them from design tables.

## Backup and restore

Back up App DB, MLflow metadata DB, versioned artifact bytes, registered dataset snapshots, identity state and pinned source/images as a coordinated recovery set. Database backups do not recreate missing artifacts. Do not garbage-collect active, rollback or approval-referenced bundles.

Restore to an isolated environment with relay/consumer/controller stopped. Verify migrations, image/source compatibility, model/evaluation/approval digests and required artifacts. Reconcile actual executions/routes before replay. Exercise golden inference and rollback before traffic. Actual retained versions determine recoverable RPO; measured restore determines RTO.

Local unit tests do not qualify backup/restore or production SLOs. These remain the policy/environment gates listed in the design.

## 完整 Compose 回歸

使用受支援的 Python 3.12／Torch 2.6 CPU 映像與全新的隔離 project／資料目錄；
先建立私有測試 `.env.ml`，不可指向 production DB／Identity／Artifact Store。
產生器拒絕覆寫既有檔案；不要為重跑而刪除未確認用途的既有設定。

```sh
python scripts/create_ml_env.py
export ML_ACCEPTANCE_VAR_DIR="$PWD/var/acceptance-new-run"
export ML_E2E_SOURCE_COMMIT="$(git rev-parse HEAD)"
mkdir -p "$ML_ACCEPTANCE_VAR_DIR"
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance-new-run up --build -d management relay supervisor control gateway redis
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance-new-run run --build --rm --no-deps acceptance
```

`verify_ml_acceptance.py --require-stack` 缺少端點、runtime 不符、必要案例跳過或任一
檢查失敗都不能通過。Protected Databricks 與 Docker host-only 檢查保留獨立範圍。
報告保存在 `$ML_ACCEPTANCE_VAR_DIR/acceptance/`；先核對狀態、JUnit 與 blockers，
再清除自己建立的 disposable project：

```sh
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ml-acceptance-new-run down --volumes --remove-orphans
```

K8s 完整回歸使用 [Acceptance 手冊](deployment/kubernetes-acceptance.md)，兩套結果
互不替代。最新已執行結果集中於 [實作狀態](implementation-status.md)。
