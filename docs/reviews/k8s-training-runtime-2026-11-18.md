# Pod Training Runtime qualification — 2026-11-18

Baseline: master `a5c3847` after merged PR #9.
Feature: `feature/k8s-training-runtime`, Draft PR #10.

## Result and source

**POD_TRAINING_RUNTIME_PASSED**: all seven real Minikube cases passed in one
complete run on source `9c422b7fa89c8571da928ca9deb914c86d0b5cda` and its actual
containerd manifest digest. Production, cross-node and node-partition qualification
remain false. See the [sanitized receipt](../evidence/k8s-training-runtime-2026-11-18/result.json).

The final source keeps trainers, feature/loss/evaluation algorithms, dataset and
artifact formats, Workload Contract Registry, Approval/Serving generations and
SQL ownership/route fencing unchanged. It adds Pod-aware runtime coordination,
a guardian and exact intent/terminal/technical evidence before manual ACK. There
is no SQL migration, new dependency or Kubernetes Job per training invocation.

## Executed evidence

| Case | Actual result |
| --- | --- |
| MLP | Management HTTP → SQL Outbox/Relay → real RabbitMQ/Taskiq → compute group → MLflow registration/evaluation → SUCCEEDED; Redis snapshot and manual ACK follow. |
| EBM | Same real runtime chain succeeds with the existing neural workload and synthetic dataset; no policy threshold was weakened and no Model Approval was granted. |
| Cancel | Original compute group was paused in its own PID namespace, authorized HTTP cancellation requested stop, and a matching physical receipt preceded CANCELLED and ACK. |
| Deadline | Guardian's wall deadline killed paused compute independently of CPU time; matching stop evidence preceded FAILED/TRAINING_DEADLINE and ACK. |
| Worker crash | Taskiq child was SIGKILLed while the same guardian/compute group remained alive. SQL owner actually changed to Control; resuming the original process produced SUCCEEDED with the same attempt, execution UUID, Pod UID and one fit. Terminal redelivery then ACKed. |
| Redis outage | Real Redis Deployment was stopped. Training still committed SUCCEEDED in SQL while RabbitMQ retained one unacked delivery. Restoring Redis produced the snapshot and ACK without refitting. |
| Pod drain | A paused busy Supervisor Pod was normally deleted. Its Taskiq signal/drain path confirmed compute stop and FAILED/SUPERVISOR_DRAIN. A new Pod UID appeared, terminal redelivery ACKed and the queue drained. |

Both healthy MLP/EBM cases retained their original SQL owner. Only the intentional
Worker-crash case changed owner. All seven jobs have one attempt and an exact
confirmed physical-stop receipt. Final queue counts are ready=0/unacked=0.

The operator only creates the scoped synthetic account/dataset inputs and makes
Management requests. It does not invoke trainers directly or edit SQL state,
owner tokens, terminal states or ACK evidence. Fault injection checks the original
Pod/PID/start-tick identity before signalling. No force deletion, removed finalizer,
forged stop receipt or automatic retry was used. Older failed/partial receipts are
retained privately and are not substituted for the final complete run.

## Issues found and corrected

- Concurrent Worker/Control initialization exposed MLflow's get-then-create race.
  Only RESOURCE_ALREADY_EXISTS triggers a lookup of the named winner; auth and
  unrelated failures still propagate. Positive/negative SDK tests cover this.
- A launch already beyond its deadline must report DEADLINE even when compute
  never started; it is not a generic stop/failure. A dedicated no-compute case
  and the real paused-compute deadline verify both boundaries.
- Slow registration/artifact/evaluation I/O must renew SQL ownership. A background
  renewal spans postprocessing, every SQL transition remains fenced, and final
  completion checks the deadline/drain state. A deterministic clock regression
  verifies that a healthy finalizer is not reclaimed as stale.
- Observing an original local guardian uses the Worker PID namespace, avoiding an
  API call every 50 ms. Remote Control still uses Pod/container identity and never
  signals a remote PID. API loss does not invalidate the Worker's own physical
  stop proof; remote missing/changed identities remain UNKNOWN.
- The test broker's management port is not exposed by its AMQP-only Service.
  The probe now targets the actual private broker Pod address with its scoped
  credentials; no public service/ingress was added.
- Recovery may legitimately return STARTING to RUNNING immediately. The probe
  therefore verifies actual SQL owner change without exposing the token, rather
  than depending on a transient NEEDS_RECONCILIATION status.

## Checks

- Actual packaged image, read-only root and no source bind mount: **85 passing
  Training/ownership/Kubernetes cases, zero skipped**.
- Full Backend CI on the frozen implementation source: **1146 passed, zero
  failures/errors/skips**, no acceptance blockers, including real EBM lifecycle.
- SQLite/PostgreSQL contracts, production image, ingestion and Foundation CI
  passed. Protected live Databricks remains a separate conditional qualification.
- Scoped Ruff/format, dependency boundaries, Pyflakes and strict Domain/Application
  typing passed. The original local/Compose execution adapter remains available.
- Supervisor/Control may get Pods, but cannot get Secrets, exec into Pods or delete
  Pods. Gateway receives no API token. Real container IDs and Pod/node identities
  are recorded; the operator has no API token and a bounded deadline.

CI: [ML #74](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44137008682)
and [Foundation #10](https://github.com/PenHsuanWang/ml-system-prod/actions/runs/44137008364).
The PR description records checks on the final documentation/evidence head.

## Retained environment and remaining gates

The isolated `ml-k8s-training-runtime` namespace retains infrastructure, PVCs,
normal application roles, bounded operator, synthetic inputs and job/transport
receipts for inspection. Prototype image updates drained idle application roles
normally and changed only their image/public source pins. Infrastructure,
credentials, schema and data were preserved. Earlier Foundation/Storage/Serving
namespaces and unrelated untracked drafts were not modified.

Single-node Minikube cannot certify CSI/flock semantics across nodes, physical
fencing after node partition/force deletion, production capacity/SLO/restore,
enterprise identities/MLflow permissions, network policy enforcement or arbitrary
workload isolation. Missing/contradictory physical evidence remains uncertain and
unacked; no SQL lease or missing Pod frees the durable physical training slot.

The next work package is complete governed **K8s Acceptance**, followed by actual
company-environment **Production Operations**. This runtime qualification grants
neither enterprise-data/model-quality approval nor production acceptance. See
[ADR-020](../adr/020-pod-training-runtime.md) and the
[runbook](../deployment/kubernetes-training-runtime.md).
