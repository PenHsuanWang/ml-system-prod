# WP7 platform usability and acceptance

Baseline `master@e6c257ea366d0aebbdfd14785d9631b089fce926` includes the merge of
PR #22 and its lease and retry corrections. This work is on
`feature/platform-usability-acceptance`.

## Delivered contracts

- Public `GET /api/v1/platform/info` identifies the current service and links
  to its API reference, capabilities and Identity routes when available.
- Authenticated `GET /api/v1/platform/capabilities` filters workflow entry
  points by the service's enabled modules and the actor's current permissions.
  Each action retains its own tenant, resource and approval checks. A listed
  capability can have no resources or an unavailable runtime.
- Management `GET /api/v1/platform/audit-events` requires `ml:operate` and an
  exact subject with an explicit seven-day maximum window. The SQL reader
  filters by authenticated tenant and uses bounded deterministic pages.
- Management and Gateway serve pinned Swagger/ReDoc assets and render paths
  under their configured ASGI prefixes. Optional same-origin Identity links are
  configured by the operator. Only the existing Identity handlers serve those
  requests through the documented proxy allowlist.
- Validation errors use the same request ID as the tracing header. The
  existing status codes, error fields and prediction API remain compatible.

The [quickstart](../ml-runbook.md#self-service-quickstart-wp7) includes role
handoffs, proxy configuration, support correlation and operational limits.
The change reuses the existing Identity service, SQL audit store and active
prediction catalog. Its additive HTTP and packaging changes require no
database migration. To remove same-origin sign-in, unset
`ML_GATEWAY_IDENTITY_PREFIX` and remove the exact proxy locations. A rollback
can restore the previous qualified image and proxy configuration using the
existing database.

## Verification

The workflow tests run real File Batch publication, SQL persistence, EBM dataset
building, PyTorch training, independent evaluation and fixed-serving inference.
The MLP path uses the approved fixture dataset. The EBM path starts from
published synthetic snapshots, builds an immutable dataset via HTTP, and trains
on that exact dataset version. Builder, trainer, reviewer, deployer and
prediction-client sessions use distinct fixture grants. The prediction user
starts from Gateway capabilities and runs the example from the endpoint's
verified contract. The fixture uses controlled identity/resolution and simulated
controller activation, so this proves application and SQL behavior. The isolated
Compose MLP test also provisions separate users via the existing real Identity
API and exercises the discovery links and session revocation when run against a
qualified stack.

The initial browser smoke identified a ReDoc CDN logo request and ambiguous
documentation links in an ASGI mount containing both services. Both findings
were corrected. Chromium rendered all four prefixed Swagger/ReDoc pages with
zero off-origin requests under network interception. The built Python wheel
contains the three distribution files and five accompanying license/notice files.

Implementation tested at `b0527bfb0178c8cf2eca4582609a2eb7363725e3` in managed
Linux with Python 3.12.14. The browser and wheel also passed at
`f1c6cfaf1075a6b9ab348ca08bd22ca3c7a7d336`, which has identical runtime and
asset files. For PostgreSQL, the focused suite used per-test schemas in a
disposable PostgreSQL 17 tmpfs container bound to loopback. SQLite used
temporary databases.

```sh
WP7_PY=/workspace/ml-system-prod/.venv/bin/python
unset ML_TEST_DATABASE_URL ML_TEST_AMQP_URL ML_TEST_REDIS_URL
unset ML_TEST_MANAGEMENT_URL ML_TEST_PREDICTION_URL ML_REQUIRE_STACK_ACCEPTANCE
"$WP7_PY" -m pytest tests/ml tests/test_boundaries.py -q \
  --basetemp=/tmp/wp7-full --junitxml=/tmp/wp7-full.xml
# For the focused run, set ML_TEST_DATABASE_URL to the disposable PostgreSQL fixture.
"$WP7_PY" -m pytest tests/ml/test_platform_portal.py \
  tests/ml/test_prediction_gateway_docs.py tests/ml/test_prediction_catalog_e2e.py \
  tests/ml/test_governance_discovery.py tests/ml/dataset_builder \
  tests/ml/test_training_discovery.py -q \
  --basetemp=/tmp/wp7-postgres --junitxml=/tmp/wp7-postgres.xml
"$WP7_PY" -m scripts.verify_init_compose
```

| Executed scope | Passed | Failed | Skipped | Exit |
| --- | ---: | ---: | ---: | ---: |
| Full ML and architecture regression, SQLite | 1,471 | 0 | 5 | 0 |
| Focused portal, Gateway, governance, builder and training, PostgreSQL | 106 | 0 | 0 | 0 |
| Chromium rendering of prefixed Swagger and ReDoc | 4 | 0 | 0 | 0 |

SQLite and PostgreSQL logs and JUnit XML are under `/tmp/wp7-full.*` and
`/tmp/wp7-postgres.*`. The full run skipped cases requiring the disposable
backend stack, approved external Databricks, isolated HTTP endpoints or live
RabbitMQ and Redis. Both runs reported the existing Starlette TestClient warning.

Architecture boundaries, Pyflakes, scoped Ruff lint and format, CI and focused
Mypy, ingestion/deployment/Serving/Training/EBM/init checks, Kubernetes
acceptance static checks, documentation and file manifest checks passed.
`/tmp/wp7-static-final.log` records the commands and exit statuses. Docker
Compose rendered the synthetic acceptance/ingestion/dataset-build profiles,
and `scripts.verify_init_compose` rendered all six generated profiles including
both external credential variants. The Chromium evidence is in
`/tmp/wp7-browser.log`. Its harness served both applications on a loopback
socket under different ASGI mounts and disallowed off-origin requests.
The documented proxy locations passed `nginx -t` with nginx 1.28 in an isolated
container with networking disabled.

## Deployment qualification

| Qualification | Status | Remaining evidence |
| --- | --- | --- |
| Live isolated Compose | BLOCKED | The managed Docker VFS host had less space than required for a writable copy of one dependency image. Run the role-separated MLP path and existing live EBM suite using the [Compose runbook](../ml-runbook.md#完整-compose-回歸). Qualify the proxy and WP6 build worker in the same disposable profile. |
| Live Kubernetes | BLOCKED | No isolated cluster or Minikube tooling was available. Run the [Kubernetes engineering gate](../deployment/kubernetes-acceptance.md) with current image/source pins. Confirm Gateway discovery, scoped Identity and the approved worker/storage configuration in the target topology. |
| Enterprise and production | NOT VERIFIED | Collect actual TLS, Identity, Databricks and MLflow authorization, storage, SLO/capacity, dependency outage and recovery evidence in the authorized environment. |

`production_accepted=false`.

## Final review closure

Reviewed head `201d0c3685a8bd47ee1f4c4253f42fd6f6158d3f`.
Closure implementation `dac0ea4f66181ae61c65a87602d1bfbcc651bb9b`.

### EBM evaluation

The [reviewed PostgreSQL job](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/45994945675/job/140491912216)
failed. Its public run page did not expose the persisted report rows, and the
artifact and log requests did not return the underlying reports. Local executions
queried the actual evaluation rows from isolated PostgreSQL before fixture teardown.
The complete baseline fixture passed locally. A controlled DC-only reproduction
used exactly representable offsets with an otherwise identical ramp and reached
the same `REJECTED_BY_POLICY` outcome through real ingest, build, fit and evaluation.

| Executed EBM inputs | Persisted loss | Persisted lot Pearson | Lots | Samples | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| Reviewed fixture on local PostgreSQL | 0.2206274 | -1 | 2 | 4 | APPROVED_BY_POLICY in both cases |
| Exact DC-only reproduction on local PostgreSQL | 0.2206299 | null | 2 | 4 | REJECTED_BY_POLICY in both cases |
| Revised lot-specific waveforms on local PostgreSQL | 0.2206964 | 1 | 2 | 4 | APPROVED_BY_POLICY in both cases |

The exact DC-only reproduction satisfies the loss, lot and sample bounds. Identical
lot scores make the correlation undefined, which the real policy rejects.
The old fixture could instead produce slightly different features through float
rounding, as observed on this host. The test now varies slope and curvature by
lot, checks pairwise variation in the built held-out tensors, and checks the
finite persisted correlation and qualified lot count before requiring approval.
The real publisher, transformer, training, evaluator, policy and prediction
remain in the test path. Diagnostic logs are `/tmp/wp7-closure-reviewed.log`,
`/tmp/wp7-closure-dc.log` and `/tmp/wp7-close-focus.log`.

### Management job links

The existing API URL resolver now renders Training and Ingestion job links
using the app's route names and ASGI root path. HTTP tests follow the returned
links under both root and `/management`. They cover Training admission and
status, and Ingestion admission, listing, status and published result. Existing
fields retain their response types and root deployment paths.

### Dataset Build Worker

The [opt-in Kubernetes workload](../../deploy/k8s/README.md#dataset-build-worker)
uses the WP6 SQL worker, a pinned non-root runtime, a private SQL and Identity
role file, and resource limits. It has writable dataset/execution subpaths
and a read-only RawSnapshot subpath. The dataset builder opens its raw store
without creating directories. The isolated profile creates a Mongo account
granted only `find` on `identity_users`. The reference Compose worker takes
its own lookup URI through private configuration. Migration 0009 is applied
through the existing migration role.

The local worker smoke used real PostgreSQL 17 with Alembic through 0009,
MongoDB 7.0.48 with authorization, the normal Management factory with synthetic
development settings, and two separate invocations of the actual worker entry
point. Mongo accepted account lookup and denied session reads and identity
writes. The first worker exited idle. The second consumed the job submitted
through HTTP, using unwritable RawSnapshot directories, and its result and
lineage were verified through Management. In the contract test, a new SQL
engine and replacement worker recovered an abandoned lease under a controlled
clock and published exactly one result. These tests establish process and SQL
recovery behavior on local fixtures.

The private acceptance overlay rendered with the scoped Secret, pinned
container and expected shared-storage mounts. The Kubernetes harness now
restarts the worker Pod, admits the EBM build through Management, and verifies
the published lineage before submitting training. Live Minikube acceptance
still requires an isolated cluster with sufficient disk. The managed egress
policy excludes `api.github.com`, and the workflow dispatch preflight returned
HTTP 403. An authorized runner can use the existing pull-request trigger or
`gh workflow run k8s-acceptance.yml --ref feature/platform-usability-acceptance`.
Qualification of the mounted storage and restart behavior in a running
Kubernetes namespace is pending.

### Verification evidence

The full contract suites use Python 3.12.14 on managed Linux and disposable
SQLite databases or isolated schemas in the PostgreSQL 17 tmpfs container.
The local suite excludes real external services via the repository's normal
environment gates. The hosted Backend Stack uses its own isolated dependencies.

```sh
WP7_PY=/workspace/ml-system-prod/.venv/bin/python
WP7_DB=sqlite  # Or postgres with ML_TEST_DATABASE_URL set to the isolated fixture.
"$WP7_PY" -m pytest tests/ml tests/test_boundaries.py -q \
  --basetemp="/tmp/wp7-close-full-$WP7_DB" --junitxml="/tmp/wp7-close-full-$WP7_DB.xml"
```

The local logs and JUnit XML use the prefix `/tmp/wp7-close-full-`. The live worker
evidence is `/tmp/wp7-close-live-worker.log`. The architecture, lint, typing,
Kubernetes manifest, Compose rendering and documentation gates are recorded in
`/tmp/wp7-close-static-final.log`. Hosted results are tied to the
[implementation workflow](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/46011077912).

| Executed closure suite | Passed | Failed | Skipped | Exit |
| --- | ---: | ---: | ---: | ---: |
| Full SQLite contracts and architecture | 1,476 | 0 | 5 | 0 |
| Full PostgreSQL contracts and architecture | 1,476 | 0 | 5 | 0 |
| Focused PostgreSQL diagnostics and regression | 27 | 0 | 0 | 0 |

Local skips cover the isolated Backend stack, approved live Databricks, external
HTTP endpoints, and live RabbitMQ/Redis receivers. The hosted contract matrix
runs its database suites with RabbitMQ and Redis. Both hosted contract jobs
passed. The [Hosted Backend Stack job](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/46011077912/job/140548591690)
started its isolated Compose roles and passed the zero-skipped integration gate,
including the existing separate live EBM case. The run uploaded its acceptance
artifacts. This evidence covers the implementation commit and the reference
backend, including the fixes for the two previously failing PostgreSQL cases.
The following report and manifest commit contains the same runtime and tests.
Kubernetes dynamic acceptance and enterprise qualification remain separate gates.

`production_accepted=false`.
