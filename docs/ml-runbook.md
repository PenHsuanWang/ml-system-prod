# ML runtime operations

For first installation, external MLflow configuration, private role files, migration and release Go/No-Go, start with the [Day-1 deployment runbook](deployment/day1-deployment-runbook.md). This document covers Day-2 ownership, recovery and coordinated backup/restore. The reference `compose.ml.yaml` is a development stack; it is not the external production profile.

Run from the repository root. Apply `python -m bootstrap.ml_cli migrate` before upgraded roles start. Startup never creates tables.

In the production profile, execute approved maintenance through its secret-loading role entrypoint (`docker compose ... run --rm --no-deps ...`); a bare `exec` does not reload private role configuration. Preserve the Controller's host/process identity and shared execution/network assumptions when deciding whether a maintenance command may reconcile a live generation.

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
