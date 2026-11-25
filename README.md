# ML System EBM

A Python ML system with durable jobs, isolated PyTorch CPU training, immutable inference bundles, exact-bundle evaluation, manual approval, fixed-version serving, and fenced rollout/rollback.

The code extends the pinned backend template in place. Domain and application layers exchange immutable DTOs through narrow Protocol ports. `bootstrap/ml_*` assembles role-specific implementations; generic `server.py` defaults remain unchanged. GPU, DDP and HPO remain outside this CPU MVP. Kubernetes engineering qualification is documented below.

Implementation and target-environment qualification are tracked separately in [implementation status](docs/implementation-status.md).

PR #11 的最終 source `43b6606` 已通過完整 K8s engineering acceptance 與獨立 Compose Backend 驗收（1169 passed，零 failure／error／skip）。實際 source／image、原始證據及 production 限制集中於 [實作狀態](docs/implementation-status.md)；文件入口見 [docs 導讀](docs/README.md)。

## Choose a deployment path

| Goal | Entry point | What it starts / requires |
| --- | --- | --- |
| Local algorithm development | `bootstrap.ml_cli demo` / `demo-ebm` below | SQLite, a development tracker and synthetic data |
| Reference development stack | `compose.ml.yaml` below | Project-owned infrastructure, MLflow and MinIO; `dev` settings |
| Reference stack with batch ingestion | Add Ingestion / Databricks overlays below | The separate Ingestion Worker plus approved source configuration |
| One-node Kubernetes engineering qualification | [K8s profile index](deploy/k8s/README.md) and [Acceptance runbook](docs/deployment/kubernetes-acceptance.md) | Pinned production image, private role Secrets/CA, native Serving and Pod-aware Training in isolated Minikube |
| A release using existing external services | [Day-1 deployment runbook](docs/deployment/day1-deployment-runbook.md) | Pinned release image/source, production settings, existing MLflow and approved infrastructure / secrets |

Start with the Day-1 runbook when MLflow and Databricks are already supplied by another team. It gives the preflight, migration, dependency qualification, complete role startup, operator source registration, smoke tests and Go/No-Go evidence in one order. The reference development command is not the production deployment command.

The [2026-11-10 deployment delivery record](docs/reviews/day1-deployment-2026-11-10.md) separates the new profile's implementation and isolated qualification from site-specific production gates. The earlier qualified-source CI above does not claim this new profile or your external services have passed production acceptance.

Raw acquisition publishes an immutable **Raw Snapshot**. Training consumes a separately approved
**DatasetVersion**. The EBM path includes an explicit operator-only RawSnapshot-to-DatasetBuilder;
ingestion does not trigger it automatically.

Use the [configuration reference](docs/configuration-reference.md) for actual defaults,
role configuration and timeout constraints, and the [ML operations contract](docs/ml-runbook.md#api-與-client-契約)
for permissions, idempotency, cancellation and errors. The [documentation/code audit](docs/reviews/documentation-code-audit-2026-11-19.md)
maps the important implementation areas to their maintained guides.

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

The synthetic development demo registers an EBM dataset and trains in a supervised child. Without an explicitly configured quality policy, training can succeed but evaluation rejects approval. See [EBM usage and implementation status](docs/ebm-implementation-status.md) for the explicit DatasetBuilder, dataset import, API contracts, policy configuration and the independent bundle smoke. [Databricks batch ingestion](docs/databricks-ingestion-runbook.md) remains a separate Raw Snapshot capability; automatic ingestion-to-build orchestration, calibrated Energy/RUL and batch reports remain separate work packages.

## Reference development stack

首次設定可先安裝 base package，執行互動式引導：

```sh
python -m pip install -e .
ml-system init
```

Wizard 支援本地 Compose、File／Databricks，以及既有外部服務 profile；隱藏輸入憑證，
產生相容設定，並在覆寫前提供備份或取消。詳見[互動式初始化手冊](docs/deployment/interactive-setup.md)。
完成後依畫面提供的 env／Compose 組合繼續。以下保留原有的簡單憑證產生方式：

```sh
python scripts/create_ml_env.py
docker compose --env-file .env.ml -f compose.ml.yaml up --build -d
```

`create_ml_env.py` generates private credentials for this local reference stack and refuses to overwrite an existing file. It is not production secrets provisioning. This Compose file explicitly uses the built-in `http://mlflow:5000` endpoint and `dev` mode; adding an external URI to `.env.ml` alone does not replace those service settings or dependencies.

Management is at `http://127.0.0.1:8000`; prediction ingress is at `http://127.0.0.1:8001`. Roles include management, relay, training supervisor and controller, backed by PostgreSQL, RabbitMQ quorum queues, Redis, Mongo Identity, MLflow and MinIO. MLflow has its own metadata database. An explicit one-shot service applies migrations.

The local/VM controller starts a separate serving process for each generation; the reference ingress shares its network namespace in Compose. The standalone `serving` Docker target also accepts a controller-delivered DeploymentSpec and externally supplied expected digest. Reference images and the complete Backend path have the qualification above; a production release still needs its own approved image/code identity and deployment evidence.

To add the implemented Databricks worker to the **complete reference stack**, first copy and fill the approved paths in `.env.ml.ingestion.example`, then use all three files:

```sh
cp .env.ml.ingestion.example .env.ml.ingestion
# Set the approved host paths before continuing.
docker compose --env-file .env.ml --env-file .env.ml.ingestion --profile ingestion \
  -f compose.ml.yaml -f compose.ml.ingestion.yaml -f compose.ml.databricks.yaml \
  config --quiet
docker compose --env-file .env.ml --env-file .env.ml.ingestion --profile ingestion \
  -f compose.ml.yaml -f compose.ml.ingestion.yaml -f compose.ml.databricks.yaml \
  up --build -d
```

This starts Management, Relay, Supervisor, Controller, Gateway and Ingestion Worker with reference dependencies. `migrate` is a one-shot service and should exit successfully. `ingestion_cli health` checks local configuration/storage/schema, not Warehouse OAuth or query access. Complete the [source registration and qualification](docs/databricks-ingestion-runbook.md) before accepting data; use the [Day-1 runbook](docs/deployment/day1-deployment-runbook.md) for external MLflow and release deployment.

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

Run the reproducible local acceptance checks with `python scripts/verify_ml_acceptance.py`. The [full-stack acceptance procedure](docs/ml-runbook.md#完整-compose-回歸) uses a disposable Compose project, real services and `--require-stack`; missing endpoints, skipped required cases or runtime mismatch prevent acceptance. The source-qualified CI linked in implementation status executed this job. External Databricks and host-only Compose qualification have independent scopes; they are not silently counted as Backend acceptance.

```sh
python scripts/check_boundaries.py
python -m pytest tests/ml tests/test_boundaries.py -q
python -m bootstrap.ml_cli migrate
python -m bootstrap.ml_cli relay --once
python -m bootstrap.ml_cli control --once
```

Tests use isolated SQLite files by default. Set `ML_TEST_DATABASE_URL` to a dedicated PostgreSQL database to run contracts in isolated, automatically removed schemas. `ML_TEST_AMQP_URL` and `ML_TEST_REDIS_URL` enable the real Taskiq Receiver tests, including a database trigger that blocks terminal persistence and verifies that ACK evidence stays absent until recovery. CI defines both database profiles with broker services.

W3C trace context and request IDs propagate through HTTP, durable Outbox, publisher and worker; prediction ingress forwards the same context to the fixed serving process. Bodies, credentials and baggage are excluded. Install `.[observability]` and set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable bounded OTLP HTTP export; the role images include this extra. Exporter availability is not business truth. The reference serving pool has two runtime slots and retains reservations until physical retirement is confirmed.

Use the [Day-1 runbook](docs/deployment/day1-deployment-runbook.md) for installation and Go/No-Go, then the [runtime runbook](docs/ml-runbook.md) for recovery and backup/restore. Inherited template locks describe the upstream baseline; production dependency locks, image digests/SBOM, capacity/SLOs and restore objectives still require approved environment evidence.

## Design and provenance

See the [documentation index](docs/README.md) for architecture, operations,
extension contracts, retained evidence and historical design references.

## Kubernetes migration

目前 K8s 採用分離的 Management、Relay、Pod-aware Training、Control／Gateway 共用
Pod，以及獨立的固定版本 Serving。請先讀 [Kubernetes 架構與 Minikube 運行現況](docs/deployment/kubernetes-architecture.md)：
內含 C4 Container／Deployment 圖、Pods／Services／DNS／NetworkPolicy、PVC／暫存空間、
Secret／RBAC 與資源設定，並區分 PR #11 最終宣告和本地保留的驗收環境快照。

The six-work-package roadmap is defined in [ADR-017](docs/adr/017-kubernetes-runtime-boundaries.md).
Foundation, Storage Contracts, Serving Runtime and Pod Training Runtime have
local Minikube qualification. Governed Kubernetes Acceptance combines their runtime
contracts with the full ML lifecycle and fault suite. Their operating instructions and explicit limits are:

- [Foundation](docs/deployment/kubernetes-staging-runbook.md)
- [Storage Contracts](docs/deployment/kubernetes-storage-contracts.md)
- [Serving Runtime](docs/deployment/kubernetes-serving-runtime.md)
- [Pod Training Runtime](docs/deployment/kubernetes-training-runtime.md)
- [Governed Kubernetes Acceptance](docs/deployment/kubernetes-acceptance.md)

The independent Compose Backend regression remains required. Actual company-environment
Production Operations is the final work package. Local qualification is not production approval.

## Runtime source management

Databricks initialization supports connection-only setup. Authorized operators
register immutable, policy-governed source versions through the Management API.
Workers resolve exact SQL catalog definitions at execution. Legacy templates
and worker-only credentials retain their existing contracts.
See [runtime source management](docs/runtime-source-management.md) for permissions,
lifecycle behavior and separate external qualification requirements.

## Optional corporate build network

The three Dockerfiles accept the optional BuildKit corporate_ca secret while
standard builds retain public trust and require no enterprise files. See
[build trust, proxy responsibilities and runtime separation](docs/deployment/day1-deployment-runbook.md#optional-corporate-build-network).
TLS verification, Go checksums and MinIO/MC source pins remain enabled.
