# Storage Contracts — Validation Record

Date: 2026-11-17 (Asia/Taipei)

Result: **STORAGE_CONTRACTS_PASSED on Minikube hostpath**.
Baseline: master `f2040edd7151453a0fba547c455159f2657b07d0` after PR #7.

## Changes and preserved behavior

Keep the `ml-runtime` claim and existing absolute RawSnapshot/Dataset/execution
paths. Mount artifact caches separately as bounded per-Pod emptyDir volumes and
declare ephemeral-storage resources. No existing runtime PVC or Foundation
deployment was modified by this qualification.

Five source files receive I/O-only corrections: sync EBM snapshot directories,
sync receipt/raw-publication directories on idempotent retry, and sync checkpoint
or JSON bytes plus their replacement directory entry. The trainer algorithms,
seeds, callback order, artifact/checkpoint formats, dataset transformations,
registry, approval and SQL ownership/route fencing remain unchanged. No schema,
credential, dependency or Compose changes are introduced.

## Executed verification

- Real acceptance image, **without a source bind mount**, read-only root:
  **140 passed, zero skipped** across RawSnapshot publication, DatasetBuilder,
  EBM datasets/training runtime, checkpoint resume and K8s contracts.
- New storage fault/configuration cases: 25, included in that regression scope.
- Strict EBM typing: 38 source files passed.
- Ingestion lint/format: passed; strict typing: 36 source files passed.
- Scoped Ruff, dependency boundaries, rendered Foundation contracts (40 resources)
  and diff whitespace checks: passed.
- A separate 1 GiB RWO PVC in namespace `ml-k8s-storage-contracts`, StorageClass
  `standard`, provisioner `k8s.io/minikube-hostpath`: all 23 recorded checks passed.
- The writer reached its explicit post-rename cut point and was deleted through
  normal kubelet-confirmed Pod deletion. A distinct recovery Pod reconciled the
  same persisted intent. No Pod force deletion/finalizer removal was used.
- A third, distinct Pod mounted the data PVC read-only and successfully verified
  immutable raw/dataset/receipt bytes, resumed real MLP and EBM checkpoints to the
  same weights as uninterrupted training, and rebuilt verified cache fixtures.
- Injected fsync/ENOSPC errors fail closed; pre-rename checkpoint failure retains
  the previous checkpoint. Process SIGKILL before/after raw/dataset rename,
  receipt linking and progress/result replacement never exposes partial content.

The manifest-pinned image, actual PVC/PV metadata, Pod UIDs, node placement and
checks are in the sanitized [machine-readable result](../evidence/k8s-storage-contracts-2026-11-17/result.json).
The completed reader and test PVC remain for review. Existing Foundation
resources and unrelated untracked workspace drafts are preserved.

## Interpretation and remaining qualification

The backend was local hostpath and all Pods ran on node `minikube`.
`cross_node_qualified`, `power_loss_qualified` and `production_qualified` are
explicitly false. Process termination and sequential Pod remount are not proof
of persistence through node/device/storage-server power failure or concurrent
multi-node operation. Filesystem calls alone cannot certify underlying devices.

Cache rebuilding uses synthetic durable bundle fixtures and real digest
verification. It does not certify the MLflow artifact proxy or enterprise
credentials. Direct-trainer resume tests do not certify Supervisor ownership,
confirmed termination during a network partition or automatic training recovery.

Repeat the same staged runner against an approved CSI/StorageClass and actual
distinct-node placement, then add site-specific node/storage faults and
backup/restore evidence in Production Operations. Serving and Training runtime
recovery remain in their dedicated PRs. See [ADR-018](../adr/018-kubernetes-storage-contracts.md)
and the [runbook](../deployment/kubernetes-storage-contracts.md).
