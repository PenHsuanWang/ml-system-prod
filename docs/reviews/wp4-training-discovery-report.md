# WP4 training recipe and job discovery

The feature branch `feature/training-discovery` starts at master
`db2c883ea6daf54d410dbab82c3145a6e4494bc5`, the merge of PR #19. The tested implementation
ends at `0301edbe0526da7a2dc11459463de5709242d41d`. This package adds six read operations
to the existing Management API.

| Requirement | Implementation and acceptance evidence |
| --- | --- |
| Discover approved recipes and CPU profiles | The catalog enumerates the existing workload registry. Architecture, resource profile and evaluation policy come from the same admission resolver used for submission. The recipe detail generates its schema and validated example from the public request models. |
| Select a compatible dataset and submit training | HTTP tests select the MLP and EBM recipes and dataset versions, submit the returned examples to the existing endpoint, and run the actual Supervisor and CPU trainer. The resulting jobs expose their model bindings and retain their spec and dataset digests. |
| Discover jobs within existing visibility | Job listing filters tenant, owner, state and exclusive creation-time bounds before applying SQL pagination. Tenant-wide access uses `ml:jobs:read_all`. Tests cover owner, peer, tenant-wide reader, cross-tenant access, pagination and invalid queries. |
| Inspect resolved job spec safely | The job digest is checked against the persisted immutable spec. The documented projection exposes approved model and training settings, recipe and dataset identity. Typed responses leave out internal snapshot and runtime data. |
| Read genuine metrics | The API reads only the output checkpoint for the observed current attempt, with checks for the dataset digest, ordered steps, finite values and bounded public metric names. Pending jobs report `unknown`. Tests verify real recorded MLP and EBM metrics and sanitized failure for invalid stored evidence. |

See the [Management API walkthrough](../ml-runbook.md#training-recipe-and-job-discovery-wp4).
The API presents CPU profile compatibility while actual resource budgets remain deployment
configuration. It reads persisted fit metrics rather than live progress or final model
evaluation data. The existing model registry remains the evaluation authority.

The workload contract retains its original field set. Discovery metadata is a separate
read-only value supplied by the admission resolver. Historical job specs and existing
checkpoint evidence use the current tables. Submission, idempotency, outbox, attempts,
recovery and approval behavior follow their existing entry points. The package adds no
migration. Roll back by reverting the WP4 application changes.

## Verification

Local Python 3.12.14, disposable SQLite or PostgreSQL 17 test data, managed Linux workspace
`/workspace/ml-prediction-guide`. PostgreSQL used the previously isolated
`wp3-discovery-pg-20261127` tmpfs container at 127.0.0.1:15493 with random per-test schemas.
It was stopped after the run. All data and identities were synthetic.

```sh
WP4_PY=/workspace/ml-system-prod/.venv/bin/python
set -- tests/ml/test_training_discovery.py tests/ml/test_training_contracts.py \
  tests/ml/test_workload_dispatch.py tests/ml/test_api.py tests/ml/ebm/test_platform_training.py
env -u ML_TEST_DATABASE_URL "$WP4_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp4-sqlite-final-20261127 --junitxml=/tmp/wp4-sqlite-final.xml
# With ML_TEST_DATABASE_URL pointing to the disposable PostgreSQL instance:
"$WP4_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp4-postgres-final-20261127 --junitxml=/tmp/wp4-postgres-final.xml
env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL \
  -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL "$WP4_PY" -m pytest \
  tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp4-full-final-20261127 \
  --junitxml=/tmp/wp4-full-final.xml
```

| Scope | Result | Local evidence |
| --- | --- | --- |
| Focused SQLite | 72 passed, 0 failed/skipped, exit 0 | `/tmp/wp4-sqlite-final.log` and JUnit XML |
| Focused PostgreSQL | 72 passed, 0 failed/skipped, exit 0 | `/tmp/wp4-postgres-final.log` and JUnit XML |
| Full ML and boundaries | 1,433 passed, 0 failed, 5 skipped, exit 0 | `/tmp/wp4-full-final.log` and JUnit XML |

The first regression run caught two failures because the implementation initially added
fields to the original workload record. The correction restores that record shape and keeps
the original assertions. The final runs test the corrected commit. Initial logs and JUnit
reports remain available after removal of temporary data directories.
The full-suite skips require the backend stack, approved Databricks, external HTTP endpoints
or RabbitMQ/Redis fixtures. An existing Starlette TestClient deprecation warning remains.

Architecture boundaries, Pyflakes, the CI Mypy scope, and targeted Mypy for the new API
and its dependencies passed. The repository training, ingestion, Serving, EBM, deployment,
Kubernetes acceptance and init static checks passed. Documentation, manifest and Git
whitespace checks passed.

Dynamic Compose and Kubernetes acceptance, remote CI and external production qualification
were unexecuted in this session. `production_accepted=false`.
