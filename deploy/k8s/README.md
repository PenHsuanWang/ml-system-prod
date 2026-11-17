# Kubernetes foundation

This is the first of six migration work packages, for fresh one-node Minikube
installations. It does not implement ACTIVE-serving or cross-Pod training recovery.

- `base/`: application roles, migration, resource/security settings and runtime PVC.
- `infrastructure/minikube/`: local-only persistent dependencies and MLflow TLS.
- `overlays/minikube/`: public composition; intentionally contains release placeholders.
- `examples/`: public input schemas only. Real inputs and generated Secrets stay in ignored `var/`.

See [ADR-017](../../docs/adr/017-kubernetes-runtime-boundaries.md) and the
[step-by-step runbook](../../docs/deployment/kubernetes-staging-runbook.md).
Do not apply the public overlay directly: prepare a private digest-pinned overlay.
Do not point the Minikube helper at a company/production cluster.

Storage Contracts adds `storage-contracts/`, a separate synthetic qualification
namespace/PVC with staged writer, recovery and read-only reader Pods. Follow the
[storage runbook](../../docs/deployment/kubernetes-storage-contracts.md); do not
apply the three Pods simultaneously. Existing durable application paths remain
on `ml-runtime`, while artifact caches use bounded per-Pod `emptyDir` volumes.
