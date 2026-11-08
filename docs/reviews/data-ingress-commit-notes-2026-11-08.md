# Data Ingress scoped commit notes

Date: 2026-11-08 (Asia/Taipei). These commits preserve the reviewed, in-progress
Batch Data Ingress implementation at the user's request. They do not represent
completion of the original acceptance criteria or production qualification.

## Commit boundaries

1. `feat(ingestion): define batch source contracts and approved registry`
   — Domain DTOs, states/errors, consumed Application ports, immutable registry
   and contract/registry tests.
2. `feat(ingestion): add bounded file acquisition and immutable raw snapshots`
   — File adapter, JSONL validation, private staging, raw publication and shared
   adapter/File/publication tests.
3. `feat(ingestion): persist and orchestrate durable ingestion jobs`
   — SQL source/job/catalog persistence, migration, Service/Supervisor, Bootstrap
   composition, identity aliases and persistence/recovery/OCP tests.
4. `feat(ingestion): expose async management API and operator CLI`
   — HTTP schemas/routes, opt-in Management wiring, worker/operator commands,
   typing configuration and API/separate-worker tests.
5. `docs(ingestion): document file pipeline and outstanding acceptance gaps`
   — Ingress design/operations, module/developer addenda, historical audit,
   evidence and these commit notes.

The Notebook and broader OCP/Workload/ADR proposal documents remain outside this
commit scope. Four links to those local drafts are rendered as explicit paths
with an uncommitted-draft note. Their contents and Proposed status are preserved.
Production source was not repaired as part of this commit preparation.

## Validation and open findings

- The pre-commit Ingress suite returned **166 passed, 1 failed** in 10.60 seconds.
  The failure was
  `test_snapshot_publication.py::test_concurrent_identical_publication_is_one_immutable_snapshot`.
  Earlier 167-pass results remain dated evidence and do not supersede this failure.
- Dependency boundaries and scoped Ruff passed. Strict Mypy checked 28 modified
  production files successfully on the existing legacy host/cache. This is not
  the supported-runtime or external-service acceptance gate.
- A deterministic probe confirms a further P2 race: a lookup first sees a missing
  tenant directory, another identical publisher completes, then the lookup's
  exception handler mistakes the now-existing parent for an incomplete snapshot
  and raises `SnapshotInvalid` at `raw_snapshot_writer.py:442`. The competing
  snapshot verifies and an identical retry succeeds; the probe did not observe
  partial publication or overwritten content. See the [probe](../evidence/data-ingress-commit-2026-11-08/publication-race-repro.py.txt),
  [output](../evidence/data-ingress-commit-2026-11-08/publication-race-repro.log)
  and [result](../evidence/data-ingress-commit-2026-11-08/result.json).
- The four previous findings remain open: production readiness route registration,
  publication lease renewal, phase-aware conflict handling and cleanup traversal
  fairness. Their reproductions and acceptance limits remain in the
  [2026-11-06 audit](data-ingress-completion-audit-2026-11-06.md).
- Databricks, Kafka and Raw-to-Dataset Builders remain future work. Only the File
  source is registered by the shipped composition root.

No hook bypass or test suppression is part of these commits. Target Python,
PostgreSQL/RabbitMQ integration, production capacity and reliability remain
unqualified. Commit descriptions deliberately record the implementation scope
and known follow-ups instead of claiming the original task is complete.
