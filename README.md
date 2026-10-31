# ML System EBM

A Python ML system with durable jobs, isolated PyTorch CPU training, immutable inference bundles, exact-bundle evaluation, manual approval, fixed-version serving, and fenced rollout/rollback.

The code extends the pinned backend template in place. Domain and application layers exchange immutable DTOs through narrow Protocol ports. `bootstrap/ml_*` assembles role-specific implementations; generic `server.py` defaults remain unchanged. GPU, DDP, HPO and Kubernetes are outside this CPU MVP.

Implementation and target-environment qualification are tracked separately in [implementation status](docs/implementation-status.md).

The latest [Backend acceptance report](docs/backend-acceptance-2026-10-29.md) records 217 passing local tests and 3 skipped real-service cases. Full Backend acceptance remains incomplete until the required target-runtime and real-service checks execute successfully.

## Real local training

Use Python 3.11 or 3.12 in an isolated environment:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e '.[dev,training]'
python -m bootstrap.ml_cli demo
```

The demo explicitly applies migrations to `var/ml.db`, launches a separate training process, fits preprocessing only on the training split, exports a complete bundle, registers it in the **local development tracker**, evaluates that exact bundle on held-out data, and prints durable Job/model identities. It does not approve or deploy the model.

SQLite and the filesystem tracker are explicit development adapters. Non-dev settings require PostgreSQL, MLflow and pinned source/image configuration. The fixture is a deterministic two-feature binary CPU MLP with fixed train/validation/test counts of 384/64/64; its accuracy is not a production quality baseline.

Import a real tenant dataset explicitly after migration:

```sh
python -m bootstrap.ml_cli register-dataset --tenant-id demo \
  --dataset-id sensors --version v1 --source-path /path/to/sensors.json
```

The JSON contains `features` (rows of two finite numbers in `[-100,100]`), `labels` (0 or 1), and `splits` with nonempty `train`, `validation`, `test` index lists. Splits must be disjoint and cover every row; training must contain both classes. The importer validates and stores a content-addressed snapshot, including its split indices. Dataset versions are immutable and tenant-scoped. Submit its ID/version using the normal training API. Other feature shapes/tasks require a new approved recipe and schema.

## EBM neural workload

The approved `ebm_poc_replay_v1@1.0.0` workload adds immutable `[N,7,4,9,31]` feature snapshots, CNN training, safe epoch resume, complete neural bundles, exact-bundle evaluation and fixed-version neural serving. It uses the existing Job, Registry, Approval and Deployment flow.

```sh
python -m pip install -e '.[dev,training,ebm]'
python -m bootstrap.ml_cli demo-ebm
```

The synthetic development demo registers an EBM dataset and trains in a supervised child. Without an explicitly configured quality policy, training can succeed but evaluation rejects approval. See [EBM usage and implementation status](docs/ebm-implementation-status.md) for dataset import, API contracts, policy configuration and the independent bundle smoke. Databricks ingestion, calibrated Energy/RUL and batch reports remain separate work packages.

## Reference development stack

```sh
python scripts/create_ml_env.py
docker compose --env-file .env.ml -f compose.ml.yaml up --build -d
```

Management is at `http://127.0.0.1:8000`; prediction ingress is at `http://127.0.0.1:8001`. Roles include management, relay, training supervisor and controller, backed by PostgreSQL, RabbitMQ quorum queues, Redis, Mongo Identity, MLflow and MinIO. MLflow has its own metadata database. An explicit one-shot service applies migrations.

The local/VM controller starts a separate serving process for each generation; the reference ingress shares its network namespace in Compose. The standalone `serving` Docker target also accepts a controller-delivered DeploymentSpec and externally supplied expected digest. These images/Compose services still require actual build and qualification evidence.

Provision an operator explicitly; startup never creates accounts:

```sh
docker compose --env-file .env.ml -f compose.ml.yaml exec management \
  python manage.py create-admin --tenant-id demo --username mladmin \
  --permission ml.train --permission ml.cancel --permission ml.model.read \
  --permission ml.model.approve --permission ml.deploy --permission ml.predict \
  --permission ml.jobs.read_all --permission ml.operate
```

The command prompts for a password. Template permissions use dots; ML identity adapters map them to the explicit public `ml:...` scopes. Configure separate scientist, reviewer, deployment and prediction accounts as needed. Authenticate at `POST /api/v1/identity/login` and send the bearer token to ML endpoints.

Submit `POST /api/v1/training/jobs` with an `Idempotency-Key` and this body:

```json
{
  "spec": {
    "recipe": {"id": "nn_classifier", "version": "1.0.0"},
    "dataset": {"id": "dataset-demo", "version": "2026-10-28-v1"}
  }
}
```

The response contains a persistent `job_id` and `status_url`. Replay the same request/key after a lost response. Local key retention defaults to 24 hours from first acceptance; replay does not extend it. Query long-term history by Job ID. To retry a terminal job, submit a new key with optional top-level `retry_of_job_id`.

## Lifecycle endpoints

| Operation | Endpoint |
| --- | --- |
| Submit / query / cancel | `POST /api/v1/training/jobs`, `GET .../jobs/{id}`, `POST .../jobs/{id}/cancel` |
| Attempt history | `GET /api/v1/training/jobs/{id}/attempts` |
| Dataset metadata | `GET /api/v1/datasets/versions/{dataset_id}@{version}` |
| Models / evaluations | `GET /api/v1/model_registry/models`, `GET .../bindings/{id}/evaluations` |
| Manual approval | `POST /api/v1/model_registry/bindings/{id}/approvals` with `evaluation_id`, `environment` |
| Deployment | `POST /api/v1/deployments` with `endpoint_id`, `model_binding_id`, `approval_id` |
| Observe / rollback | `GET /api/v1/deployments/{id}`, `POST .../{id}/rollback` |
| Predict | `POST /api/v1/prediction/predict` on prediction ingress |
| Health / metrics | `/health/ready`; management `/metrics` requires `ml.operate` |

Prediction input is `{"endpoint_id":"quality","instances":[{"features":{"sensor_a":1.0,"sensor_b":1.0}}]}`. Outputs carry fixed model/deployment provenance and explicitly uncalibrated softmax scores. Serving does not resolve an alias per request. Failed rollout keeps the healthy route; rollback creates a new deployment/generation.

## Tests and operations

Run the reproducible local acceptance checks with `python scripts/verify_ml_acceptance.py`. The [full-stack acceptance procedure](docs/backend-acceptance-2026-10-29.md#可重跑的驗收入口) uses a disposable Compose project, real services and `--require-stack`; missing endpoints, skipped required cases or runtime mismatch prevent acceptance. CI includes this stack job, but no remote run is claimed.

```sh
python scripts/check_boundaries.py
python -m pytest tests/ml tests/test_boundaries.py -q
python -m bootstrap.ml_cli migrate
python -m bootstrap.ml_cli relay --once
python -m bootstrap.ml_cli control --once
```

Tests use isolated SQLite files by default. Set `ML_TEST_DATABASE_URL` to a dedicated PostgreSQL database to run contracts in isolated, automatically removed schemas. `ML_TEST_AMQP_URL` and `ML_TEST_REDIS_URL` enable the real Taskiq Receiver tests, including a database trigger that blocks terminal persistence and verifies that ACK evidence stays absent until recovery. CI defines both database profiles with broker services.

W3C trace context and request IDs propagate through HTTP, durable Outbox, publisher and worker; prediction ingress forwards the same context to the fixed serving process. Bodies, credentials and baggage are excluded. Install `.[observability]` and set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable bounded OTLP HTTP export; the role images include this extra. Exporter availability is not business truth. The reference serving pool has two runtime slots and retains reservations until physical retirement is confirmed.

See the [runbook](docs/ml-runbook.md) for recovery and backup/restore. Inherited template locks describe the upstream baseline; target ML dependency locks, image digests/SBOM, real-service CI and numeric SLOs still require environment evidence.

## Design and provenance

- [HLD](docs/pytorch-ml-system-hld.md), [LLD v1.3](docs/pytorch-ml-system-lld.md), [ADR-014](docs/adr/014-implementation-baseline.md).
- [Feature spec](specs/001-durable-training-jobs/spec.md), [implementation plan](specs/001-durable-training-jobs/plan.md).
- [EBM Notebook fit–gap review](docs/EBM_PoC_ML_System_Fit_Gap_2026-10-29.md), [EBM integration plan](docs/ebm-integration-plan.md), [current EBM implementation](docs/ebm-implementation-status.md): local neural training and platform serving are implemented; calibration/RUL, Databricks and target-environment qualification remain pending.
- [Initial review](docs/architecture-review-2026-10-29.md), [v1.2 follow-up](docs/architecture-review-2026-10-29-v1.2.md).
- [Upstream commit](docs/template-baseline.txt). Root v1.0 architecture files are explicitly superseded.
- The original documentation manifest is retained in `docs/archive/FILE-MANIFEST-v1.1.json`; the root manifest describes the current source bundle.
