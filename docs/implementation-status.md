# Implementation status

Updated: 2026-11-10. This page describes the implemented CPU ML and batch
ingestion baseline. Repository integration, target-environment qualification and
the remaining EBM work packages are separate decisions.

## Current implementation and evidence

The current PR implements the CPU MLP/EBM training and fixed-version Serving
paths, the provider-neutral Data Ingress framework, File Batch acquisition and
the Databricks SQL Warehouse batch adapter. No unfinished concrete runtime
stub or missing integration was found in the source-completion review.
See the [merge assessment](reviews/merge-readiness-2026-11-10.md) for scope and
the explicitly deferred work.

At audited source `7a175ad0a2da02fcc70962d297b032a63a58db0d`, both the
[push workflow](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42742534571)
and [PR workflow](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/42742540971)
passed every required job. Each independent Backend report recorded
**FUNCTIONAL_BACKEND_ACCEPTED: 963 passed, zero failures/errors/skips** on
Python 3.12.16 / Torch 2.6 CPU. Each SQLite/PostgreSQL contract matrix recorded
971 passed and two separate opt-in skips. The protected Databricks live job was
not invoked; it is not included in Backend acceptance.

The actual unified image and private non-root entrypoint were built and
verified. Real-service Backend CI covers PostgreSQL, MongoDB, RabbitMQ, Redis,
MLflow/S3, training, evaluation, manual model approval, deployment, prediction,
rollback and identity revocation. These are isolated test services, not the
user's production endpoints.

| Area | Implemented | Remaining qualification / separate work |
| --- | --- | --- |
| Architecture | pinned template, role factories, immutable DTOs and narrow Protocol ports | source boundaries and strict typing pass; new architectural extensions need their own review |
| Jobs | atomic SQL idempotency/admission/attempt/outbox, ownership fencing, cancellation and recovery | SQLite/PostgreSQL contracts pass; production load/capacity remains unqualified |
| Transport | confirmed RabbitMQ publication, manual Taskiq Receiver and Redis ACK evidence | actual Receiver integration passes; site broker permissions/operations still require qualification |
| Training/data | CPU MLP and EBM child processes, immutable tenant datasets, fixed splits, train-only transforms, metrics and safe epoch resume | representative PoC numerical parity/quality and production resource limits need approved data |
| Registry/governance | immutable bundles, MLflow/local adapters, exact-bundle evaluation, durable ERROR/recovery, manual Approval binding | test MLflow/S3 roundtrips pass; the supplied external server needs its own TLS/permission/artifact qualification |
| Serving/rollout | verified fixed-version runtimes, golden warm-up, bounded physical execution, provenance, fenced routes, generation rollback/drain | actual multi-role lifecycle passes; site ingress, SLOs and load/restore evidence remain open |
| Raw ingress | Source Registry, File/Databricks adapters, OAuth M2M, fixed Delta SQL, bounded JSONL, private staging, durable publication/reconciliation and cleanup | live Databricks plus PostgreSQL gate DBX-09 has executable tests but no approved external fixture/result |
| Deployment/operations | explicit migrations, six long-lived roles, one-shot operator, standalone external-service profile, private role secrets, CA/reader propagation, image preflight, metrics/tracing and runbooks | trusted Registry release digest/SBOM, approved environment, capacity, security, backup/restore and Go-Live sign-off |

Use the [Day-1 runbook](deployment/day1-deployment-runbook.md) for deployment and
the [current delivery evidence](reviews/day1-deployment-2026-11-10.md) for the
implemented profile and local TLS/RBAC lab. That lab uses real MLflow,
PostgreSQL and S3 with a test-only authorization gateway; it does not qualify
the user's external services. All production acceptance flags remain false.

## Explicitly unfinished follow-up scope

Raw Snapshots and ML DatasetVersions remain separate. An explicit operator-only EBM
Raw-to-Feature/DatasetBuilder is implemented, but automatic ingestion-to-build orchestration is
not. Databricks Remote Executor,
CalibrationSnapshot, calibrated Energy/RUL, Batch Analysis/Report Store and
as-of/rolling-origin qualification are also separate, unfinished work packages.
GPU, DDP, HPO and multi-host/Kubernetes deployment are outside the CPU MVP.

The implemented EBM path serves neural outputs from precomputed Spectrograms.
It does not implement the entire Notebook's calibrated decision pipeline.
See [EBM implementation/usage](ebm-implementation-status.md) and the
[full integration plan](ebm-integration-plan.md) for those boundaries.

## Historical evidence

The [2026-10-29 Backend report](backend-acceptance-2026-10-29.md) records the
earlier 217-pass/three-skip result. The
[2026-10-31 EBM evidence](evidence/ebm-2026-10-31/result.json) records 468 passes
and three skips on the legacy Python 3.10 / Torch 2.0.1 host with its selector
workaround. Those dated host limitations and counts are historical; current
CI uses the supported Python 3.12 / Torch 2.6 CPU image and ordinary event loop.

The [2026-11-08 ingestion delivery](reviews/ingestion-stabilization-databricks-2026-11-08.md)
and [2026-11-09 Actions repairs](reviews/actions-failure-fixes-2026-11-09.md)
retain earlier findings and execution records. Their then-current Draft and
environment status should not be read as the current source-completion verdict.
Production sign-off, representative model quality, numeric SLOs, retention and
RPO/RTO are still not inferred from engineering fixtures or green CI.
