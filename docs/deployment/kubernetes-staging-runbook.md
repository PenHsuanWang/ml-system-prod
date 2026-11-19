# Kubernetes Foundation — Minikube Runbook

Scope: fresh, isolated, **idle-role** startup on one-node Minikube. This is PR 1
of the six-PR migration. Read [ADR-017](../adr/017-kubernetes-runtime-boundaries.md)
before running commands. ACTIVE-serving recovery, cross-Pod training recovery,
real CSI qualification, HA and production readiness are not delivered here.

This boundary applies to the Foundation profile. Native Serving, Pod Training and
combined Acceptance are now implemented as opt-in overlays; select the current
[deployment profile](../../deploy/k8s/README.md) before following a runbook.

## 1. Prerequisites and review

Use Linux/WSL2, Docker, a running `minikube` profile, kubectl with Kustomize,
OpenSSL, and Python 3.11+ with PyYAML (`python -m pip install 'PyYAML>=6,<7'`).
The original Foundation smoke used 2 CPUs and approximately 10 GB RAM; this is a
historical observation, not a minimum or capacity qualification for full Acceptance.
Reserve disk space for images and PVCs. Infrastructure is single replica with local hostpath storage.
The application helper intentionally has no production-context option.

```sh
git status --short --branch
minikube -p minikube status
kubectl --context=minikube get nodes
kubectl --context=minikube get storageclass
python scripts/check_k8s.py
python -m pytest tests/ml/k8s --noconftest -q
```

Do not include existing unrelated untracked drafts in commits. Before each
work package fetch master and create its feature branch. Review precise staged
paths and commit in separate design, deployment and validation units. Open a
Draft PR with executed checks and remaining gates; do not merge automatically.

## 2. Build and load the reviewed images

Run from the repository root. The runtime source must be a reviewed commit;
record its SHA alongside the loaded image. The example tags are local transport
names; the generated deployment uses the actual containerd manifest digest.

```sh
k8s_revision=$(git rev-parse HEAD)
docker build --target production --build-arg "ML_RELEASE_CODE_REVISION=$k8s_revision" \
  -f Dockerfile.ml -t ml-k8s-foundation:local .
docker build -f Dockerfile.mlflow -t ml-k8s-mlflow:local .
docker build --target server -f Dockerfile.minio -t ml-k8s-minio:local .
docker build --target client -f Dockerfile.minio -t ml-k8s-mc:local .

minikube -p minikube image load ml-k8s-foundation:local
minikube -p minikube image load ml-k8s-mlflow:local
minikube -p minikube image load ml-k8s-minio:local
minikube -p minikube image load ml-k8s-mc:local
minikube -p minikube image pull postgres:17
minikube -p minikube image pull rabbitmq:4.4-management
minikube -p minikube image pull redis:7
minikube -p minikube image pull mongo:7
minikube -p minikube image pull nginx:1.28-alpine

python scripts/k8s_foundation.py lock-images \
  --runtime-image ml-k8s-foundation:local --mlflow-image ml-k8s-mlflow:local \
  --minio-image ml-k8s-minio:local --mc-image ml-k8s-mc:local
python scripts/k8s_foundation.py prepare \
  --image-lock var/k8s-image-lock.json --revision "$k8s_revision"
```

The lock command reads containerd descriptors and registers digest aliases for
locally imported images. It does not use Docker `.Id` as a manifest digest.
Retain `var/k8s-image-lock.json`; do not hand-edit it to make a failed image match.
For remote registries use their verified manifest references and availability.
This local lock is not a signature/provenance or vulnerability qualification.

`prepare` creates private `var/k8s-foundation/` once, with random credentials,
role JSON, an MLflow auth configuration, a 30-day test CA/certificate and the
private Kustomize overlay. It refuses overwrites. All role private values come
from files, not `secretKeyRef` environment injections. Real Secrets, CA private
keys and rendered private manifests must remain outside Git and shared logs.
The public JSON examples are deliberately invalid placeholders.

Init copies projected Secret/CA files into a UID-owned private volume because the
production loader rejects symlinks. Running Pods retain their private copies;
changing a Secret does not rotate those credentials in place. Production rotation,
expiry alerts and coordinated Pod replacement require the site operations plan.
See the [configuration layers](../configuration-reference.md#profile-與設定層).

## 3. Start the fresh namespace

```sh
python scripts/k8s_foundation.py deploy
```

Order: common declarations and dependencies → ready infrastructure → completed
MinIO initialization → completed application migration → application roles.
Migration uses `backoffLimit: 0`. Its Job failure stops the helper before applying
runtime Deployments; every role's init also verifies SQL table readiness.
The production entrypoint validates each role again before exec.

The helper refuses an existing namespace and is not a release-upgrade tool.
On failure keep private output, Job/pod status and logs. Correct the reviewed
declarations, inspect the existing schema and reconcile deliberately with the
same private inputs. Do not regenerate passwords against existing PVCs, delete
uncertain execution directories, force Job completion, or run schema downgrade.
Private rendered stage files contain credentials: do not attach them to PRs.

## 4. Verify the foundation boundary

```sh
kubectl --context=minikube -n ml-k8s-foundation get jobs,pods,pvc
kubectl --context=minikube -n ml-k8s-foundation logs job/ml-migrate
kubectl --context=minikube -n ml-k8s-foundation get events --sort-by=.lastTimestamp
```

All six dependency Deployments should be ready; `minio-init` and `ml-migrate`
must complete with exit 0. The five application Deployments contain seven
long-running/one-shot role definitions in total: management, relay, supervisor,
control, gateway, ingestion-worker, plus migration. Six roles run continuously.
Each `prepare-ROLE` init must finish with its safe bootstrap validation marker.

HTTP probes on Management and Gateway test service/DB/identity availability.
They are **not** proof that a model is loaded. Non-HTTP worker readiness here is
container startup, not heartbeat/progress or failure recovery acceptance.
No accepted jobs, identities or model deployments are created automatically.

For a local Management readiness check, use a separate terminal:

```sh
kubectl --context=minikube -n ml-k8s-foundation port-forward service/ml-management 18000:8000
```

Then request `http://127.0.0.1:18000/health/ready`. Gateway is available through
`service/ml-gateway`, port 8001. Do not expose either endpoint with NodePort or
Ingress as a shortcut. Business APIs retain JWT authorization.

Run the bounded MLflow HTTPS/auth smoke inside the trusted control container:

```sh
python scripts/k8s_foundation.py verify
```

This provisions the separately generated reader using the MLflow admin API,
then tests writer access, reader access, unauthenticated rejection, untrusted-CA
rejection and reader write denial against a retained local experiment. It also
checks Management/Gateway readiness and records `var/k8s-foundation/smoke.json`.
The helper refuses to overwrite this receipt; preserve each attempt. Never
print the role JSON or pass credentials as command-line arguments. The existing
`bootstrap.deployment_cli qualify-mlflow` remains the formal artifact/permission
qualification entry point; health/TLS smoke does not replace its evidence.

## 5. Operational limits and handoff

- Fixed two global serving reservations, with rollout headroom required.
- Control/Gateway share networking and files; shared networking does not share PID namespaces.
- Separate supervisor keeps child-process training. Control cannot reliably
  observe its processes across Pods; no real workloads until the runtime PRs.
- No ACTIVE runtime rebuilding yet. Do not claim safe model recovery or zero-downtime rollout.
- Secret/CA init copies are snapshots. Rotation needs coordinated upstream
  changes and affected Pod recreation; no hot reload is promised. MLflow auth
  bootstrap passwords do not rotate an existing auth database.
- The local writer is a bootstrap admin; infrastructure connection identities
  are development identities, not enterprise least-privilege qualification.
- Namespace NetworkPolicy needs an enforcing CNI. The tested default kindnet
  does not establish that the policy is enforced. Formal positive/negative
  network tests belong to company-cluster acceptance.
- `Recreate`/one replica and RWO hostpath do not prove physical execution death,
  exactly-once work, HA, cross-node storage correctness or backup recoverability.
- Databricks is disabled; no company endpoint or data is contacted.

Retain the namespace/PVCs after verification for review. Cleanup requires an
explicit decision about local data; this helper provides no automatic deletion.

Next packages: Storage Contracts → Serving Runtime → Training Runtime → K8s
Acceptance → Production Operations. Only the last package's actual company
environment evidence can close production gates. The six packages preserve
model/data/governance contracts and keep SDKs outside Domain/Application.
