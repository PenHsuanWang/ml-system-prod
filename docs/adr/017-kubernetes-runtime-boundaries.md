# ADR-017: Kubernetes Foundation and Runtime Boundaries

Date: 2026-11-17

Status: Proposed for review; foundation implementation only, production acceptance pending.

## Context

The baseline is master `ce81327fd8bf2908dc0d3dec2c0efb67e04b1612`.
Containerization and SQL contracts already exist. Local serving addresses,
ACTIVE-runtime recovery and host/PID-based training observations still prevent
claiming Kubernetes production readiness. Minikube is available for local tests.

## Decision

Use Git-managed Kustomize declarations under `deploy/k8s`, reviewed feature
branches and atomic documentation, implementation and validation commits.
Each subsequent work package starts from the latest merged master. Open a Draft
PR for human review; do not merge or enable automatic merging.

The reusable application base requires operator-provided private role JSON, CA
and immutable release images. A separate **local-only** Minikube overlay adds
PostgreSQL, RabbitMQ, Redis, MongoDB, MLflow and source-built MinIO. The deployment
helper always selects context `minikube` and namespace `ml-k8s-foundation`.
It refuses to initialize an existing namespace. It never deletes a namespace,
PVC, old execution or publication, and is not an upgrade/rollback controller.

### Runtime topology

Management, relay, supervisor and ingestion each have one Pod. Control and
Gateway share one Pod/network and `/app/var`, preserving loopback serving.
They do not share a PID namespace. Training remains a child of the separate
supervisor; the controller's cross-Pod recovery limitation remains explicit.
Singleton Deployments use `Recreate`, with no HPA. These settings do not prove
that an unreachable old process is dead or provide exactly-once physical work.
The two global serving reservations remain unchanged; an active model must leave
capacity for its rollout. CPU-only, no accepted real training/deployments and no
external ingress are the foundation smoke-test envelope.

### Configuration and trust

Application containers run as UID/GID 10001, drop all capabilities, use
RuntimeDefault seccomp, forbid privilege escalation and have read-only root
filesystems. Writable paths are explicit volumes. Each role has a separate
Secret and memory-backed private directory. A non-root init container reads the
projected Secret/CA and writes regular owned `0400` files; only the init container
mounts the projected role Secret. It runs the existing production configuration
validation. The production entrypoint and its no-symlink, ownership, role
allowlist, HTTPS and release checks remain unchanged.

ServiceAccounts have token automount disabled and receive **no API permissions**.
Control/Gateway share the control ServiceAccount but mount separate role files.
Future Kubernetes adapters must introduce narrowly scoped RBAC in their own PR.
A namespace NetworkPolicy allows namespace-local traffic and cluster DNS only;
its enforcement requires a supporting CNI and is not certified by Minikube kindnet.
All Services are ClusterIP. Access for smoke checks uses explicit port-forward
or exec; JWT identity is retained and no user/admin account is seeded.

The local tool generates random credentials and a 30-day test CA below ignored
`var/`; neither secrets nor rendered private manifests enter Git. Images are
locked to repository **manifest** digests, not Docker config IDs. Local MLflow
uses verified HTTPS via a TLS sidecar and its existing basic-auth application.
Its foundation writer is a local bootstrap administrator; this is not a
production least-privilege identity. A separate reader is provisioned for smoke
checks. Formal writer/reader/artifact qualification and enterprise identities
remain release gates. MLflow may be inside the cluster: HTTPS and authorization,
not physical location, define its trust boundary.

Init copies do not hot-reload projected Secrets or CA bundles. Rotation requires
coordinated upstream credentials and recreation of affected Pods, including
serving children; changing an initialization password does not update a database
on an existing PVC. The fresh-install helper deliberately refuses that operation.

### Initialization and storage

Apply common resources and local infrastructure, wait for actual infrastructure
readiness and successful MinIO initialization, run the existing Alembic migration
as a non-retrying Job, wait for Job completion, then start applications. Application
init containers also check the required SQL tables. No application startup runs
migrations. A migration failure preserves evidence and blocks application apply.
Do not replay migrations or downgrade schemas to force rollout success.

Application Pods share one RWO runtime PVC at the existing absolute `/app/var`
path; local infrastructure has individual PVCs. This topology targets one-node
Minikube, not a multi-node storage design. Do not treat successful hostpath tests
as CSI/RWX qualification. RawSnapshot rename-no-replace, DatasetBuilder hard
links, file/directory fsync and interruption tests belong to Storage Contracts.
Artifact caches may later use disposable volumes when backed by verified MLflow
artifacts. PostgreSQL application and MLflow metadata use different databases,
avoiding migration-version-table collisions. Stateful single-replica development
Deployments are not an HA or backup design.

## Six-PR acceptance boundaries

| Work package | Required outcome |
| --- | --- |
| Foundation (this PR) | Declarative local installation, private-file/CA compatibility, successful migration and idle-role startup. |
| Storage Contracts | Atomic publication, hard links, fsync, collisions and interrupted publication; repeat against the real CSI in Production Operations. |
| Serving Runtime | ACTIVE physical recovery, resolver injection, identity/digest readiness, update/rollback without bypassing SQL route/approval fencing. |
| Training Runtime | Pod-aware identity, explicit Worker/Control ownership, confirmed stop versus UNKNOWN, cancellation/deadline/drain/manual ACK behavior. |
| K8s Acceptance | Complete governed ML lifecycle and fault tests; preserve Compose regressions. |
| Production Operations | Company cluster, CSI, multi-node recovery, least privilege, TLS/MLflow qualification, backup/restore, capacity and alerting evidence. |

Preserve trainers, feature/loss/evaluation logic, Dataset transformation rules,
Workload Contract Registry, artifact formats, Outbox, idempotency, approval,
ownership fencing and route revisions. Runtime recovery may need small
Application coordination changes; Kubernetes SDKs remain Infrastructure-only.
Pod recreation is not a new approved model deployment generation.

Databricks is disabled in the local overlay. Live qualification applies when that
source is part of the deployed workload. HPA, dynamic cluster admission and
one-Job-per-training execution are deferred unless independently justified.

## References

- [Kustomize](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/)
- [Secret projection and updates](https://kubernetes.io/docs/concepts/configuration/secret/)
- [Process namespaces](https://kubernetes.io/docs/tasks/configure-pod-container/share-process-namespace/)
- [Force deletion and physical execution uncertainty](https://kubernetes.io/docs/tasks/run-application/force-delete-stateful-set-pod/)
- [Deployment runbook](../deployment/kubernetes-staging-runbook.md)
