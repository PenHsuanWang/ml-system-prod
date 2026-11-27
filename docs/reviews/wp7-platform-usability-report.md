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
