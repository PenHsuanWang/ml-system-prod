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
