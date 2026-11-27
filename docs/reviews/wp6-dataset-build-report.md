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

The job policy has a 30-minute deadline, three attempts, a five-second dependency
retry delay and a cap of 100 pending jobs per tenant. Automatic recovery can reclaim
expired work. WP6 keeps user cancellation and manual running-job retry deferred.
The opt-in Compose role shares dataset/execution roots and mounts raw snapshots read-only. The
[runbook](../ml-runbook.md#dataset-build-jobs-wp6) describes operation and rollback.

## Initial verification

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

## PR #22 final review closure

Master baseline `20a22d262d21eff5271f79cd0bd0e3be96556797`.
Reviewed feature head `8cd71aac5cf1c33bf721abd369f82182499edbf9`.
Corrected implementation verified at `2e1bc04c31eb4ec4713cf7f20e3b2047f0c8d19e`.

| Finding | Correction and regression evidence |
| --- | --- |
| Long-running execution | A job-scoped heartbeat renews the five-minute lease every minute. Both claims and renewals cap the lease at the job deadline. Renewal, failure and completion sample time after locking the current token's row. Tests advance a controlled clock while the real worker runs an approved EBM build, recover from a missed heartbeat, reject renewal at expiration, reclaim the job, reject stale publication before and after the replacement succeeds, and enforce the deadline during publication. |
| Transient dependency failures | SQL classifies invalidated connections, pool timeouts, selected PostgreSQL SQLSTATEs and SQLite busy/locked results. Mongo classifies connection failures during active-user lookup. Shared storage classifies specific temporary I/O errors. Controlled driver failures exercise both outcomes of a lost commit acknowledgement. A rolled-back publication can retry. A committed success retains one DatasetVersion, one receipt and one success audit event. Tests also cover terminal Mongo errors, storage permission failures, digest corruption and revoked grants. |

The SQL transaction binds the dataset and lineage receipt and marks the job
successful together. A failed ownership check rolls back all three writes. The
existing schema and migration history support the heartbeat and fencing conditions.

The focused suites used disposable SQLite files and the isolated PostgreSQL 17
tmpfs fixture described above. They include the revision 0009 upgrade, downgrade
and re-upgrade regression. Mongo driver failures, commit acknowledgement loss and
temporary filesystem errors were injected at the infrastructure boundaries. The
long-running test advances a controlled clock and exercises real build logic and
SQL publication. It measures lease behavior independently of workload duration.

```sh
WP6_PY=/workspace/ml-system-prod/.venv/bin/python
unset ML_TEST_DATABASE_URL ML_TEST_AMQP_URL ML_TEST_REDIS_URL
unset ML_TEST_MANAGEMENT_URL ML_TEST_PREDICTION_URL
set -- tests/ml/dataset_builder tests/ml/test_datasets.py \
  tests/ml/test_training_discovery.py tests/ml/ingestion/test_persistence.py
"$WP6_PY" -m pytest "$@" -q --basetemp=/tmp/wp6-closure-sqlite \
  --junitxml=/tmp/wp6-closure-sqlite.xml
# Set ML_TEST_DATABASE_URL to the disposable PostgreSQL test instance.
"$WP6_PY" -m pytest "$@" -q --basetemp=/tmp/wp6-closure-postgres \
  --junitxml=/tmp/wp6-closure-postgres.xml
unset ML_TEST_DATABASE_URL
"$WP6_PY" -m pytest tests/ml tests/test_boundaries.py -q \
  --basetemp=/tmp/wp6-closure-full --junitxml=/tmp/wp6-closure-full.xml
```

Each test command was run from the feature worktree on managed Linux with
Python 3.12.14. Logs and JUnit XML are local under `/tmp/wp6-closure-*`.

| Scope | Passed | Failed | Skipped | Exit |
| --- | ---: | ---: | ---: | ---: |
| Focused SQLite | 79 | 0 | 0 | 0 |
| Focused PostgreSQL | 79 | 0 | 0 | 0 |
| Full ML and boundaries | 1,459 | 0 | 5 | 0 |

The full run skipped tests requiring the disposable backend stack, a live
Databricks fixture, external Management and Prediction HTTP endpoints, or real
RabbitMQ and Redis. The existing Starlette TestClient deprecation warning appeared.

Architecture boundaries, Pyflakes, CI-scoped Mypy, EBM typing, scoped Ruff
lint and formatting, and the repository ingestion, deployment, Serving, training,
Kubernetes acceptance and init static gates passed. Documentation, manifest and
Git whitespace checks passed. `/tmp/wp6-closure-static.log` records each exact
command and exit status.

The SQL deadline fences publication and lease ownership. Compute already in progress
can continue until it returns or the process stops. Live MongoDB failover,
real shared-volume outages, long-duration capacity, live Compose and Kubernetes,
and production deployment remain unverified for this closure.
`production_accepted=false`.
