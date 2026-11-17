# Kubernetes Serving Runtime

This is the third Kubernetes engineering PR, after Foundation and Storage
Contracts. It preserves SQL governance and model behavior; ADR-019 records the
runtime identity and recovery rules.

## Prepare the isolated profile

Use the existing Minikube profile/context and Foundation's pinned image-lock
workflow. The production runtime image must contain this PR's source. Keep the
full source revision and the real containerd manifest digest; never substitute a
Docker image ID for an image manifest digest. Credentials and rendered private
resources remain under ignored `var/` with private permissions.

```sh
docker build --target production --build-arg ML_RELEASE_CODE_REVISION="$(git rev-parse HEAD)" \
  -t ml-k8s-serving:qualified -f Dockerfile.ml .
minikube -p minikube image load ml-k8s-serving:qualified
python scripts/k8s_foundation.py lock-images \
  --runtime-image ml-k8s-serving:qualified --mlflow-image ml-k8s-mlflow:3.6.0 \
  --minio-image ml-system-minio-source:20261109 --mc-image ml-system-mc-source:20261109 \
  --output var/k8s-serving-image-lock.json
python scripts/k8s_foundation.py prepare --serving-runtime \
  --namespace ml-k8s-serving-runtime --directory var/k8s-serving-runtime \
  --image-lock var/k8s-serving-image-lock.json --revision "$(git rev-parse HEAD)"
python scripts/k8s_foundation.py deploy \
  --namespace ml-k8s-serving-runtime --directory var/k8s-serving-runtime
python scripts/k8s_foundation.py verify \
  --namespace ml-k8s-serving-runtime --directory var/k8s-serving-runtime
```

As in Foundation, build/load the other approved images first. Public manifests
contain placeholders and external Secret references; do not apply them directly.
The helper refuses an existing namespace or output directory. Namespace selection
is restricted to the two explicit Minikube qualification namespaces. Do not use it
as an upgrade, Secret rotation or migration tool for a running installation.
The original Foundation profile remains local and retains its existing 40 resources;
the opt-in Serving profile renders 44, adding an SA, Role, RoleBinding and API-egress
policy. No cluster-wide role or automatic ServiceAccount token mount is added.

## Execute recovery and routing checks

```sh
python scripts/verify_k8s_serving.py --output var/k8s-serving-runtime/serving-result.json
```

This command is restricted to `--context=minikube` and namespace
`ml-k8s-serving-runtime`. It pauses Relay, Supervisor and Control while preparing
three real synthetic model fixtures in a dedicated operator Pod. It uses the
existing Training supervisor, independent bundle evaluator, Model Registry and
explicit approvals. The EBM fixture uses a named engineering-only quality policy
for a synthetic dataset. It does not claim product quality or Notebook parity.
No approval rule, dataset, algorithm or production policy is weakened globally.

Control/Gateway then restart with the opt-in native backend. The harness checks
MLP prediction through Gateway; normal Serving Pod deletion and a new Pod UID;
whole native Deployment deletion and ACTIVE reconstruction; unchanged SQL route,
generation, approval and spec digest across recovery; Control/Gateway restart;
new approved model update and confirmed old runtime retirement; rollback as a new
generation; and EBM inference through the same runtime contract. The operator's
password/tokens never enter receipts or logs. A sanitized receipt records checks,
source/image pins and route identities. Model payloads, role files and CA keys stay
in the private environment. Retain failed receipts and inspect that environment
before retrying; the harness refuses to overwrite an existing receipt.

The final EBM runtime, namespace/PVCs and operator are retained for inspection.
Relay and Supervisor stay scaled to zero: this is an isolated Serving qualification
stack, not an ongoing training service. An interrupted run may also leave Control
scaled to zero. Do not run the harness against shared data. Inspect its namespace
before explicitly cleaning it up; there is no automatic deletion of PVCs or the
Foundation/Storage namespaces. Operator lifetime is bounded to two hours.

## Failure handling and release

Gateway returns 503 on a missing/wrong model, stale generation/spec/Pod identity,
redirect or route change during a request. A legitimate upstream 4xx/429 remains
an error response after identity preflight. It cannot authorize an activation.
A Serving restart only changes physical Pod identity; an approved rollout changes
platform deployment identity/generation and SQL route revision.

An unavailable Kubernetes API, terminating/orphaned managed Pod or inconsistent
resource declaration retains the existing reservation. Investigate ownership and
restore API access. Do not force-delete Pods or edit SQL release/route fields to
free a slot. A failed candidate is fenced before cleanup and cannot arrive late
as an activation. Existing global capacity of two reservations remains unchanged.

Keep the approved image repository available at its immutable digest. New code/image
releases require new qualification; old ACTIVE recovery uses the image/source frozen
in its SQL spec. A legacy local deployment without that source field requires an
explicit new approved deployment before opting into Kubernetes. No old spec is
rewritten and no ACTIVE route is silently transferred to another runtime.

## Checks and limits

The ML CI includes runtime ownership/identity tests, existing SQLite/PostgreSQL
model governance and complete Backend acceptance. Foundation CI renders both
profiles and checks private bootstrap/configuration. Local checks:

```sh
python scripts/check_boundaries.py
python scripts/check_serving_runtime.py
python scripts/check_deployment.py
python scripts/check_k8s.py
python -m pytest tests/ml/test_serving_runtime.py tests/ml/test_model_governance.py \
  tests/ml/test_deployment_entrypoint.py tests/ml/k8s -q
```

Minikube does not qualify cross-node rescheduling, node partition/force deletion,
production CSI, registry loss, arbitrary credential rotation, HA controllers,
Autoscaling, mTLS/CNI enforcement or production rollout operations. Training Runtime,
observability and Production Operations remain the subsequent engineering PRs.
