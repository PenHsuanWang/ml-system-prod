# Storage Contracts — Qualification Runbook

This is work package 2 of [ADR-017](../adr/017-kubernetes-runtime-boundaries.md).
Read [ADR-018](../adr/018-kubernetes-storage-contracts.md) for the mount map,
publication/sync requirements and qualification limits.

## Local contracts and image packaging

Use Python 3.12, Docker, kubectl/Kustomize, Minikube and PyYAML. The image uses
the existing acceptance target so actual storage code, scripts, tests and public
K8s fixtures are bundled together. All training data is generated synthetically.

```sh
python scripts/check_k8s.py
python scripts/check_boundaries.py
docker build --target acceptance -f Dockerfile.ml -t ml-storage-contracts:local .
docker run --rm --read-only --tmpfs /tmp:rw,mode=1777,size=256m \
  --entrypoint python ml-storage-contracts:local \
  -m pytest tests/ml/k8s/test_storage_faults.py \
  tests/ml/k8s/test_storage_configuration.py --noconftest -q -p no:cacheprovider
```

These Linux tests exercise real child processes and filesystem operations, not
a mocked storage provider. Fault injection deliberately kills only test-owned
children and injects synchronization errors; it does not reproduce device or
node power failure. The existing checkpoint, dataset and raw-publication suites
remain regression requirements.

The lightweight Foundation CI checks configuration separately. The ML contracts
and full Backend suites execute the new Torch/process storage cases with their
normal dependencies; no cases are excluded from Backend zero-skip acceptance.

## Real PVC and Pod recreation

The runner creates namespace `ml-k8s-storage-contracts` and a new 1 GiB claim
named `storage-contracts`. It does not mount the Foundation `ml-runtime` PVC.
It refuses an existing namespace and preserves all artifacts on failure.

```sh
minikube -p minikube status
minikube -p minikube image load ml-storage-contracts:local
python scripts/verify_k8s_storage.py lock-image --local-tag ml-storage-contracts:local
storage_image=$(cat var/k8s-storage-image.txt)
python scripts/verify_k8s_storage.py run --context minikube \
  --image "$storage_image" --storage-class standard \
  --output var/k8s-storage-contracts
```

`lock-image` reads the containerd manifest descriptor and registers a digest alias
for local imports. It never substitutes a Docker config ID. The declaration's
unconfigured image is intentionally unusable; the runner requires a pinned one.

The runner applies namespace/security/PVC declarations, then the writer. This
order supports `WaitForFirstConsumer` StorageClasses: it does not wait for PVC
binding before scheduling the first consumer. Do not apply all three Pods at
once; the reader and recovery depend on the writer's exact persisted intent.

The writer runs the suite then prints `POD_CRASH_READY` after raw publication's
rename but before parent fsync. The runner waits for that marker, deletes the
test Pod normally, waits for confirmed deletion, starts a separate recovery Pod,
then starts a final read-only reader. It has bounded deadlines and does not
force-delete Pods or remove finalizers. The qualification image needs no service
credentials or Kubernetes API permissions; the operator needs permission to
manage the isolated resources and read PVC/PV metadata.

Success prints `STORAGE_CONTRACTS_PASSED` and saves `result.json`, the exact
rendered declarations and reader logs beneath the requested ignored output
directory. Save failed outputs too; use a new output directory after deliberate
cleanup and correction. No successful result file is produced for a failed case.

```sh
kubectl --context=minikube -n ml-k8s-storage-contracts get pods,pvc
kubectl --context=minikube -n ml-k8s-storage-contracts logs storage-reader
```

The writer and recovery Pods are removed only after their recorded boundary or
successful completion. The namespace, PVC and completed reader remain for review.
The runner has no automatic cleanup. Cleanup of this synthetic test volume must
be deliberate; never apply that cleanup to application/production PVCs.

## Company CSI handoff

Repeat the **same** suite with the approved pinned image, context and StorageClass.
The runner always uses the isolated qualification namespace, even when another
context is explicitly selected. No company cluster is contacted by the local
Minikube run. Example values below require operator review and replacement:

```sh
python scripts/verify_k8s_storage.py run --context APPROVED_STAGING_CONTEXT \
  --image registry.example.org/ml/storage-contracts@sha256:APPROVED_MANIFEST_DIGEST \
  --storage-class APPROVED_CSI_CLASS --access-mode ReadWriteMany \
  --reader-node APPROVED_DISTINCT_NODE --output var/csi-storage-attempt-001
```

Record actual node placement and CSI driver, not just requested access modes.
RWO may permit multiple same-node Pods; it does not qualify cross-node sharing.
An RWX request does not guarantee rename-no-replace, hard links, fsync or
visibility semantics. Do not change those contracts to accommodate a backend
that fails them; review a different backend or a separate storage-adapter design.

The final reader tests sequential remount/recovery. It does not test overlapping
publishers on different nodes, device persistence after power loss, controller
failover, snapshots, backup/restore or HA. Those need site-specific fault
injection and evidence in Production Operations. All reports retain explicit
`power_loss_qualified: false` and `production_qualified: false`.

## Application storage rollout

The base now mounts `/app/var/artifacts` as a per-Pod 1 GiB cache and declares
ephemeral-storage resources. Existing persistent paths and the `ml-runtime` claim
are unchanged. Old cached files on the PVC are hidden, not migrated or deleted.
Keep the existing release/Secret/CA configuration and production MLflow profile.
Before updating an existing application environment, inspect accepted/in-flight
work and coordinate its stop/recovery under the runtime work packages. This
qualification runner does not upgrade the Foundation namespace or certify
ACTIVE-serving/training recovery during a rollout.
