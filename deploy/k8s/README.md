# Kubernetes deployment profiles

Foundation, Storage Contracts, Serving Runtime, Training Runtime and governed
Acceptance are implemented. Start with [implementation and qualification status](../../docs/implementation-status.md)
for the executed source/image evidence and the remaining Production Operations work.

For the C4 container/deployment views, Pod and Service inventory, storage, network,
RBAC and the dated local Minikube snapshot, read the
[Kubernetes architecture guide](../../docs/deployment/kubernetes-architecture.md).

| Declaration | Runtime / purpose | Procedure |
| --- | --- | --- |
| `base/`, `infrastructure/minikube/`, `overlays/minikube/` | Fresh one-node infrastructure, migration and idle application roles; local execution adapters | [Foundation](../../docs/deployment/kubernetes-staging-runbook.md) |
| `storage-contracts/` | Separate synthetic PVC qualification with staged writer, recovery and read-only reader Pods | [Storage Contracts](../../docs/deployment/kubernetes-storage-contracts.md) |
| `overlays/minikube-serving/` | Native fixed-version Serving, ACTIVE reconstruction and identity-checked routing; local Training | [Serving Runtime](../../docs/deployment/kubernetes-serving-runtime.md) |
| `overlays/minikube-training/` | Native Serving plus Pod-aware Training, guardian, Worker/Control ownership and manual ACK | [Training Runtime](../../docs/deployment/kubernetes-training-runtime.md) |
| `overlays/minikube-acceptance/` | Combined governed lifecycle and bounded synthetic fault qualification | [Acceptance](../../docs/deployment/kubernetes-acceptance.md) |
| `examples/` | Public input schemas with deliberately invalid release placeholders | [Configuration reference](../../docs/configuration-reference.md) |

The Foundation helper prepares each opt-in profile in its own fresh qualification
namespace; these are not sequential upgrades to a running namespace. Storage
qualification uses a separate PVC and does not apply its three Pods simultaneously.
Durable application paths remain on `ml-runtime`; artifact caches use bounded
per-Pod `emptyDir` volumes. Follow each runbook's storage and retention rules.

Do not apply public overlays directly. Build from a committed source, lock actual
image manifest digests and prepare a private overlay with role Secrets and CA.
Real inputs, credentials and generated resources stay under ignored `var/`; only
allowlisted sanitized receipts may be published. The helper targets the local
`minikube` context, not a production cluster or an existing installation.

[ADR-017–021](../../docs/adr/README.md) define the contracts. Single-node engineering
acceptance leaves production CSI, partitions, network-policy enforcement, enterprise
services, capacity/SLO and backup/restore qualification outstanding.

## Dataset Build Worker

Add `deploy/k8s/dataset-build` to the site's private overlay. Pin its runtime
image alongside the other roles. Apply migration 0009 before rollout. Provide
a role-specific `ml-runtime-dataset-build-worker` Secret with `runtime.json`
containing `ML_DATABASE_URL` and `APP_MONGO_URI`. Use the dedicated
[Mongo Identity lookup grant](../../docs/ml-runbook.md#worker-recovery-and-rollout).
The role uses the existing private configuration copy, pinned release and CA
contract. Its Pod has no service or Kubernetes API token.

Set the worker's roots consistently with Management and Ingestion. The sample
shares the existing `ml-runtime` PVC through `raw-snapshots` (read-only),
`datasets` and `executions` (writable) subpaths. Prepare them with ownership
and access appropriate for UID/GID 10001, and qualify the site's volume and
scheduling mode. Source publication must use the same absolute RawSnapshot path.

The disposable Minikube acceptance overlay provisions the narrow Mongo role
when its fresh Mongo volume initializes. The acceptance harness rolls the worker,
submits a build through Management, and reads the successful result and verified
lineage. An interrupted attempt can be reclaimed after lease expiration. Use the
[worker recovery runbook](../../docs/ml-runbook.md#worker-recovery-and-rollout)
to interpret deadlines and retry limits.
