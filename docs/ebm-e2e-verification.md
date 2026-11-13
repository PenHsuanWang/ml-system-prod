# EBM end-to-end verification

Executed evidence: [2026-11-13 machine-readable report](evidence/ebm-e2e-2026-11-13/report.json).
The isolated full Backend runner completed **1,004 passed, zero failures/errors/skips**, including
the final live EBM case. Python 3.12.16 / Torch 2.6.0 CPU were verified. Cached test images with
read-only current source mounts were used locally; CI rebuilds the repository images. The
report records both the tested local source commit and the published equivalent commit/tree.
Real enterprise-data and production qualification remain pending.

PR #4 extends the disposable Backend acceptance environment from published RawSnapshots
through DatasetBuilder, tenant DatasetVersion, Management HTTP submission, durable Outbox,
RabbitMQ/Taskiq Worker and training child, MLflow/S3, frozen-bundle evaluation, an explicit
test reviewer, Deployment Controller, fenced ACTIVE route, and HTTP Prediction Gateway.

The reusable harness is `bootstrap.ebm_e2e.verify_engineering_lifecycle`. It accepts explicit
`EBMRawSnapshotReference` IDs/digests and an authorized tenant actor, and reuses PR #3's
builder. It neither launches the Worker nor advances Controller states itself. It requires
an isolated dev environment and an `engineering-test-only*` policy before it performs work.
Automatic reviewer approval in this harness is solely engineering evidence.

## Reproduce

Use a fresh disposable Compose project, private credentials, and a dedicated writable
`ML_ACCEPTANCE_VAR_DIR`, as described in `compose.ml.acceptance.yaml`. Export
`ML_E2E_SOURCE_COMMIT=$(git rev-parse HEAD)` for the checked-out source being tested.
Use `docker-compose` in place of `docker compose` where the standalone CLI is installed.

```sh
python scripts/create_ml_env.py
export ML_ACCEPTANCE_VAR_DIR="$PWD/var/ebm-acceptance"
export ML_E2E_SOURCE_COMMIT="$(git rev-parse HEAD)"
mkdir -p "$ML_ACCEPTANCE_VAR_DIR"
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ebm-acceptance up --build -d management relay supervisor control gateway redis
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ebm-acceptance run --build --rm --no-deps acceptance \
  python -m pytest tests/ml/ebm/test_realdata_e2e.py -q \
  --junitxml=/app/var/acceptance/ebm-junit.xml
```

The test publishes ten deterministic synthetic Lots with two wafer targets per Lot using
existing ingestion infrastructure. The inputs follow exactly `ebm-raw-input-v1`. No
enterprise column mapping or raw enterprise data is assumed. The fixture and disposable
reviewer account are test setup, not external-service or identity doubles. The fast rejected
policy test uses the real child and local tracker; it is explicitly not full-stack evidence.

The test-only permissive policy is confined to the acceptance overlay. The normal Compose
profile's quality policy remains operator supplied. Existing service endpoints, source
adapters, training algorithms, state machines and governance rules are unchanged.

## Report and qualification

`$ML_ACCEPTANCE_VAR_DIR/acceptance/ebm-e2e.json` records source commit, snapshot IDs/digests
and source revisions in the Builder receipt, DatasetVersion and receipt digest, frozen Recipe
and code revision, durable Job/Attempt, actual MLflow Run/Model Version, Bundle digest,
Evaluation/policy/decision, test Approval, Deployment generation and Route revision, and
Gateway output/provenance. CI uploads it alongside JUnit and the complete Backend result.
Only allowlisted provenance and finite neural outputs are recorded; credentials, request
headers, source records and connection URLs are excluded.

- `ENGINEERING_E2E_PASSED` is written only after all live lifecycle assertions execute.
- `ENGINEERING_E2E_FAILED` and a safe failure code are written on an executed failure.
- `real_data_status` remains `EXTERNAL_QUALIFICATION_PENDING` for synthetic tests.
- `REAL_DATA_E2E_PASSED` is reserved for separately executed, approved enterprise data
  qualification. This harness never emits it or grants production Model Approval.

`test_execution` counts the live lifecycle case only. JUnit records all passed, failed and
skipped tests. Without stack endpoints the live case skips and produces no passing report;
`ML_REQUIRE_STACK_ACCEPTANCE=1` makes missing endpoints a failure. Do not reuse an old report
as evidence for a skipped or failed execution; use a fresh acceptance directory per run.

The reference CPU profile has two global physical Serving slots. The complete acceptance
runner executes the live EBM case after the existing MLP rollout suite; a successful EBM
deployment remains active until disposable stack teardown. Repeated live runs require a fresh
stack. A capacity rejection in a reused stack is a failed run, not passing qualification.

Remaining qualification requires approved de-identified snapshots, confirmed enterprise
schema mapping where needed, model-owner-approved quality policy, and an authorized human
reviewer's actual approval. Neural outputs remain uncalibrated and make no Energy/RUL claim.

After inspecting reports, stop only the disposable project:

```sh
docker compose --env-file .env.ml -f compose.ml.yaml -f compose.ml.acceptance.yaml \
  -p ebm-acceptance down --volumes --remove-orphans
```
