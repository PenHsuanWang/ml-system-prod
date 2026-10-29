# Implementation status

Date: 2026-10-29. Source implementation and target-environment qualification are distinct.

**Latest acceptance verdict: local checks passed; full Backend / production acceptance incomplete.** See the [Backend acceptance report](backend-acceptance-2026-10-29.md) for reproduced defects, fixes, executed evidence and remaining integration gates. The repeat run includes 217 passing tests and 3 skipped real-service cases.

| Area | Implemented | Evidence / outstanding qualification |
| --- | --- | --- |
| Architecture | pinned template, opt-in factories, immutable DTOs and narrow ports | boundary checker and core strict typing |
| Jobs | atomic SQL idempotency/admission/attempt/outbox, owner fencing, cancellation and result guards | SQLite concurrency/races; PostgreSQL opt-in/CI |
| Transport | confirmed mandatory RabbitMQ publisher, manual Taskiq Receiver, Redis ACK evidence | real-service test supplied; not executed locally |
| Training/data | actual CPU MLP child process, immutable tenant snapshot import, fixed splits/train-only transforms, epoch metrics, checkpoint/resume | real subprocess/custom-label training/evaluation/cancel smoke tests |
| Registry/governance | complete immutable bundles, local and MLflow adapters, durable evaluation ERROR/recovery, exact evaluation/report/approval binding | local path exercised; remote MLflow/S3 still requires live evidence |
| Serving | fixed handle, verified golden warm-up, strict input/output, bounded physical inference and provenance | real ASGI prediction and resource tests |
| Rollout | local/VM serving processes, SQL-authoritative ingress, routing revision/fencing, new-generation rollback/drain | contract tests; full multi-role Compose rollout pending |
| Operations | schema-aware readiness, low-cardinality gauges, W3C propagation and bounded optional OTLP export, finite recovery/publication budgets, five migrations, runbook, CI and container definitions | context/privacy tests; collector/image build, restore, capacity/SLO/security qualification pending |

Executed local evidence is recorded in [CPU smoke evidence](evidence/ml-cpu-2026-10-29/README.md), including JUnit results and the exact host workaround. The CLI demo reached durable `SUCCEEDED` with a registered binding and held-out evaluation. Source compilation, management imports without Torch, dependency boundaries, core typing and the official Compose JSON Schema were also checked. Compose schema validity does not prove a container can build or start.

The local environment has Python 3.10 instead of the supported 3.11/3.12 target, cannot resolve package indexes, and cannot access Docker. Cached Torch 2.0.1 and dependencies were recovered under `/tmp` without changing the global environment to obtain real CPU smoke evidence. This does not qualify the target Torch 2.6.0 CPU/Python 3.12 image.

This host delays asyncio cross-thread wakeups while its selector sleeps. ASGI tests used an external test-only polling-selector plugin; application event loops were not patched. Normal commands and target CI use the ordinary event loop.

Inherited template locks and evidence preserve upstream provenance; the entire inherited template suite was not rerun on this unsupported host. New complete hash locks, built image digests/SBOM, executed real-service CI and a qualified production profile have not been produced offline. Production sign-off, numeric SLOs, retention and RPO/RTO are not inferred from fixture training.
