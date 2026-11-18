# Governed Kubernetes engineering acceptance

This is work package 5, following Foundation, Storage, Serving and Training
Runtime. ADR-021 defines the scope. All application roles run together in the
new `ml-k8s-acceptance` namespace. Earlier qualification namespaces and PVCs
are retained. This is a synthetic-data engineering gate, not production approval. See the
[executed review and immutable evidence](../reviews/k8s-acceptance-2026-11-18.md).

## Build and prepare

Start the existing local `minikube` profile/context. Build from a committed,
published source with no tracked modifications. Use the actual full commit and
containerd manifest digests, not Docker image IDs. The tools image extends the
same production build but adds dev tests/tools only for a bounded operator.

```sh
docker build -f Dockerfile.ml --target production \
  --build-arg ML_RELEASE_CODE_REVISION="$(git rev-parse HEAD)" \
  -t ml-k8s-acceptance:qualified .
docker build -f Dockerfile.ml --target k8s-acceptance \
  --build-arg ML_RELEASE_CODE_REVISION="$(git rev-parse HEAD)" \
  -t ml-k8s-acceptance-tools:qualified .
minikube -p minikube image load ml-k8s-acceptance:qualified
minikube -p minikube image load ml-k8s-acceptance-tools:qualified
python scripts/k8s_foundation.py lock-images \
  --runtime-image ml-k8s-acceptance:qualified --mlflow-image ml-k8s-mlflow:3.6.0 \
  --minio-image ml-system-minio-source:20261109 --mc-image ml-system-mc-source:20261109 \
  --output var/k8s-acceptance-images.json
python scripts/verify_k8s_storage.py lock-image \
  --local-tag ml-k8s-acceptance-tools:qualified --output var/k8s-acceptance-tools.txt
python scripts/k8s_foundation.py prepare --acceptance --namespace ml-k8s-acceptance \
  --directory var/k8s-acceptance --image-lock var/k8s-acceptance-images.json \
  --revision "$(git rev-parse HEAD)"
python scripts/k8s_foundation.py deploy --namespace ml-k8s-acceptance \
  --directory var/k8s-acceptance
python scripts/k8s_foundation.py verify --namespace ml-k8s-acceptance \
  --directory var/k8s-acceptance
python -m scripts.verify_k8s_acceptance \
  --tools-image "$(cat var/k8s-acceptance-tools.txt)" \
  --output var/k8s-acceptance-evidence
```

Load the remaining documented Foundation infrastructure images before locking.
The helper refuses existing namespace/private directories. Never apply the public
placeholder overlay directly. Private rendered resources, credentials, CA keys,
fixture identities and raw synthetic data stay below ignored `var/`. Upload only
the explicitly allowlisted result files, never the full private directory.

The opt-in Acceptance overlay alone sets a named engineering EBM policy and
60-second Training/10-second queue/one-second stop-grace budgets for bounded
faults. Strict production entrypoint, verified MLflow TLS and the production
model-environment value remain in use. Reviewer approvals target that isolated
test namespace's environment and never authorize a real production installation.
No shared base, workload, loss, seed, feature, schema or approval rule is changed.

## Executed operations and faults

The tools operator has no API token, retains non-root/read-only runtime security,
and stops after two hours. It creates scoped reviewer/viewer/foreign-tenant test
accounts and synthetic inputs. Existing ingestion and DatasetBuilder adapters
publish and verify the EBM input lineage; these are fixture preparation, not
evidence of a live external Warehouse. MLP includes a deliberately inverted
test-label dataset to verify real quality rejection.

All model fits go through HTTP admission, SQL Outbox, Relay, RabbitMQ/Taskiq,
Pod guardian, unchanged compute, MLflow registration and independent evaluation.
The harness exercises idempotency, tenant isolation, permission denial and missing
approval; explicit HTTP approval, native activation and Gateway inference;
Serving Pod deletion, ACTIVE Deployment reconstruction and Control/Gateway
restart; policy rejection; a real candidate reader-auth failure; approved update,
retirement and rollback as a new generation; EBM lineage/inference; and the global
two-Serving-reservation limit. Existing Training's seven real cases then execute
in this same namespace while both approved endpoints remain active.

The reader-auth fault changes only the source Secret copied by a newly created
candidate Pod. Its original value is restored in a finally block as soon as init
has made the private copy. Existing ACTIVE Pods retain their original private
configuration. The candidate must fail, be fenced and physically retired before
its capacity is released; its failure may increment the route authority revision
while the ACTIVE deployment, generation, approval and spec remain unchanged.
Never overwrite a failed receipt, patch SQL terminal/owner/route fields, forge a
stop receipt or force-delete Pods. Investigate retained resources after an
interruption, including reader configuration restoration and uncertain fits.

One explicit continuation exists for an interrupted harness immediately after
the first model's Pod/ACTIVE-resource recovery (exactly the first five checks).
With a new output directory, `--resume-after-recovery <old-result.json>` verifies
the same source, runtime/tools images, operator, one completed fit and unchanged
approved route before continuing. It preserves and hashes the original receipt;
it neither refits nor repeats approval. Later checkpoints are rejected. A fresh
CI qualification always starts from zero and does not use this option. Private
operation-error logs are for local diagnosis and must not be uploaded as evidence.

## Independent Compose regression and CI

The `Kubernetes governed acceptance` workflow builds pinned runtime/tools and
infrastructure, starts a disposable Minikube, locks actual manifests and runs the
same complete gate. Public artifacts contain result/training receipts, source/
image pins, Foundation smoke and bounded Pod status. No private manifests or
configuration are uploaded. Failure/partial receipts remain incomplete.

`ML contracts and CPU lifecycle` continues to run the entire existing isolated
real-service Compose Backend acceptance, including EBM, with zero required-suite
skips. Keep this independent gate; do not repoint Compose tests at Kubernetes or
replace the existing local adapter. Foundation/profile and fail-closed evidence
tests run in both relevant CI paths. Live Databricks remains a separate approved
external qualification.

Minikube acceptance leaves production CSI/flock across nodes, node partitions,
force deletion, CNI enforcement, enterprise identity/MLflow authorization,
production SLO/capacity, backup/restore and deployment operations unqualified.
The final Production Operations work package must qualify the actual company
environment. `production_accepted` remains false.
