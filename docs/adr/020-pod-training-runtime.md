# ADR-020: Pod-aware Training Runtime

Date: 2026-11-18
Status: Proposed for review; local runtime qualification is not production approval.
Baseline: master `a5c3847f06deba4c4822556e98b8151b82453dff`, after PR #9.

## Decision and preserved contracts

Keep the approved MLP/EBM handlers, trainers, features, losses, Dataset rules,
checkpoint/bundle formats, Evaluation, Approval, Outbox and SQL owner-token fencing.
No SQL migration, dependency or new workload is required. Keep the local adapter
as default. The opt-in `minikube-training` overlay extends native Serving with a
Pod-aware child-process adapter; this PR does not introduce a Kubernetes Job per
training command, HPA or arbitrary model code.

## Physical identity and capacity

An execution freezes its UUID, attempt, original launch token, complete envelope
digest, namespace, Pod name/UID, actual container ID, node, supervisor instance and
local process identity. SQL records the execution reference before dispatch.
Durable dispatch and guardian gates precede fork/exec; uncertain dispatch is never
repeated. A foreign/missing Pod or changed container is UNKNOWN, not STOPPED.
Control never inspects or signals a PID from another Pod.

A credential-free guardian wraps the unchanged compute entrypoint. It checks the
complete launch envelope, owns the compute process group, enforces an independent
absolute wall deadline, accepts scoped durable stop requests, and waits for the
entire group to disappear before atomically publishing a matching stop receipt.
SIGTERM escalates to SIGKILL within the configured grace; unconfirmed stop produces
no positive receipt. The guardian continues after a Taskiq child-process crash.

The shared runtime PVC retains a physical training-slot intent. A short POSIX
flock protects slot admission, while the durable intent survives guardian death.
Only an exact confirmed stop receipt permits the next fit. Worker redelivery waits
unacknowledged for that capacity; a SQL lease, Pod deletion, container restart or
mutex release cannot free it. This protects against a second fit while an old
compute group is uncertain. Real CSI/multi-node lock semantics still require site
qualification; single-node Minikube is not storage or node-fencing certification.

## Worker and Control ownership

Worker alone claims new commands and launches compute. It heartbeats its SQL claim
and monitors its own execution. Only Control recovers stale claims in the Pod
profile. Existing SQL recovery rotates the owner token; the old owner cannot
commit business transitions. Control retains that claim across bounded observations
instead of repeatedly consuming the recovery budget. It may observe/finalize the
same fit, request cancellation or enforce its deadline; it cannot relaunch it.

Control reconciliation is incremental, so a remote fit does not hold the Serving
loop for its whole training duration. External postprocessing errors retain the
same execution/evidence for recovery. Missing identity, unavailable API, missing
receipt or an exhausted recovery budget remains reconciliation work, never an
implicit success, confirmed failure or automatic refit.

## Cancellation, deadline and drain

Cancellation remains the existing authorized SQL intent. The active owner requests
physical stop and commits CANCELLED only after confirmation. Independent guardian
wall deadlines still apply after Worker loss; the owner records TRAINING_DEADLINE
from the physical receipt. Launch exceptions also require positive stop evidence.

In the Pod profile, the Worker wraps Taskiq's installed SIGINT/SIGTERM handlers:
Taskiq stops admission while a thread-safe drain flag requests compute stop. A
confirmed drained fit is FAILED with SUPERVISOR_DRAIN (or CANCELLED if cancellation
won). There is no implicit retry; the existing explicit retry creates its own job.
An uncertain or technically unacknowledgeable delivery stays unacked and is
redelivered after channel closure. The overlay declares task-drain/shutdown budgets
inside the existing Pod termination grace. Local/Compose execution remains available.

## Manual acknowledgement

Before ACK, validate the exact command/schema/attempt/spec against the durable SQL
Outbox intent and require a terminal Job. Persist a bounded Redis technical
snapshot containing command, job, attempt, spec digest, terminal state and revision;
only then call transport ACK. Redis errors/negative writes do not authorize ACK.
Redis is not business truth. A terminal redelivery reconstructs its snapshot and
ACK without fitting again. No generic Taskiq error/result path may ACK this task.

## Permissions and limits

Only Supervisor and Control mount projected Kubernetes API credentials. The new
namespaced Role permits get on Pods; it grants no exec, Pod deletion, Secret read
or cluster-wide permission. The existing Serving controller permissions remain
separate. Gateway, Serving and compute environments receive no new credential
variables. Compute is trusted static code in the Supervisor container, not a
filesystem isolation boundary from its parent role.

Qualification uses an independent Minikube namespace and real RabbitMQ, Redis,
PostgreSQL and MLflow. It must exercise real fitting, cancellation, deadline,
Worker loss/reconciliation, normal Pod drain, redelivery and Redis ACK gating.
Node partition/force deletion, multi-node CSI/flock, enterprise MLflow permissions,
production resource/SLO/restore and arbitrary workload admission remain unqualified.
Full governed K8s acceptance and Production Operations remain the next two PRs.
