# WP6 dataset build orchestration

The branch `feature/dataset-build-orchestration` starts at
`20a22d262d21eff5271f79cd0bd0e3be96556797`, the master merge of PR #21.
Implementation and tests are at `692a86422aedb0a5c111bb2a75c3c2fe83bcd9a3`.

| Requirement | Implementation and evidence |
| --- | --- |
| Discover builder inputs, limits and result contract | Builder detail uses the approved EBM ID/version, input/output schema, shape and typed configuration. It publishes a synthetic submission example. |
| Build an immutable dataset through the API | The HTTP test selects published File Batch snapshots, submits a build without reading raw payloads in Management, runs a worker composed with a separate SQL engine, and reads the resulting dataset and verified lineage over HTTP. Test identities are controlled. |
| Durable admission and ownership | SQL queues canonical, digest-protected specs. An actor/tenant idempotency scope returns the same job without restoring grants. Tests cover reordered inputs, concurrent same-key submit, conflict, missing grants, cross-owner/cross-tenant isolation, unknown fields and a mismatched manifest. |
| Verified and atomic publication | The same SQL transaction persists the DatasetVersion, verifies and binds its receipt, checks the live lease and completes the job. A stale attempt rolls back catalog writes. The next attempt can publish successfully from the same immutable inputs. The original CLI uses the same preparation with its existing publication contract. |
| Bounded failure and recovery | Worker tests exercise denied publication after grants change, dependency retries, exhaustion, deadline failure and exclusive claiming. Active grants are resolved before data access and publication. Raw snapshot ownership is checked again during catalog registration. |
| Additive migration | Revision 0009 adds the queue. SQLite and PostgreSQL migration tests upgrade, downgrade to 0008, and upgrade again, preserving existing dataset metadata and lineage schema. |

The worker uses the existing SQL polling approach, the approved EBM transformer and the
immutable catalog, without sharing training job semantics. Polling and compute run outside
Management. The creation transaction and the publication fence are short SQL transactions.
The job stores neither credentials nor permissions. The worker resolves current account
grants using the same active-user lookup as the token resolver.

The initial policy uses a five-minute lease, 30-minute job deadline, three attempts,
five-second dependency retry delay and 100 pending jobs per tenant. Lease expiry fences
publication. It does not terminate an executing process. Automatic recovery can reclaim
expired work. WP6 keeps user cancellation and running-job retry deferred. The opt-in Compose
role shares dataset/execution roots and mounts raw snapshots read-only. The
[runbook](../ml-runbook.md#dataset-build-jobs-wp6) describes operation and rollback.

## Verification

Isolated managed Linux worktree `/workspace/ml-prediction-guide`, Python 3.12.14.
SQLite tests used disposable files. The PostgreSQL test used the previously isolated
`wp3-discovery-pg-20261127` PostgreSQL 17 tmpfs container on 127.0.0.1:15493 with per-test
schemas and synthetic data. The worker's account lookups were controlled in the HTTP/SQL
test and were not a live Mongo qualification.

```sh
WP6_PY=/workspace/ml-system-prod/.venv/bin/python
set -- tests/ml/dataset_builder tests/ml/test_datasets.py \
  tests/ml/test_training_discovery.py tests/ml/ingestion/test_persistence.py
env -u ML_TEST_DATABASE_URL "$WP6_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp6-sqlite-20261127 --junitxml=/tmp/wp6-sqlite.xml
# With ML_TEST_DATABASE_URL set to the isolated PostgreSQL instance:
"$WP6_PY" -m pytest "$@" -q \
  --basetemp=/tmp/wp6-postgres-20261127 --junitxml=/tmp/wp6-postgres.xml
env -u ML_TEST_DATABASE_URL -u ML_TEST_AMQP_URL -u ML_TEST_REDIS_URL \
  -u ML_TEST_MANAGEMENT_URL -u ML_TEST_PREDICTION_URL "$WP6_PY" -m pytest \
  tests/ml tests/test_boundaries.py -q --basetemp=/tmp/wp6-full-20261127 \
  --junitxml=/tmp/wp6-full.xml
```

| Scope | Outcome | Local evidence |
| --- | --- | --- |
| Focused SQLite | 70 passed, 0 failed/skipped, exit 0 | `/tmp/wp6-sqlite.log` and JUnit XML |
| Focused PostgreSQL | 70 passed, 0 failed/skipped, exit 0 | `/tmp/wp6-postgres.log` and JUnit XML |
| Full ML and boundaries | 1,450 passed, 0 failed, 5 skipped, exit 0 | `/tmp/wp6-full.log` and JUnit XML |

The full-suite skips require a disposable backend stack, live Databricks, external HTTP
endpoints, or RabbitMQ/Redis. The existing Starlette TestClient warning remains.
The test PostgreSQL container was stopped. Disposable test files were removed and the
logs and JUnit results above were retained.

Architecture boundaries, Pyflakes, CI-scoped Mypy, scoped Ruff and format checks,
and the repository deployment, training, ingestion, Serving, EBM, Kubernetes acceptance
and init static gates passed. The worker CLI help, docs, manifest and Git whitespace
checks passed. Docker Compose config rendered the reference and ingestion profiles plus
the optional dataset-build worker with synthetic environment values and `--env-file /dev/null`.
Full live Compose and Kubernetes acceptance, remote CI and external production
qualification are outside this local evidence. `production_accepted=false`.
