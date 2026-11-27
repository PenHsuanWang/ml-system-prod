# ML runtime operations

For first installation, external MLflow configuration, private role files, migration and release Go/No-Go, start with the [Day-1 deployment runbook](deployment/day1-deployment-runbook.md). This document covers Day-2 ownership, recovery and coordinated backup/restore. The reference `compose.ml.yaml` is a development stack; it is not the external production profile.

Run from the repository root. Apply `python -m bootstrap.ml_cli migrate` before upgraded roles start. Startup never creates tables.

In the production profile, execute approved maintenance through its secret-loading role entrypoint (`docker compose ... run --rm --no-deps ...`); a bare `exec` does not reload private role configuration. Preserve the Controller's host/process identity and shared execution/network assumptions when deciding whether a maintenance command may reconcile a live generation.

Actual defaults, environment keys and profile overrides are in the
[configuration reference](configuration-reference.md). Lifecycle JSON examples start in
the [README](../README.md); EBM schemas/policy and Ingestion have their own linked guides.

## API 與 client 契約

參考 Compose 的預測入口為 `http://127.0.0.1:8001/docs`。在 Management 登入後，
於 Swagger 選 **Authorize** 並貼上 access token。使用 `GET /api/v1/prediction/endpoints` 列出可用端點，
開啟明細取得輸入契約與 `request_example`，在預測操作中選用 MLP 或 EBM 格式。成功回應的 `provenance` 提供這次實際使用的名稱、版本與部署世代。
端點 API 與契約發布步驟見[探索 API 文件](plans/prediction-endpoint-discovery.md)。

Management／Gateway／Serving 使用目前 Identity session 的 Bearer token。帳號不會在
啟動時自動建立；`manage.py create-admin` 的 dot permissions 對映為以下 colon scopes。
權限不授予跨 tenant 存取；API 不接受 caller 自選 actor／tenant／import path。

| 操作 | 實際授權／結果 |
| --- | --- |
| Training submit | `ml:train`；202；必填 `Idempotency-Key`，1–128 visible ASCII characters |
| Job／attempt 查詢 | 同 tenant 的 owner，或 `ml:jobs:read_all`；不符合時 404 |
| Training cancel | 上述可見性加 `ml:cancel`；202；已終態為 409 |
| Dataset discovery / metadata | `ml:train` 或 `ml:dataset:read`；保留 `dataset_id@version` lookup |
| Dataset full lineage | 上述權限，加所有 Raw Snapshot 的 ingestion read 與 owner／operate 可見性 |
| Model／versions／evaluation 查詢 | `ml:model:read` |
| 以 binding ID 人工 Approval | `ml:model:approve`；201；必須有同 binding／manifest 的 `APPROVED_BY_POLICY` evaluation |
| 以 name/version 人工 Approval | 上述 approve 加 `ml:model:read`（會先列出版本） |
| Deployment create／rollback | `ml:deploy`；回傳 202，須輪詢 observed state |
| Deployment read | `ml:deployment:read` 或既有 `ml:deploy`，限目前 tenant |
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

## Dataset discovery and lineage (WP3)

These authenticated metadata APIs belong to **Management (8000)**. Gateway (8001) remains the
prediction entry point. Use Management `/docs` or `/openapi.json` for typed parameters and responses.
Existing source browsing, approved-source registration and ingestion execution are unchanged.

| GET path under `/api/v1` | Purpose and permissions |
| --- | --- |
| `/ingestion/jobs` | Visible jobs; `ml:ingest`, `ml:ingestion:read` or `ml:ingestion:operate`; owner scope unless operate, always same tenant |
| `/ingestion/snapshots` | Only snapshots of durably PUBLISHED, visible jobs; same permissions |
| `/datasets` | Dataset families; `ml:train` or `ml:dataset:read` |
| `/datasets/{id}/versions` | Immutable versions and safe schema/feature/split metadata; same dataset permission |
| `/datasets/{id}/versions/{version}` | Exact version metadata; same permission |
| `/datasets/{id}/versions/{version}/lineage` | Receipt, builder configuration and source revisions/digests when every input is visible |

All collections use `limit` (default 25, maximum 100), `offset` (0–1,000,000), optional
`created_after` and `created_before` (finite, nonnegative UTC Unix seconds, strict bounds;
after must precede before). Unknown parameters are rejected. Pages contain `items`, `limit`,
`offset`, and `next_offset`. Continuation is null when the results are exhausted or the next
offset would exceed 1,000,000. Jobs/snapshots accept exact `source_id` and
`source_version`; jobs also accept `state`. Families accept exact `dataset_id`. Job/snapshot
ordering is creation time descending then ID descending; family ID and version ordering are
lexically ascending. Family counts reflect matching time bounds. Offset pages can shift during
concurrent writes; enumerate again for a fresh view. Successful new metadata responses use
`Cache-Control: private, no-store`; clients should also discard error responses on sign-out.

Dataset visibility preserves tenant versions plus existing public fixtures. A private version
shadows the same public ID/version. Missing/invisible collections are empty; missing/invisible
exact versions return 404. The existing `/datasets/versions/{dataset_id}@{version}` response
is unchanged. There is no inferred latest version and no raw-data download endpoint.

### Operator and reader walkthrough

The following uses synthetic names; choose sources approved by the deployment operator.
Set `MANAGEMENT_URL` to the trusted Management origin and `ACCESS_TOKEN` to an authenticated
session token. Discover resources rather than requesting filesystem paths or private IDs:

```sh
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/ingestion/available-sources"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/ingestion/jobs?source_id=builder-fdc&state=PUBLISHED&limit=25"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/ingestion/snapshots?source_id=builder-fdc&source_version=v1"
```

To acquire new data, use existing `POST /ingestion/jobs` with `source_id`, `source_version`
and `Idempotency-Key`, then follow the returned status/result URLs. Discovery neither ingests
data nor proves that a table is currently reachable. Select FDC/wafer snapshots and retain
their exact manifest digests. Dataset creation remains the explicit, authorized
[DatasetBuilder CLI](ebm-implementation-status.md#rawsnapshot-datasetbuilder);
HTTP build orchestration is deferred to WP6. New CLI builds atomically register the dataset
and verified receipt in SQL. A filesystem receipt alone does not establish catalog lineage.

After a synthetic `built`, `v1` dataset has been published, a reader can discover and inspect it:

```sh
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/datasets?dataset_id=built&limit=25"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/datasets/built/versions"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/datasets/built/versions/v1"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/datasets/built/versions/v1/lineage"
```

A family page has the following shape (publication time is illustrative):

```json
{"items":[{"dataset_id":"built","version_count":1,"last_published_at":1795737600.0}],"limit":25,"offset":0,"next_offset":null}
```

Version metadata exposes the content digest, schema versions, feature shape/order/config and
split counts when known. Unknown historical fields are null. `lineage_status=recorded` means
a receipt is stored; the lineage detail separately verifies its integrity and input access:

- `complete`: exact dataset identity, canonical receipt digest and current SQL-published input
  metadata agree. Includes builder/version, configuration, snapshot IDs, source versions/revisions,
  configuration/content/manifest digests, counts and byte sizes. The receipt includes the reader's
  own tenant identity so its canonical SHA-256 remains independently reproducible.
- `restricted`: dataset metadata is readable, but ingestion read permission or visibility of any
  input is missing. Receipt and its digest are withheld; no source IDs are disclosed.
- `incomplete`: no SQL receipt (`legacy_metadata_missing`) or failed verification
  (`receipt_integrity_failed`). No fabricated history or live qualification is implied.

Dataset content identity and provenance identity are distinct. Once recorded, a source/config
change needs a new dataset version even if feature bytes are identical. An equivalent build is
idempotent; different established provenance conflicts. Rebuilding an existing legacy version
without a SQL receipt leaves it incomplete; no automatic backfill assigns a new origin.
Lineage reads check persisted metadata, not current raw-file bytes or live external connectivity.

### Errors, upgrade and rollback

New GETs have no write/idempotency side effects. Authentication failures are 401; permission
failures 403; invalid/unknown fields or bounds 422; unavailable SQL/composition 503 (retryable
with backoff). Stored raw snapshot identity failures return the existing 400 ingestion error.
Exact dataset lookups can return 404. Existing error envelopes and request IDs are preserved;
401 may use the existing `detail` body. Correct 4xx requests/permissions before retrying.

Apply additive `0008_dataset_lineage` with `python -m bootstrap.ml_cli migrate` through the
approved maintenance role before upgraded roles start. It follows WP2 `0007_prediction_contracts`.
No existing source/dataset digests or historical rows are rewritten. Keep the table when rolling
back application code. An explicit downgrade to 0007 removes only new receipt evidence, retains
dataset versions/content and makes lineage incomplete; back up evidence first. Migration tests
exercise this only on disposable SQLite/PostgreSQL databases. Production migrations require
deployment authorization. `production_accepted=false`; target Compose/K8s and enterprise
Databricks qualification are separate gates.

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

## Runtime source management

Databricks initialization supports connection-only setup. Authorized operators
register immutable, policy-governed source versions through the Management API.
Workers resolve exact SQL catalog definitions at execution. Legacy templates
and worker-only credentials retain their existing contracts.
See [runtime source management](runtime-source-management.md) for permissions,
lifecycle behavior and separate external qualification requirements.

## Training recipe and job discovery (WP4)

Use the Management API on port 8000 and sign in through the existing Identity workflow. Open
Management `/docs`, choose **Authorize** and use the current Bearer access token. All paths below
are relative to `/api/v1`. The recipe catalog reflects the approved workloads in the running
release. A recipe version describes a particular dataset and request contract rather than the
model version created by a later training job.

| GET | Access and behavior |
| --- | --- |
| `/training/recipes` | `ml:train`. Built-in recipe IDs and versions in ascending order, with the resource profile, input schema, architecture and expected artifact profile. |
| `/training/recipes/{recipe_id}/versions/{version}` | `ml:train`. Dataset requirements and per-sample feature shape, evaluation policy version, output schema version, and the same model/training constraints as admission. `submit_example` is a validated synthetic POST body with defaults. |
| `/training/resource-profiles` | `ml:train`. Built-in CPU profile IDs and the approved recipe versions that use them, ordered by profile ID. Actual CPU/memory and time budgets depend on the deployed Training role. |
| `/training/jobs` | Authenticated job owner or same-tenant `ml:jobs:read_all`. Returns job state/phase, current attempt ID, revision, timestamps, spec digest and resulting model binding when present. |
| `/training/jobs/{job_id}/spec` | Same visibility as job status. Returns the dataset ID/version and content digest, recipe, resolved model/training settings, evaluation policy and immutable execution spec digest. |
| `/training/jobs/{job_id}/metrics` | Same visibility as job status. Returns persisted fit metric snapshots for the observed current attempt, ordered by zero-based epoch index. Before a result is checkpointed the status is `unknown` with an empty `samples` array. |

1. List recipes and read the exact recipe version. Compare its dataset schema and feature shape
   with an accessible version from [Dataset discovery](#dataset-discovery-and-lineage-wp3).
   MLP needs the approved two-feature dataset contract. EBM needs the EBM replay dataset
   schema and `[7, 4, 9, 31]` per-sample shape. The EBM example uses a synthetic dataset that
   must be registered separately. Training admission checks the selected dataset again.
2. Copy the recipe detail's `submit_example` and replace the dataset reference as needed.
   Submit to the existing `POST /training/jobs` with an `Idempotency-Key`. A 202 response means
   the job was accepted. See the existing [client contract](#api-與-client-契約) for replay,
   cancellation and terminal-state behavior.
3. List jobs or poll the existing status URL. Follow the job's spec and metrics endpoints to
   read its resolved inputs and recorded training metrics. A completed job with a
   `model_binding_id` can be inspected through the existing registry with `ml:model:read`.
   EBM evaluation can reject a successfully trained model when an operator policy is absent.
   Deployment still requires the existing environment approval.

Job lists use the shared limit (default 25, maximum 100) and offset (0 through 1,000,000).
They sort by creation time and then job ID, both descending. Use the returned `next_offset`
until it is null. Concurrent job changes can shift offset pages. Filter by exact `state` or
exclusive `created_after` / `created_before` bounds in UTC Unix seconds. The recipe and
resource-profile collections are bounded by the built-in registry. Discovery GETs do not
have idempotency keys or mutate job state. The new job reads use private, no-store caching.

The spec digest identifies the complete stored execution spec. The response is a safe view of
that spec and omits internal paths, runtime settings and operator policy configuration.
Metrics come from the current attempt's persisted output instead of a live progress file or
MLflow. The endpoint serves up to 1,000 epoch snapshots with only known finite fit metrics.
MLP `validation_accuracy` is a fraction. Loss fields are the recorded training objective
values, and units for those values are unspecified. Final evaluation metrics remain in the
existing model registry. A metrics response can contain recorded fit output even when a later
evaluation or registration failed. Check the job state separately.

Authentication failure is 401. Recipe/profile reads return 403 without `ml:train`. Unknown
recipes or invisible job details return 404. Invalid query fields, state or bounds return 422.
Unavailable or invalid stored evidence returns 503 with the existing retryable error envelope.
Use the response's `X-Request-ID` when reporting a problem. No schema change is needed for WP4.

## Model registry and deployment discovery (WP5)

On Management port 8000, sign in through Identity and use the Bearer token in `/docs`.
Paths below are relative to `/api/v1`. Grant `ml.model.read` in Identity to inspect models and
`ml.deployment.read` to inspect deployments. These map to `ml:model:read` and
`ml:deployment:read`. Existing `ml.deploy` grants retain read access, with no grant migration.
Assign the read-only grants independently of `ml.model.approve` and `ml.deploy`.

| GET path | Information and scope |
| --- | --- |
| `/model_registry/bindings` | `ml:model:read`. Paged SQL bindings. Filter by exact `registered_name` and/or `model_version`. Order is registered name, version string, then binding ID. There is no implicit latest version. |
| `/model_registry/models/{name}` | `ml:model:read`. Name and count of registered versions visible in the current tenant. Use the bindings list with `registered_name` to browse versions. |
| `/model_registry/bindings/{id}/contract` | `ml:model:read`. Digest-verified public inference contract and latest verified usage review, tied to the binding manifest. Missing or invalid evidence is `incomplete`. |
| `/model_registry/bindings/{id}/lineage` | `ml:model:read`. Binding, run ID and manifest. The recorded job attempt, recipe and spec digest also require job ownership or same-tenant `ml:jobs:read_all`. The pinned dataset additionally requires `ml:train` or `ml:dataset:read`. Missing or inaccessible fields remain null with `status=incomplete`. |
| `/model_registry/bindings/{id}/evaluation-results` | `ml:model:read`. Immutable evaluations for the exact binding/manifest and known public metrics. Newest recorded time first, then ID descending. Undefined metrics can be null. |
| `/model_registry/bindings/{id}/approvals` | `ml:model:read`. Recorded approvals matching the binding and evaluation evidence, including the approved environment. Newest recorded time first, then ID descending. |
| `/deployments` | `ml:deployment:read` or `ml:deploy`. Model binding/version, state, generation, approval, environment and route revision. Optional exact `endpoint_id`, `state`, and `environment`. Order is endpoint ID ascending, then generation descending. |
| `/deployments/endpoints` and `/deployments/endpoints/{id}` | Same read grants. Current tenant SQL route assignments in endpoint ID order. An endpoint can be `active`, `unassigned`, or `inconsistent`. Readiness is always `unknown` in these observations. |
| `/deployments/endpoints/{id}/history` | Same read grants. Recorded generations newest first and their current observed state. `expected_route_revision` is the generation's stored precondition. `route_revision` is the current SQL observation. |
| `/deployments/{id}/events` | Same read grants. Only persisted deployment audit records, in ascending event time and ID order. Currently these are `deployment.create` records. |

For example, a governance reader can browse a synthetic deployment named `quality`, then
use its returned `model_binding_id` to inspect that model's exact evaluation and approval.
Replace the example names with values from the visible lists:

```sh
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/deployments/endpoints/quality"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/deployments/endpoints/quality/history?limit=25"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/model_registry/bindings/$BINDING_ID/evaluation-results"
curl -sS -H "Authorization: Bearer $ACCESS_TOKEN" \
  "$MANAGEMENT_URL/api/v1/model_registry/bindings/$BINDING_ID/approvals"
```

Binding identity, evaluation decisions, approvals, deployment generations and active
assignments are separate records. The Controller still owns SQL route changes. A rollback
creates a new generation and initially leaves the current assignment in place. The audit
records do not distinguish rollback creation from ordinary creation, or preserve a timeline
of controller transitions. Use Prediction Gateway endpoint discovery for inference
contracts as a prediction-only user.

Collections return `items`, `limit`, `offset`, and `next_offset` (null at the end or at the
offset bound). The default limit is 25, maximum 100, and offsets range from 0 to 1,000,000.
Concurrent changes can shift pages. Identity derives the tenant, and unexpected query fields
are rejected. New reads use private, no-store caching. Authentication failure is 401,
missing permission is 403, inaccessible or missing detail is 404, invalid input is 422,
and unavailable persisted evidence is 503 with the existing error envelope and request ID.

The existing `GET /model_registry/models` binding array, versions, evaluations and approval
contracts remain in place. Deployment detail retains its existing response and accepts the
new read grant. Creation and rollback still require `ml:deploy`. WP5 adds no SQL migration.
