# WP5 model registry and deployment discovery

Branch `feature/registry-deployment-discovery` starts at master
`107da8c27316469df5c3631e3d216d848dcbccbf`, the merge of WP4 in PR #20.
The tested implementation is `ed39c64b85f3493e4f60b1a430e14c679c306341`.

| Requirement | Implementation and verification |
| --- | --- |
| Browse models and exact versions | New tenant-scoped SQL binding pages with exact name/version filters and deterministic order. Model family detail is a count and name. The existing models endpoint still returns a binding array. |
| Understand contract, lineage and evaluation | Contract and review digests reuse WP2 identities. Tests cover missing evidence, digest mismatch and a digest-valid document with invalid API field types. Lineage verifies the persisted training spec, applies job ownership and dataset grants separately, and withholds internal paths. Evaluation reads project known metric fields. |
| Inspect actual approvals | The paged approval query joins the exact binding, manifest and approved evaluation report. Existing evaluation and approval routes retain their contracts. |
| Survey deployment assignments | The same SQL records expose model/version, generation, route revision, state, approval and environment. The read-only identity grant permits inspection and denies create/rollback. A rollout and rollback test follows the existing SQL state writers through active assignments. |
| Inspect history and real events | Endpoint history pages stored generations and their observed states. The audit endpoint returns persisted deployment creation records only. Tests confirm that controller transitions do not become inferred events. Invalid route identity is explicit and readiness is unknown. |

Model reads require `ml:model:read`. Deployment reads accept the new `ml:deployment:read`
grant and the existing `ml:deploy` grant. The Identity alias is `ml.deployment.read`.
No grant migration is needed. Create and rollback both require `ml:deploy`.
The read projections use existing SQL tables with tenant filters and bounded pagination.
The Controller retains route authority. No database migration or new service deployment is needed.
Reverting WP5 removes the new read APIs and reinstates the original deployment detail permission.

See the [Management workflow](../ml-runbook.md#model-registry-and-deployment-discovery-wp5).
The Gateway remains the consumer-facing inference entry point. Governance reads use SQL as
their authority for binding and route identity. They do not query live MLflow or Serving.

## Verification

The test environment was the managed Linux workspace `/workspace/ml-prediction-guide`,
Python 3.12.14, synthetic identities, isolated temporary SQLite databases, and a disposable
PostgreSQL 17 tmpfs instance `wp3-discovery-pg-20261127` at 127.0.0.1:15493 with per-test
schemas. The PostgreSQL instance was stopped after testing.

```sh
WP5_PY=/workspace/ml-system-prod/.venv/bin/python
set -- tests/ml/test_governance_discovery.py tests/ml/test_model_governance.py \
  tests/ml/test_prediction_catalog.py tests/ml/test_prediction_catalog_e2e.py \
  tests/ml/test_training_discovery.py tests/ml/test_api.py tests/ml/test_backend_acceptance.py
env -u ML_TEST_DATABASE_URL "$WP5_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp5-sqlite-20261127 --junitxml=/tmp/wp5-sqlite.xml
# With ML_TEST_DATABASE_URL set to the isolated PostgreSQL instance:
"$WP5_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp5-postgres-20261127 --junitxml=/tmp/wp5-postgres.xml
env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL \
  -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL "$WP5_PY" -m pytest \
  tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp5-full-20261127 \
  --junitxml=/tmp/wp5-full.xml
```

| Scope | Outcome | Local evidence |
| --- | --- | --- |
| Focused SQLite | 52 passed, 0 failed/skipped, exit 0 | `/tmp/wp5-sqlite.log`, corresponding JUnit XML |
| Focused PostgreSQL | 52 passed, 0 failed/skipped, exit 0 | `/tmp/wp5-postgres.log`, corresponding JUnit XML |
| Full ML and boundaries | 1,436 passed, 0 failed, 5 skipped, exit 0 | `/tmp/wp5-full.log`, corresponding JUnit XML |

The five full-suite skips require a running backend stack, an approved Databricks fixture,
external HTTP endpoints or RabbitMQ/Redis. The existing Starlette TestClient warning remains.

Architecture boundaries, Pyflakes, CI-scoped Mypy, and the deployment, training, ingestion,
Serving, EBM, Kubernetes acceptance and init static checks passed. The deployment check now
covers the new WP5 code and tests. Documentation, manifest and Git whitespace checks passed.

Full dynamic Compose and Kubernetes acceptance, external production qualification and
remote CI have no local verification in this report. `production_accepted=false`.
