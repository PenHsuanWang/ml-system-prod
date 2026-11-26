# WP2 prediction endpoint discovery review

## Baseline and review scope

Master baseline a5b1767fb2994ae6db5127b4311179a93d9af6f4.
WP1 dependency 50914652912b262b6652e68b53591cbdc7fb526e.
WP2 implementation eb30663aa97fdba37914208550c71ec8e17edb5c,
on feature/prediction-endpoint-discovery, stacked on WP1.

The user authorized sequential local commits and completion of WP2, followed by review.
WP3–WP7 remain outside this delivery. The initial implementation branch was clean.
The source commit is frozen; this report/evidence follow in a documentation commit.

## Result and architecture

A user with only ml:predict can open Gateway docs, authenticate, discover their tenant's active
endpoints, read a verified contract and executable request_example, and perform MLP/EBM inference.
Model version, generation, manifest and request ID identify the actual serving response.

The existing Gateway adds GET /api/v1/prediction/endpoints and
GET /api/v1/prediction/endpoints/{endpoint_id}.
EndpointDiscoveryService uses a single bounded SQL query joining authoritative routes,
active unreleased deployments, tenant ModelBindings, public contracts and latest usage review.
The Controller owns route changes. Discovery reports readiness=unknown and the existing POST
continues to verify physical readiness, identity and current route.

SQLRegistryRepository receives the contract-publication method. Additive migration 0007 creates
model_public_contracts and model_usage_reviews. Technical content is canonical/digest-protected
and immutable per binding. Human descriptions use separately audited append-only revisions.
A bound HMAC keyset cursor expires in 15 minutes and reauthorizes every page.
Success and handled errors return private/no-store headers.

The initial publication/backfill mechanism deliberately uses an authenticated operator CLI.
It requires current Mongo Identity ml:model:read and ml:model:approve, verifies bundle/member
hashes and the approved runtime/golden contract, then writes safe technical data plus reviewed
usage atomically. There is no automatic Supervisor extraction or mandatory metadata gate on
deployment in this release. Legacy active endpoints remain explicitly incomplete until compatible
publication; continued inference requires their original qualified runtime to remain available.
Business text must be reviewed for the prediction audience.

The nested technical document is immutable and digest-verifiable. The separate top-level
request_example substitutes the active endpoint ID for direct use.
Unknown physical units remain null. Registry backend and uncalibrated output semantics are
explicit. Neither discovery operation accesses live MLflow/artifacts/Serving or returns internal
artifact locations. Technical schema-generation changes need compatibility review to preserve
historical contract identity.

## Source inventory

| Area | Files and impact |
| --- | --- |
| Domain/application | src/domain/prediction/catalog.py, src/application/prediction/catalog.py; immutable contract, reviewed usage validation, publication and read ports, scoped cursor/admission. |
| Existing registry authority | src/infrastructure/persistence/ml_registry.py; serialized tenant publication, idempotency/conflict, business revisions and audit. |
| Persistence/read model | prediction_contract_schema.py and prediction_catalog.py in src/infrastructure/persistence; shared metadata plus SQL bounded read adapter. |
| Migration | migrations/versions/0007_prediction_contracts.py and migrations/env.py; additive tables and explicit metadata registration. |
| HTTP/bootstrap | src/api/prediction_catalog.py, bootstrap/ml_gateway.py; strict DTOs, auth, no-store and dependency assembly. |
| Verified authoring | bootstrap/prediction_contracts.py and prediction_contract_cli.py; actual DTO schemas, verified runtime, bounded private token and reviewed JSON files. |
| Tests/quality | tests/ml/test_prediction_catalog.py, test_prediction_catalog_e2e.py, scripts/check_serving_runtime.py; SQL/API/concurrency/migration/real-model tests and expanded scoped Ruff/Mypy CI checks. |
| Docs | Existing endpoint design, Gateway descriptions, runbook, implementation status, inventory, standard, index and manifest updated; this report and compact evidence are additive. |

## Verification evidence

[Machine-readable commands, counts and hashes](../evidence/prediction-endpoints-20261126/qualification.json)
refer to implementation commit eb30663. Overlapping suites must not be summed.

| Executed command / environment | Result |
| --- | --- |
| env -u ML_TEST_DATABASE_URL -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml tests/test_boundaries.py -q -rs --junitxml=/tmp/wp2-final-head.xml | PASSED, 1398 passed / 0 failed / 5 skipped, 78.44 seconds, exit 0. |
| ML_TEST_DATABASE_URL=(isolated fixture URL) /workspace/ml-system-prod/.venv/bin/python -m pytest tests/ml/test_prediction_catalog.py tests/ml/test_prediction_catalog_e2e.py tests/ml/test_prediction_gateway_docs.py tests/test_boundaries.py -q --junitxml=/tmp/wp2-pg-final-closure.xml | PASSED, 197 passed / 0 failed / 0 skipped, exit 0. PostgreSQL 17 in owned disposable wp2-catalog-pg-20261126 on 127.0.0.1:15492, tmpfs data, random per-test schemas. |
| /workspace/ml-system-prod/.venv/bin/python /tmp/wp2-pg-migration.py | PASSED, fresh random PostgreSQL schema, 0006 → 0007 → 0006 → 0007, preserved legacy row. Script uses Alembic Config.attributes database_url scoped to the owned fixture and drops only its newly created schema. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_serving_runtime.py | PASSED, 18 Ruff/format files, 8 scoped typed files. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_boundaries.py | PASSED. |
| /workspace/ml-system-prod/.venv/bin/python -m pyflakes bootstrap src tests/ml | PASSED. |
| /workspace/ml-system-prod/.venv/bin/python -m mypy --follow-imports=skip src/domain/ml.py src/domain/training src/domain/deployments src/domain/model_registry src/domain/prediction src/domain/datasets src/application/training src/application/deployments src/application/model_registry src/application/prediction src/application/datasets src/application/ml_services.py | PASSED, CI scope, 41 typed files. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_ingestion.py | PASSED, existing formatting/typing scope including migration environment. |
| /workspace/ml-system-prod/.venv/bin/python scripts/check_docs.py; /workspace/ml-system-prod/.venv/bin/python scripts/update_manifest.py --check; git diff --check | PASSED on the implementation commit, repeated for final documentation. |

The five existing skips require backend, live Databricks or broker fixtures.
An existing Starlette TestClient deprecation warning remains. Development iterations corrected
the retirement fixture, observation-clock comparison and actual MLP preprocessing field source;
the final results above contain zero failures.

The end-to-end tests run real separate CPU training children and real fixed-serving models,
with distinct Gateway/Serving HTTP application instances and SQL-backed metadata.
Controlled Identity/HTTP transport fixtures connect those instances in the test process.
They cover both MLP and EBM, evaluation, explicit approval, contract publication, deployment,
prediction-only discovery, actual model output and provenance.
After loading Serving, artifact materialization is disabled to prove discovery/inference avoid
a live artifact fetch. This is fixture qualification, not a live enterprise deployment.

## Acceptance and limitations

| Acceptance | Evidence |
| --- | --- |
| Prediction-only user obtains endpoint and calls both model profiles | Real-model HTTP workflow for MLP and EBM; no supplied endpoint ID in the client flow. |
| Tenant/auth/cursor isolation | Same endpoint name in two tenants; invisible details 404, denied grants, revoked-session fixture, signature/scope/expiry/page-limit errors and SQL-side bounds. |
| Immutable contracts and metadata review | Idempotent concurrent publication, conflicting concurrent publication, manifest/digest corruption, reviewed usage revisions and audit. |
| Legacy/restart/rollout/rollback | Incomplete legacy rows, explicit verified backfill, independent SQL read instances, pending v2 leaves v1 visible, activation and rollback update the current generation. |
| Migration and compatibility | SQLite and PostgreSQL upgrade/downgrade/re-upgrade preserve existing authority records; existing ML regression passes. |
| External production qualification | NOT_EXECUTED. Full Compose and Kubernetes dynamic lifecycle, live Mongo CLI authentication and enterprise MLflow/network/TLS qualification remain separate. |

The standalone CLI help, bounded/private file reader and publication use case are tested.
An actual live Mongo reviewer session was not used. Model-purpose text in engineering tests and
the permissive EBM quality thresholds are explicitly synthetic fixtures.
Production rollout must apply the additive migration before the new application readiness checks,
publish/backfill contracts through the approved operator role, and qualify actual dependencies.
Rollback order and metadata export considerations are documented in the operator guide.

READY FOR REVIEW for WP2. production_accepted=false.
Stop after local commits and manual review; no WP3 implementation or production action.

## PR #18 final review closure — 2026-11-27

This section supersedes the original delivery's unrestricted backfill wording and local-only
stop instruction. The user authorized only the two findings, synchronization with master,
verification, commits and pushing the existing PR branch. WP3 and automatic publication are
excluded. No changes were made to either Serving runtime, approval rules or qualification flags.

Reference PR head: `75802fd6c8f0f8cc2b8f5f3348e775dfd80730cf`.
Latest fetched master: `651ce174c247a7873eaf533b9ee44716d7bc9268`.
Synchronization merge: `f16d9ce2347c23cc76496fc8d2c495b78e7bd593`.
Corrections were tested on that merge plus the WP2 closure worktree; the implementation commit
is recorded in the final evidence refresh. A separate worktree preserves the WP3 branch unchanged.

### Finding 1 — historical bundle backfill

**Resolved through an explicit compatibility restriction and operator procedure.** The CLI
already supplies the qualified release's code revision to the loaders. MLP compares the bundle's
trusted digest when an expected digest is supplied; EBM requires it and also checks its runtime
compatibility contract. Neither permits publishing H with a qualified caller at C when H != C.
The extractor now requires the explicit trusted revision and rejects null/blank/oversized values
before accessing artifacts, preventing its former optional MLP trust bypass. It never substitutes
the manifest's self-reported digest. Artifact/member/golden checks are preserved.

No in-place cross-revision backfill is supported. Same-revision legacy publication remains
supported when verification succeeds. Historical bundles stay immutable and incomplete; keep
their qualified pinned Serving runtime if they must remain active. To obtain complete metadata
under C, train a new candidate/binding under C, evaluate, obtain fresh approval, publish via the
authenticated C CLI and use normal authorized deployment. Do not spoof `ML_CODE_REVISION`,
rewrite manifests or reuse old approvals. See the
[exact procedure](../plans/prediction-endpoint-discovery.md#backfill-compatibility-across-code-upgrades).

New regression tests train real MLP/EBM bundles with distinct synthetic historical/current
revision labels, invoke the real CLI composition and SQL writer with controlled Identity
transport, verify H rejection causes no contract/usage writes or bundle changes, then publish
and idempotently republish the new C binding. This exercises the enforcement; it does not claim
qualification of an actual historical release binary or enterprise runtime.

### Finding 2 — reverse proxy URLs

**Resolved at the API boundary.** SQL discovery returns logical metadata; Gateway uses named
routes and ASGI request root path to render origin-relative `detail_url` and `predict_url`.
All existing wire fields remain. Empty root paths preserve `/api/v1/...`; `/inference` yields
`/inference/api/v1/...`. No forwarded header is trusted as root-path configuration.

The existing real-model E2E now runs MLP and EBM under both roots. A prediction-only account
follows list → returned detail URL → returned predict URL and receives real model output with
matching binding version/manifest provenance. Auth, tenant, cursor, generation, readiness,
no-store and no-live-artifact discovery checks remain unchanged.

### Closure verification

Environment: isolated managed Linux worktree `/workspace/ml-wp2-closure`, Python 3.12.14.
`PY` below denotes `/workspace/ml-system-prod/.venv/bin/python`. Local files/data are synthetic;
PostgreSQL 17 uses only disposable `wp2-closure-pg-20261127`, loopback 15494, tmpfs storage and
random per-test schemas. No production resources or private configuration were accessed.

| Exact command / check | Final outcome |
| --- | --- |
| `PY -m pytest tests/ml/test_prediction_catalog.py tests/ml/test_prediction_catalog_e2e.py -q --junitxml=/tmp/wp2-closure-focused.xml` | PASS: 29 passed, 0 failed/skipped; exit 0. |
| Same focused targets with isolated `ML_TEST_DATABASE_URL` and `--junitxml=/tmp/wp2-closure-postgres.xml` | PASS: 29 passed, 0 failed/skipped; exit 0. |
| `env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL PY -m pytest tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp2-closure-full-20261127 --junitxml=/tmp/wp2-closure-full.xml` | PASS: 1,406 passed, 0 failed, 5 skipped; exit 0. |
| `PY scripts/check_serving_runtime.py` | PASS: existing Ruff/format/Mypy scope includes all correction modules/tests; exit 0. |
| `PY scripts/check_boundaries.py`; `PY -m pyflakes bootstrap src tests/ml` (separately) | PASS, each exit 0. |
| Existing CI core Mypy command from the original table above | PASS: 41 source files; exit 0. |
| `PY scripts/check_ingestion.py`, `PY scripts/check_ebm_typing.py`, `PY scripts/check_deployment.py`, `PY scripts/check_training_runtime.py`, `PY scripts/check_k8s_acceptance.py`, `PY scripts/check_init.py` (separately) | PASS, each exit 0; static gates, not deployment qualification. |
| `PY -m bootstrap.prediction_contract_cli --help` | PASS, exit 0; no external authentication requested. |

Final docs/manifest validation and exact commands/log hashes are recorded in the closure
qualification evidence. Counts overlap; do not add them. The five full-suite skips still require
real backend/Databricks/broker fixtures, and the existing Starlette TestClient warning remains.
Full Compose/K8s and external enterprise qualification were not executed for this closure.
GitHub PR/CI inspection with `gh pr view 18` returned HTTP 403 from GraphQL, so remote checks are
NOT VERIFIED here. Git transport is checked independently when pushing; API failure is not CI failure.

Both findings are addressed for review. `production_accepted=false`. Stop after pushing the
corrections to PR #18; do not merge or claim final remote CI approval.
