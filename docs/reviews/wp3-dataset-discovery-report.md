# WP3 — Dataset discovery and lineage delivery

Date: 2026-11-27 (Asia/Taipei). PR #19 integration and pagination correction are ready for review.
`production_accepted=false`. This delivery covers WP3.

## Baseline and scope

- Current master baseline: `eb472cae0f08856e0728df7cec757e016d349818` (merge of PR #18).
- Feature branch: `feature/dataset-discovery-lineage`.
- Previous WP3 HEAD: `998a0895f9ad79db15d4ffc86b9e18498e054520`.
- Integration merge: `c58b9affa98e1b1211eb9fd93d423d941eab3f1b`, with the previous WP3 HEAD
  and current master as its two parents.
- Committed plan: `183e54acf143d419ee3b510355150ac57a48215e`.
- Original implementation commit: `7055a2598f117a7069f859b26056a068f2cf74ea`.
- Integrated source and test commit: `4cda8252e646d570a7c809899017ff65b8b41074`.
- No applicable AGENTS.md was found in the repository or checked parent paths.

The existing code already has governed source browsing, immutable RawSnapshot publication,
DatasetBuilder and exact known-version Dataset Catalog lookup. WP3 supplies the missing
bounded discovery and SQL-bound provenance; it does not add HTTP dataset creation.

The only merge conflict was the generated source manifest, which was rebuilt from the merged
tracked file set. The three-dot diff against `eb472ca` contains WP3 changes only. The WP2
trusted code revision checks and API URL rendering under ASGI `root_path` match master, and
the commits containing those corrections are ancestors of the integrated branch.

All four WP3 collection routes return a continuation only if there are more results and
the next offset is at most 1,000,000. A continuation at exactly 1,000,000 is valid. The API
returns `null` if advancing by the limit would exceed that bound. The regression exercises all
four HTTP routes with real visible records and simulated lookahead at the offset boundary.

## Delivered behavior and changed components

| Component/files | Change and impact |
| --- | --- |
| `src/domain/pagination.py`, `src/domain/ingestion/discovery.py`, `src/domain/datasets/lineage.py` (new) | Bounded typed windows, source/state filters and canonical digest-protected receipt validation; no infrastructure dependencies. |
| `src/application/datasets/{ports,service}.py`, `src/application/ingestion/{ports,service}.py` | Separate discovery ports; existing permissions and application service boundaries. |
| `src/api/collection_schemas.py`, `src/api/datasets/`, `src/api/ingestion/discovery_schemas.py` (new); ingestion routers and `src/api/ml_http.py` | Strict metadata DTOs, six authenticated read-only routes, stable operation IDs, documented errors and bounded pagination. Preserve old exact-version route, including dataset ID `versions`. |
| `src/infrastructure/persistence/ml_datasets.py`, `src/infrastructure/ingestion/persistence/ingestion_jobs.py` | SQL-side visibility/filter/order/bounds; safe metadata and verified lineage retrieval; no raw-file downloads. |
| `src/infrastructure/persistence/dataset_lineage_schema.py`, `migrations/versions/0008_dataset_lineage.py` (new), `migrations/env.py` | Additive receipt table using existing dataset primary key and App DB. Follows WP2 0007, no historical row rewrite. |
| `src/infrastructure/datasets/ebm_builder.py` | Verify bounded immutable filesystem receipt before atomic catalog registration. Existing artifacts and feature algorithms remain unchanged. |
| `bootstrap/{ml_api,ml_wiring,ingestion_wiring}.py` | Compose discovery dependencies in existing Management runtime; no new service or credential grants. |
| `tests/ml/dataset_builder/test_discovery.py`, `test_lineage_migration.py` (new) | SQL/API/security/concurrency/atomicity/legacy and upgrade/downgrade coverage. |
| `scripts/check_ingestion.py`, `scripts/check_ebm_typing.py` | Include new shared query/domain/API/catalog modules in existing typing/format validation scopes. |
| README, architecture/configuration/implementation status/ingress docs, ML runbook, inventory, ADR-024, plan, manifest | Operator workflow, authority/limits, migration order, compatibility and rollback guidance. |

Architecture and decisions are in [ADR-024](../adr/024-dataset-discovery-lineage.md).
The [runbook](../ml-runbook.md#dataset-discovery-and-lineage-wp3) defines the full API contract,
permissions, filters, sorting, examples, errors and upgrade/rollback procedure.

## Security and compatibility

New ingestion lists use existing owner/operate checks in SQL and never accept caller tenant IDs.
Dataset read permissions remain `ml:train` or `ml:dataset:read`; private/public shadow resolution
is preserved. Full receipts additionally require existing raw-input visibility. Restricted
responses withhold receipt/digest/source IDs. Typed whitelists omit filesystem paths, artifact
locations, payloads and credentials. GETs perform metadata SQL queries, not external SDK or
raw-file reads. New successful metadata responses are private/no-store.

New dataset and verified receipt inserts share one transaction and tenant lock. Equivalent
registration is idempotent; established same-content/different-origin evidence conflicts,
including concurrent races. Corrupt receipts fail closed and do not leave partially visible
catalog versions. Legacy versions without a SQL receipt remain incomplete even on retry;
existing dataset/source digests and training contracts are unchanged. SQL evidence corruption
is reported as incomplete, distinct from denied input visibility. Audit records retain actor,
tenant and digest through the existing mechanism. No new permissions are introduced.

## PR #19 integration verification

The tests ran from `4cda8252e646d570a7c809899017ff65b8b41074` in
`/workspace/ml-prediction-guide` with Python 3.12.14. Both focused runs exercised dataset
and lineage discovery/upgrade, ingestion persistence, training contracts, and WP2 prediction
catalog and runtime integration, including trusted-revision and reverse-proxy cases.
The PostgreSQL run used the disposable `wp3-discovery-pg-20261127` PostgreSQL 17 container
on 127.0.0.1:15493 with tmpfs storage and per-test schemas. It was stopped after the run.

The commands below use the existing virtual environment and the same focused test targets
for both databases. For PostgreSQL, `ML_TEST_DATABASE_URL` pointed to the disposable instance.

```sh
PY=/workspace/ml-system-prod/.venv/bin/python
set -- tests/ml/dataset_builder tests/ml/test_datasets.py \
  tests/ml/ingestion/test_persistence.py tests/ml/test_training_contracts.py \
  tests/ml/test_prediction_catalog.py tests/ml/test_prediction_catalog_e2e.py
env -u ML_TEST_DATABASE_URL "$PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp3-pr19-sqlite-retry-20261127 --junitxml=/tmp/wp3-pr19-sqlite-retry.xml
"$PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp3-pr19-postgres-20261127 --junitxml=/tmp/wp3-pr19-postgres.xml
env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL \
  -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL "$PY" -m pytest \
  tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp3-pr19-full-retry-20261127 \
  --junitxml=/tmp/wp3-pr19-full-retry.xml
```

| Test scope | Outcome | Local evidence |
| --- | --- | --- |
| Focused SQLite | 102 passed, 0 failed/skipped, exit 0 | `/tmp/wp3-pr19-sqlite-retry.log`, corresponding JUnit XML |
| Focused PostgreSQL | 102 passed, 0 failed/skipped, exit 0 | `/tmp/wp3-pr19-postgres.log`, corresponding JUnit XML |
| Full ML and architecture tests | 1,428 passed, 0 failed, 5 skipped, exit 0 | `/tmp/wp3-pr19-full-retry.log`, corresponding JUnit XML |

The initial SQLite and full-suite attempts failed while the workspace filesystem was full,
and Docker could not create a fresh container layer. The runs above followed removal of the
two prior disposable pytest data directories. Their logs and JUnit reports were retained.
The full-suite skips require the external fixtures listed in the initial verification below.

The architecture boundary check, Pyflakes, CI-scoped Mypy, and repository ingestion, EBM,
Serving, deployment, training, Kubernetes acceptance and init static checks all passed.
Scoped Ruff lint and format checks, documentation, generated manifest and Git whitespace
checks also passed. The quality check commands use the same repository scripts listed below.
Remote CI and dynamic deployment qualification are outside this local evidence.

## Initial WP3 verification before the PR #19 integration

Execution environment: managed Linux cloud workspace `/workspace/ml-prediction-guide`,
Python 3.12.14 at `/workspace/ml-system-prod/.venv/bin/python` (abbreviated **PY** below).
Default tests use disposable SQLite/temp files and synthetic data. PostgreSQL commands target
only `wp3-discovery-pg-20261127`, cached PostgreSQL 17 with tmpfs data, loopback port 15493 and
random per-test SQL schemas. No existing volumes or production/private configuration were used.

| Exact command (replace PY with the path above) | Outcome / evidence |
| --- | --- |
| `env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL PY -m pytest tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp3-local-full-20261127 --junitxml=/tmp/wp3-local-full.xml` | PASS: 1,419 passed, 0 failed, 5 skipped; exit 0. `/tmp/wp3-local-full.log`, XML. |
| `PY -m pytest tests/ml/dataset_builder tests/ml/test_datasets.py -q --junitxml=/tmp/wp3-dataset-sqlite.xml` | PASS: 44 passed, 0 failed/skipped; exit 0. `/tmp/wp3-dataset-sqlite.log`, XML. |
| `PY -m pytest tests/ml/dataset_builder tests/ml/test_datasets.py tests/ml/ingestion/test_persistence.py tests/ml/test_training_contracts.py -q --junitxml=/tmp/wp3-postgres-regression.xml` with isolated `ML_TEST_DATABASE_URL` | PASS: 72 passed, 0 failed/skipped; exit 0. `/tmp/wp3-postgres-regression.log`, XML. |
| `PY scripts/check_boundaries.py` | PASS, exit 0; `/tmp/wp3-boundaries.log`. |
| `PY -m pyflakes bootstrap src tests/ml` | PASS, exit 0; `/tmp/wp3-pyflakes.log`. |
| `PY -m mypy --follow-imports=skip src/domain/ml.py src/domain/training src/domain/deployments src/domain/model_registry src/domain/prediction src/domain/datasets src/application/training src/application/deployments src/application/model_registry src/application/prediction src/application/datasets src/application/ml_services.py` | PASS, 42 source files; exit 0. `/tmp/wp3-core-typing.log`. |
| `PY scripts/check_ingestion.py` | PASS, 94 Ruff/format and 58 scoped typed files; exit 0. `/tmp/wp3-ingestion-check.log`. |
| `PY scripts/check_ebm_typing.py` | PASS, 47 scoped typed files; exit 0. `/tmp/wp3-ebm-typing.log`. |
| `PY scripts/check_deployment.py`, `PY scripts/check_serving_runtime.py`, `PY scripts/check_k8s_acceptance.py`, `PY scripts/check_training_runtime.py`, `PY scripts/check_init.py` (executed separately) | PASS, each exit 0; respective `/tmp/wp3-check_*.log`. These are static checks, not dynamic deployment acceptance. |

Focused Ruff lint/format checks for the new DTO/domain/migration/tests also pass. Exact target
lists, final documentation/manifest checks and durable counts are recorded in the final
[qualification JSON](../evidence/wp3-dataset-discovery-20261127/qualification.json). Test scopes overlap and must not be added together.

Five full-suite skips require fixtures deliberately not configured here: real backend EBM
lifecycle (one), live Databricks/PostgreSQL (one), live HTTP backend lifecycle (one), and
RabbitMQ/Redis receiver modes (two). One existing Starlette TestClient deprecation warning
remains. A development migration assertion initially counted the seeded public dataset; it now
checks the preserved legacy record by ID. Final qualification results above contain no failures.

## Acceptance traceability and remaining limits

| WP3 requirement | Status and evidence |
| --- | --- |
| Bounded ingestion jobs/snapshots with filters and SQL tenant/owner scope | PASS: discovery SQL/API tests on SQLite/PostgreSQL; existing persistence regressions. |
| Dataset families/versions/detail without private resource IDs | PASS: authenticated HTTP walkthrough, deterministic pages, empty/invisible results, public shadow rules and legacy lookup. |
| Trace exact raw/source revisions through builder settings to dataset content | PASS: real File ingestion → existing EBM transformation/publication → SQL catalog → HTTP lineage. Controlled Identity/ASGI transport; not live external deployment. |
| Immutable/protected provenance, atomic publication and audit | PASS: canonical/digest/unknown-contract rejection, corrupt receipts, rollback, equal/conflicting concurrent builds and audit checks. |
| Tenant and input-access isolation; no raw download or private paths | PASS: permission/owner/operate, cross-tenant and restricted lineage response tests. |
| Legacy compatibility and migration | PASS: SQLite/PostgreSQL upgrade/downgrade/re-upgrade preserve records; legacy rows stay incomplete; full ML regression. |
| Production/full-stack qualification | NOT EXECUTED for WP3: external Compose/K8s, real enterprise Databricks, live Identity/MLflow/broker deployment; `production_accepted=false`. |

Dataset creation still requires the authorized operator CLI. WP4 recipe discovery, WP5 lifecycle
transparency, WP6 durable dataset build APIs and WP7 production usability remain separately
authorized future work. No automatic provenance backfill or full API-only self-service claim.
Migration 0008 follows WP2 migration 0007, already on master.

READY FOR REVIEW. Review approval is required before any merge or production rollout.
