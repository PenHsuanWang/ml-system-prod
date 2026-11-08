# Universal Data Ingress — implementation report

## Inspection baseline

Inspected `master` at `edc140061f7cac7d0b6881f35dded4a2a92a8d6c`, origin
`git@github.com:PenHsuanWang/ml-system-prod.git`. The tracked worktree was clean.
The original Notebook and nine extension/proposal documents were untracked and
are preserved. ADR-015 remains Proposed.

The existing `DatasetCatalog`, MLP/EBM snapshot readers, tenant dataset import,
explicit module wiring, SQL transactions/audit and HTTP identity/error conventions
are reusable. `ModuleExports.datasets` is Data Egress (`ExportSource`), not Ingress.
The Training outbox has Training-specific foreign keys and completion evidence;
it is not a generic queue. No universal source adapter registry, raw snapshot
catalog or ingestion job existed at this baseline.

## Implementation boundary

The first slice uses a separate SQL polling queue and a bounded ingestion worker.
API submission is durable and returns 202; HTTP never acquires source data. There
is no new broker receiver or broker ACK claim. Before publication, the worker
persists an exact publication intent under an owner token. Recovery uses that
intent and stable snapshot identity, without fetching the source again.

Raw Snapshots remain distinct from approved ML Dataset Versions. No changes to
TrainingService, TrainingSupervisor, model trainers or model numerics are needed.
Databricks, Kafka and Dataset Builders remain separately documented extensions.

## Initial environment evidence

- Dependency boundary checker: PASS before changes.
- Host Python: 3.10.12; supported project runtime is Python >=3.11.
- Ruff was initially unavailable; baseline lint/format is NOT RUN at inspection.
- PostgreSQL, RabbitMQ and remote source qualification require their actual test
  environments; unit tests will not be presented as external integration proof.

Implementation details, final file inventory and test results are added below
after the working vertical slice is verified.

## Completion audit — 2026-11-06

The previous implementation is **not complete**. The File-to-Raw-Snapshot slice
and 167 ingestion tests pass locally, but the audit reproduced a Management API
route registration regression, publication lease exhaustion, incorrect pre-intent
conflict handling and cleanup starvation. Required final delivery sections and
some operational documentation are still incomplete. The full local ML/boundary
run passed 635 tests with 3 real-service skips; supported-runtime and external
integration qualification remain NOT VERIFIED.

See the [completion audit](data-ingress-completion-audit-2026-11-06.md) for the
Definition of Done matrix, exact findings, reproduction evidence, checks and
remaining work. This audit did not modify production code or mark the original
task complete.

## Scoped commit preparation — 2026-11-08

The user requested commits of the existing Data Ingress and File Adapter work.
The [commit notes](data-ingress-commit-notes-2026-11-08.md) describe the five
dependency-ordered groups and preserved local drafts. A fresh pre-commit run
returned 166 passed and 1 failed: concurrent identical publication can race while
the tenant publication directory is first created. A deterministic reproduction
is retained with the notes. The prior four findings remain unresolved; these are
in-progress implementation commits, not acceptance sign-off or Databricks support.
