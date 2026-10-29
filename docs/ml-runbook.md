# ML runtime operations

Run from the repository root. Apply `python -m bootstrap.ml_cli migrate` before upgraded roles start. Startup never creates tables.

## Ownership and recovery

Management readiness checks App DB and Mongo Identity. Serving requires a trusted DeploymentSpec, runtime/code match, complete artifact verification and golden inference. A loaded model does not query MLflow per prediction; current authorization still applies.

Inspect durable Job/attempt state before acting. A stale heartbeat is not death proof. The executor saves a stable launch record before spawning; the child records host, boot ID, PID and start ticks. Missing identity remains UNKNOWN and never triggers a second fit. Recycled PIDs are never signaled.

`python -m bootstrap.ml_cli control --once` scans stale executions and desired deployments. Automatic recovery is bounded to five claims; `RECONCILIATION_BUDGET_EXHAUSTED` requires investigation. Unknown execution remains nonterminal. Do not edit SQL to fake success/cancellation. Operator intervention must first establish physical executor state; no unsafe force-success endpoint exists.

## Broker and tracking failures

Outbox retries retain command identity, back off with jitter, and become DEAD after 20 publication attempts. Alert on age, DEAD publication and uncertain attempts. Repair topology before reviewed replay. Returned/nacked/ambiguous publication is never marked successful.

Worker ACK requires durable terminal evidence and a Redis technical snapshot. Redis is not the Job authority. ACK timeout/channel closure may cause redelivery; claims and final evidence prevent another fit. Queue, compute, cancellation and total finalization deadlines are distinct.

Fit success followed by tracking/registration failure preserves the bundle and checkpoint. Recovery finalizes that result and reconciles the registration origin; it does not retrain. Quality rejection retains the report and prevents approval.

## Deployment failures

Keep the old healthy generation until the candidate passes verification and warm-up. Fence unknown activation by advancing the authoritative routing revision before failure cleanup; observing the old route alone is insufficient. Rollback creates a fresh generation referencing existing immutable approval/model evidence. Retiring runtimes drain; absent stop proof they stay RETIRING.

The reference local/VM adapter requires a controlled shared execution filesystem/network namespace. A multi-host/Kubernetes adapter needs equivalent identity, resource isolation, observed routing and fencing; replacing the launcher alone is insufficient.

The reference pool reserves two serving slots, including ACTIVE, pending and unconfirmed retirement processes. FAILED candidates with unreleased resources remain in the controller's cleanup scan. A restart can resume every pre-activation phase. Do not release a slot manually while its process state is unknown.

## Observability

Use `X-Request-ID` and W3C `traceparent` for correlation. The SQL Outbox persists only allowlisted trace headers and the validated request ID. Trace spans cover HTTP, publication, worker execution, prediction and rollout reconciliation; payloads, credentials, baggage and exception text are excluded. Configure an OTLP HTTP collector with `OTEL_EXPORTER_OTLP_ENDPOINT` and the observability extra. Collector delivery, trace sampling and alert thresholds need target-environment validation.

## Backup and restore

Back up App DB, MLflow metadata DB, versioned artifact bytes, registered dataset snapshots, identity state and pinned source/images as a coordinated recovery set. Database backups do not recreate missing artifacts. Do not garbage-collect active, rollback or approval-referenced bundles.

Restore to an isolated environment with relay/consumer/controller stopped. Verify migrations, image/source compatibility, model/evaluation/approval digests and required artifacts. Reconcile actual executions/routes before replay. Exercise golden inference and rollback before traffic. Actual retained versions determine recoverable RPO; measured restore determines RTO.

Local unit tests do not qualify backup/restore or production SLOs. These remain the policy/environment gates listed in the design.
