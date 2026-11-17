# Pod Training Runtime

This is Kubernetes work package 4, after Serving Runtime. ADR-020 defines the
ownership, physical-stop and manual-ACK contracts. The local adapter remains the
default. No Kubernetes Job per training command is introduced.

## Isolated Minikube setup

Build the real production image with the actual published source revision, load
it into Minikube and lock containerd manifest digests using the existing helper:

```sh
docker build --target production --build-arg ML_RELEASE_CODE_REVISION="$(git rev-parse HEAD)" \
  -t ml-k8s-training:qualified -f Dockerfile.ml .
minikube -p minikube image load ml-k8s-training:qualified
python scripts/k8s_foundation.py lock-images \
  --runtime-image ml-k8s-training:qualified --mlflow-image ml-k8s-mlflow:3.6.0 \
  --minio-image ml-system-minio-source:20261109 --mc-image ml-system-mc-source:20261109 \
  --output var/k8s-training-image-lock.json
python scripts/k8s_foundation.py prepare --training-runtime \
  --namespace ml-k8s-training-runtime --directory var/k8s-training-runtime \
  --image-lock var/k8s-training-image-lock.json --revision "$(git rev-parse HEAD)"
python scripts/verify_k8s_training.py --configure var/k8s-training-runtime
python scripts/k8s_foundation.py deploy --namespace ml-k8s-training-runtime \
  --directory var/k8s-training-runtime
python scripts/k8s_foundation.py verify --namespace ml-k8s-training-runtime \
  --directory var/k8s-training-runtime
python scripts/verify_k8s_training.py --output var/k8s-training-runtime/training-result.json
```

Other approved infrastructure images must already be loaded as in the Foundation
runbook. Public overlays contain external Secret references and image placeholders;
do not apply them directly. The helper refuses existing namespaces/output paths.
Private files and credentials remain below ignored var/ and never enter Git.

The configure step is restricted to this isolated namespace. It sets a 60-second
training timeout, 10-second queue budget and one-second cancellation grace for
bounded engineering fault tests; it does not alter the reusable production
configuration or EBM quality policy. EBM training/evaluation is verified without
granting Model Approval or claiming product-quality acceptance.

## Executed qualification

The operator provisions a scoped synthetic account and immutable dataset inputs.
It has no Kubernetes API token and a two-hour deadline. All training requests then
use Management HTTP, the real SQL Outbox/Relay, RabbitMQ and Taskiq Worker. The
operator never calls a trainer directly or edits SQL job/ownership/terminal states.

The runner exercises MLP and EBM training, technical snapshots and manual ACK;
pauses a compute group in its original Supervisor PID namespace to test cancel
and independent guardian deadline; kills the Taskiq child while its guardian and
same fit remain alive, waits for Control's fenced takeover and resumes that same
compute group; interrupts Redis and proves a terminal SQL result remains unacked
until Redis returns; then normally deletes a busy Supervisor Pod to test drain,
confirmed stop, redelivery and a replacement Pod UID. Fault signals validate
Pod/PID/start-tick identity and are restricted to the disposable namespace.

The receipt contains allowlisted job/attempt/execution/Pod identities, matching
stop confirmations, terminal error codes and Redis/queue counts. It excludes
passwords, tokens, owner tokens, source rows and private connection strings.
Retain failed receipts; the runner refuses to overwrite them. Inspect partial
runs before another attempt rather than replaying a paused or uncertain fit.

The namespace, PVCs, role services, scoped account, completed artifacts and bounded
operator remain for inspection. There is no automatic namespace/PVC deletion.
Earlier Foundation, Storage and Serving namespaces are not changed. Runtime cleanup
requires an explicit decision about the retained local data.

## Operational rules

Only Worker launches new fits. Only Control takes stale SQL ownership in the Pod
profile. A Worker crash/redelivery may observe an existing fit; it must not relaunch
it. Control is a remote observer/finalizer and cannot access another Pod's PID.
The guardian is trusted static compute supervision, not another business owner.

The physical training-slot intent outlives process/Pod/SQL lease uncertainty. Do
not remove `active-training.json`, edit SQL owner tokens, forge stopped receipts
or force-delete resources to free it. Missing or contradictory identity/receipt,
API failure or lost dispatch remains NEEDS_RECONCILIATION and unacked. Recovery
budget exhaustion does not prove death. Node/CSI failure needs site-specific
physical fencing and an authorized operational recovery procedure.

Cancel, deadline and drain commit terminal state only after physical confirmation.
A drained fit fails with SUPERVISOR_DRAIN unless user cancellation won; retry uses
the existing explicit retry API. SQL terminal state plus exact intent validation
precedes Redis evidence, which precedes transport ACK. Redis failure may hold the
next delivery; it never revokes an already committed SQL result or authorizes refit.

The overlay adds only get-Pod API permission to Supervisor/Control. It grants no
Secret read, Pod exec/delete or cluster-wide rights. Taskiq stops admission on its
normal signals and receives explicit task-drain/shutdown budgets inside the Pod
grace. Trusted compute shares the Supervisor container filesystem; filtering child
environment variables is not a tenant sandbox.

## Validation and remaining gates

Use scripts/check_training_runtime.py, dependency boundaries and strict Domain/
Application typing. Existing local/Compose and real-service Backend tests remain
required; no skips or quality checks are disabled to accept this runtime.

Single-node Minikube cannot qualify node partition/force deletion, cross-node
storage/locks, production CSI, enterprise MLflow authorization, HA, arbitrary
workloads, production capacity/SLO/restore or network-policy enforcement. The next
PR performs full governed K8s acceptance/fault coverage; Production Operations
then qualifies the actual company environment. Serving's approvals, platform
Generation, route fencing and two global Serving reservations remain unchanged.
