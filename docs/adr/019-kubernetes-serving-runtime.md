# ADR-019: Kubernetes Serving Runtime

Date: 2026-11-17
Status: Proposed for review; Minikube qualification is not production approval.
Baseline: master `3150c041c7ec3f82036cbc9d0d08e7cb09e2d1b1` after PR #8.

## Decision

Keep DeploymentService, tenant/permission checks, exact Evaluation/Approval binding,
SQL generation allocation, two reserved runtime slots and compare-and-swap route
activation. Keep MLP/EBM trainers, features, seeds, quality decisions, artifact
formats, Training ownership and Outbox unchanged. No SQL migration is needed.

Add a Kubernetes DeploymentExecutor and an independent endpoint resolver behind
the existing application boundary. The default remains `local`; the opt-in
`minikube-serving` overlay selects Kubernetes. Infrastructure depends only on
Domain; bootstrap selects adapters. Kubernetes APIs never enter Domain/Application.

The SQL deployment spec additionally freezes `runtime_code_revision`. Its existing
canonical JSON digest covers that field and the qualified image digest. Serving
checks both before materializing the exact approved bundle. Native recovery of
legacy specs without pinned source is rejected; switch from local Serving through
an explicit approved rollout, rather than rewriting old deployment records.

## Resource identity and address resolution

Each platform deployment UUID maps to `ml-serve-<UUID without hyphens>` for a native
Deployment, immutable spec ConfigMap and ClusterIP Service. The Service DNS is
`http://<name>.<configured namespace>.svc:8001`. No Pod IP, host process port,
request-supplied address or shared `instance.json` is needed by the K8s Gateway.
Local development retains its process adapter and loopback-only file resolver.

The native Deployment uses one replica and Recreate. Its selectors contain the
platform UUID and a bounded digest label; a full digest annotation and immutable
ConfigMap retain the complete identity. ConfigMap and Service have a controller
ownerReference to the native Deployment UID. Existing resources must match the
approved declaration; the adapter never patches/adopts foreign or modified specs.
Native object UIDs, resource versions and Kubernetes generations do not allocate
platform generations. A new native UID after recovery is expected.

The API adapter uses verified in-cluster HTTPS, bounded calls, no redirects and a
projected token re-read per request. Namespace and image repository are trusted,
validated deployment configuration. Native image references use the SQL spec's
image digest, and source/image environment overrides also come from that spec.

## Recovery, activation and retirement

The controller includes ACTIVE rows. If SQL still routes to the same deployment
and generation, reconciliation recreates missing physical resources and observes
readiness. It does not call activate, increment generation/revision, create a new
approval or release capacity. Superseded ACTIVE snapshots are ignored. Kubernetes
recreates a deleted Pod; SQL ACTIVE reconciliation also restores a deleted native
Deployment and its dependent resources. READY/ACTIVATING/RECONCILING phases can
re-prepare missing immutable resources before attempting the existing activation.

A missing native Deployment with lingering managed Pods is uncertain: wait rather
than launch another incarnation. Candidate startup has a native progress deadline;
failed candidates use the existing SQL route fence before retirement. ACTIVE
failures preserve route authority while recovery is pending. Kubernetes API
outages defer reconciliation; they are never treated as confirmed physical stop.

Readiness checks the spec/model/deployment identity, tenant and responding Pod UID.
The controller verifies that Ready Pod belongs to a ReplicaSet owned by the current
native Deployment UID. Gateway checks health and every successful prediction's
provenance against SQL, checks that the Pod UID did not change between health and
prediction, then re-reads SQL's route revision/id/generation. Mismatch, redirect or
an in-flight route change returns 503. Existing auth, input errors, request limits
and model-specific prediction schemas remain in force. Provenance gains additive
`manifest_digest`, `spec_digest` and `pod_uid` fields.

Replacement and rollback still create new platform deployments. Retirement uses
UID-preconditioned foreground deletion and retains the SQL reservation while the
native Deployment, managed Pods, Service or ConfigMap remain. API uncertainty keeps
the reservation. Force deletion, node partition, physical fencing and a dishonest
or unavailable kubelet are not qualified by API-object observation on Minikube.

## Secrets, storage and authority

Only the Control container receives a projected Kubernetes token/CA; Gateway and
Serving do not. The namespaced Role permits get/create/delete of Deployments,
Services and ConfigMaps, get/list of Pods and get of ReplicaSets. It cannot read
Secrets or delete Pods. Creating workloads can indirectly mount namespace Secrets:
Control remains a trusted platform principal, not a tenant sandbox. Admission policy
and dedicated production namespaces require site review.

Serving has its own unprivileged ServiceAccount, no API token, non-root UID/GID,
read-only root, dropped capabilities, resource limits and a bounded termination
grace. Its private role JSON contains Mongo/JWT identity and only the MLflow reader
principal, never SQL, RabbitMQ, Redis or writer credentials. Init copies projected
Secret/CA files to owned private regular files using the Foundation bootstrap.
Serving keeps its existing Mongo session checks; read-only MLflow credentials are
mapped to standard SDK variables only inside the serving process environment.

Model cache, home and temporary state use bounded Pod-local emptyDir volumes;
Serving needs no shared runtime PVC. A new Pod re-downloads and re-verifies the
bundle. The global Foundation ConfigMap and external CA/role Secrets remain
operational dependencies; coordinate rotation/recreation rather than treating this
PR as a general live configuration upgrade mechanism. In-cluster HTTP and CNI
NetworkPolicy enforcement are not mTLS or production network certification.

## Qualification boundary

See the [runbook](../deployment/kubernetes-serving-runtime.md) and executed review
in `docs/reviews/`. The isolated `ml-k8s-serving-runtime` namespace prevents changes
to Foundation/Storage data. Qualification prepares real governed model fixtures in
one explicit operator Pod with queue consumers and Control paused; it then runs
native Serving recovery/update/rollback. That fixture step does not qualify a
cross-Pod Training Supervisor, which remains the next engineering PR.
