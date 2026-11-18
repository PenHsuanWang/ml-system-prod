# ADR-018: Kubernetes Storage Contracts

Date: 2026-11-17

Status: Implemented and merged (PR #8); local qualification does not approve a production CSI.
Baseline: master `f2040edd7151453a0fba547c455159f2657b07d0` after PR #7.

## Decision and preserved contracts

Keep the absolute RawSnapshot, Dataset, DatasetBuilder and execution paths and
the existing `ml-runtime` PVC. Do not automatically migrate existing data, change
SQL snapshot references, introduce object-store pointers, or redesign storage
adapters. Training algorithms, seeds, metrics, resume data, feature transforms,
artifact schemas, approval, Outbox and ownership/route fencing remain unchanged.

The application mount map is:

| Path | Storage | Consumers / ownership |
| --- | --- | --- |
| `/app/var/raw-snapshots` | Existing runtime PVC | Ingestion publishes; Management/operator resolves approved snapshots. |
| `/app/var/datasets` | Existing runtime PVC | Dataset catalog/publisher writes; Supervisor/Control read approved versions. |
| `/app/var/executions` | Existing runtime PVC | Launch evidence, checkpoints, progress/results and per-role homes; existing runtime owners retained. |
| `/app/var/executions/dataset-builder` | Existing runtime PVC | Verified build artifacts and immutable receipts. |
| `/app/var/artifacts` | Per-Pod disk-backed `emptyDir`, 1 GiB | Disposable MLflow artifact download cache, verified again on materialization. |
| `/run/private` | Existing memory-backed role volume | Private role/CA copies; unchanged bootstrap ownership/mode checks. |
| `/tmp` | Existing per-Pod `emptyDir` | Disposable temporary work. |

The parent `/app/var` mount remains writable for existing role initialization.
This PR defines data persistence and cache separation; it does not claim
role-level filesystem isolation on the shared parent. Fine-grained read-only
application mounts need a separately reviewed ownership/bootstrap design.
Qualification's final reader mounts the **entire data PVC read-only**, with a
separate writable cache, and proves the same stored references remain readable.

The new cache mount hides any old PVC `artifacts/` directory without deleting it.
The production profile uses MLflow `runs:/` artifact references; persistent model
bundles remain in MLflow/object storage and training execution directories. No
existing PVC data or referenced Dataset/Checkpoint directory is moved/deleted.
Application containers declare ephemeral-storage requests/limits as well as the
cache size limit. The single-node RWO restriction remains; RWO is not RWOP or RWX.

## POSIX publication and synchronization

Raw snapshots keep Linux `renameat2(RENAME_NOREPLACE)`, exact-byte validation,
file/directory fsync and immutable identity. Retries of already-visible snapshots
must also sync the publication parent before acknowledging success. A sync error
continues to require reconciliation; directory visibility alone is insufficient.

EBM snapshots sync their staging directory before rename and their publication
parent afterward, including idempotent retries. DatasetBuilder receipts retain
hard-link publication and validate collisions, then sync the receipt directory
on both initial publication and a retry of an existing receipt.

MLP/EBM checkpoint replacement and atomic progress/result JSON use file fsync,
atomic replacement, then parent-directory fsync. These are I/O-only changes:
the callback ordering of each trainer, serialized content and model computation
remain unchanged. No new checkpoint format or automatic training relaunch is
introduced. Errors fail closed instead of reporting synchronized completion.

These operations are required capabilities, not proof of backend power-loss
durability. Root provisioning, device/controller cache policy, remote filesystem
server behavior and real node/storage failures still require site qualification.

## Qualification design

Use an isolated `ml-k8s-storage-contracts` namespace and a **new synthetic-data
PVC**, not `ml-runtime` or any production volume. The runner refuses an existing
namespace or evidence directory. Pods run as UID/GID 10001 with read-only roots,
dropped capabilities, no API token/credentials and no restart/retry policy.
An independent network policy declares no ingress/egress; filesystem tests need
no network or database service. Its enforcement still depends on the CNI.

1. A writer exercises actual RawSnapshot/Dataset/receipt/checkpoint code on the
   mounted filesystem, including concurrent independent publisher processes,
   SIGKILL cut points before/after rename/link, injected sync/ENOSPC errors and
   integrity rejection. Checkpoint tests use real MLP and EBM trainers.
2. The writer persists its synthetic publication intent and pauses immediately
   after another raw rename. The operator runner normally deletes this test
   Pod with one-second grace, waits for kubelet-confirmed deletion and preserves
   its UID. No `--force` or finalizer removal is used.
3. A new recovery Pod reconciles the **same intent**, syncs and verifies it.
4. A third Pod remounts the PVC read-only, verifies raw/dataset/receipt digests,
   resumes both checkpoints to the same weights as uninterrupted training and
   rebuilds verified artifact caches from synthetic durable bundle fixtures.

The cache test does not contact MLflow or replace formal artifact-proxy and
writer/reader authorization qualification. No real job, tenant, approval or
source registration is created. Synthetic direct-trainer execution is not
evidence of Supervisor ownership/recovery (the Training Runtime work package).

Record the actual manifest image digest, PVC/StorageClass, PV UID/CSI driver,
writer/recovery/reader Pod UIDs, actual node placement, checks and scope limits.
A different reader-node selector may be requested for an approved RWX/CSI
environment. `cross_node_qualified` is false unless distinct nodes were actually
observed. This sequential cross-node read/recovery test does not certify
concurrent multi-node writers or storage failover.

Unsupported rename/hard-link/fsync, failed Pods, missing evidence, changed hashes,
unknown subprocess state or a timeout all fail qualification. Preserve failures;
do not silently substitute copy/delete, skip cases or relax access modes.

## Acceptance and remaining gates

Minikube hostpath tests establish only the tested local backend/process/Pod
behavior. Power-loss, node failure, storage-server interruption, true multi-node
concurrent publication, CSI backup/restore and production durability stay pending
for Production Operations. Serving ACTIVE recovery and Pod-aware job ownership
remain in their dedicated PRs. No production readiness follows from this PR.

See [the storage runbook](../deployment/kubernetes-storage-contracts.md).

References:
- [Kubernetes volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)
- [Kubernetes emptyDir lifecycle](https://kubernetes.io/docs/concepts/storage/volumes/#emptydir)
