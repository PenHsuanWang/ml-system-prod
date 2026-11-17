"""Destructive synthetic-data tests confined to a fresh qualification directory."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import multiprocessing as mp
import os
import signal
import stat
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from src.domain.ingestion.errors import PublicationUncertain, SnapshotConflict, SnapshotInvalid
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    IngestionExecution,
    PublicationIntent,
    RawSnapshot,
    SourceDefinition,
    StagedBatch,
)
from src.infrastructure.ingestion.storage import raw_snapshot_writer as raw
from src.plugins.training.pytorch_trainer import atomic_json

CONTRACT = "posix-ml-storage-v1"
CODE = "storage-contract-fixture-v1"
CONTEXT = mp.get_context("spawn")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class Control:
    def check(self) -> None:
        pass


def intent_for(root: Path, content: bytes = b'{"synthetic":1}\n', *, attempt="attempt-a"):
    store = raw.LocalRawSnapshotStore(root)
    limits = ExtractionLimits()
    source = SourceDefinition("source-a", "v1", "storage-contract", "file", "1", "{}", limits)
    extraction = BatchExtraction(
        source,
        IngestionExecution("storage-contract", "job-a", attempt, "snapshot-a", 1794873600.0),
        limits,
    )
    staged = store.stage(extraction, [content], Control())
    return store, store.prepare(extraction, staged)


def saved_intent(value: dict) -> PublicationIntent:
    return PublicationIntent(**{**value, "staged": StagedBatch(**value["staged"])})


def _pause(connection) -> None:
    connection.send("CUT_POINT")
    connection.recv()


def _raw_child(root, intent, connection, cut, barrier=None):
    store = raw.LocalRawSnapshotStore(Path(root))
    original = raw._rename_exclusive

    def rename(*arguments):
        if cut == "before":
            _pause(connection)
        original(*arguments)
        if cut == "after":
            _pause(connection)

    try:
        if barrier is not None:
            barrier.wait(timeout=20)
        with patch.object(raw, "_rename_exclusive", rename):
            snapshot = store.publish(intent)
        connection.send({"status": "published", "snapshot": asdict(snapshot)})
    except SnapshotConflict:
        connection.send({"status": "conflict"})
    except BaseException as error:
        connection.send({"status": "error", "type": type(error).__name__})


def receive(process, connection, *, kill=False):
    try:
        require(connection.poll(45), "Storage child did not reach its bounded observation point")
        result = connection.recv()
        if kill:
            require(result == "CUT_POINT", "Child did not reach the requested crash boundary")
            process.kill()
        process.join(10)
        require(not process.is_alive(), "Storage child did not terminate")
        require(process.exitcode == (-signal.SIGKILL if kill else 0), "Unexpected child exit")
        return result
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        connection.close()


def crash_raw(root: Path, cut: str) -> RawSnapshot:
    store, intent = intent_for(root)
    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(target=_raw_child, args=(str(root), intent, child, cut))
    process.start()
    receive(process, parent, kill=True)
    observed = store.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest)
    require((observed is None) == (cut == "before"), "Crash exposed an unexpected snapshot")
    recovered = store.publish(intent)
    require(store.verify(recovered) == recovered, "Interrupted publication did not recover")
    return recovered


def race_raw(root: Path, conflict: bool) -> RawSnapshot:
    store, first = intent_for(root)
    _, second = intent_for(
        root, b'{"synthetic":2}\n' if conflict else b'{"synthetic":1}\n', attempt="attempt-b"
    )
    barrier = CONTEXT.Barrier(2)
    workers = []
    try:
        for intent in (first, second):
            parent, child = CONTEXT.Pipe()
            process = CONTEXT.Process(
                target=_raw_child, args=(str(root), intent, child, None, barrier)
            )
            process.start()
            workers.append((process, parent))
        results = [receive(process, connection) for process, connection in workers]
        statuses = sorted(value["status"] for value in results)
        require(
            statuses == (["conflict", "published"] if conflict else ["published", "published"]),
            "Publication collision violated immutability",
        )
        snapshots = [
            RawSnapshot(**value["snapshot"]) for value in results if value["status"] == "published"
        ]
        require(all(value == snapshots[0] for value in snapshots), "Identical publishers disagree")
        require(store.verify(snapshots[0]) == snapshots[0], "Collision changed the winner")
        return snapshots[0]
    finally:
        for process, connection in workers:
            if process.is_alive():
                process.kill()
                process.join(10)
            connection.close()


def raw_sync_failure(root: Path) -> RawSnapshot:
    store, intent = intent_for(root)
    original = os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory synchronization failure")
        return original(descriptor)

    # First publish normally, then emulate recovery while directory fsync fails.
    snapshot = store.publish(intent)
    with patch.object(os, "fsync", fail_directory):
        try:
            store.publish(intent)
        except PublicationUncertain:
            pass
        else:
            raise RuntimeError("Publication acknowledged an unsynchronized retry")
    require(store.publish(intent) == snapshot, "Publication retry changed identity")
    return snapshot


def sync_failures(root: Path) -> None:
    from src.plugins.training.pytorch_trainer import durable_replace

    root.mkdir()
    source, target = root / "checkpoint.tmp", root / "checkpoint.pt"
    source.write_bytes(b"new-checkpoint")
    target.write_bytes(b"previous-checkpoint")
    with patch.object(os, "fsync", side_effect=OSError(errno.ENOSPC, "injected")):
        try:
            durable_replace(source, target)
        except OSError:
            pass
        else:
            raise RuntimeError("Checkpoint acknowledged an ENOSPC sync failure")
    require(
        target.read_bytes() == b"previous-checkpoint",
        "Sync failure replaced the previous checkpoint",
    )
    original = os.fsync

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected")
        return original(descriptor)

    for _ in range(2):
        with patch.object(os, "fsync", fail_directory):
            try:
                _receipt(root / "receipt", {"qualification": CONTRACT})
            except OSError:
                pass
            else:
                raise RuntimeError("Receipt acknowledged an unsynchronized directory")
    digest, location = _receipt(root / "receipt", {"qualification": CONTRACT})
    require(
        hashlib.sha256(Path(location).read_bytes()).hexdigest() == digest,
        "Receipt reconciliation changed content",
    )


def primitives(root: Path) -> None:
    root.mkdir()
    source, target = root / "source", root / "target"
    source.mkdir()
    target.mkdir()
    with raw.directory_fd(root) as descriptor:
        try:
            raw._rename_exclusive("source", "target", descriptor, descriptor)
        except FileExistsError:
            pass
        else:
            raise RuntimeError("rename-no-replace overwrote an existing empty directory")
        os.fsync(descriptor)
    original = root / "file"
    original.write_bytes(b"storage-contract")
    with original.open("rb") as stream:
        os.fsync(stream.fileno())
    os.link(original, root / "linked", follow_symlinks=False)
    require(
        original.stat().st_ino == (root / "linked").stat().st_ino,
        "Hard link did not preserve identity",
    )
    try:
        os.link(original, root / "linked", follow_symlinks=False)
    except FileExistsError:
        pass
    else:
        raise RuntimeError("Hard-link publication replaced existing evidence")
    with raw.directory_fd(root) as descriptor:
        os.fsync(descriptor)


def dataset_metadata() -> dict:
    from src.domain.datasets.ebm import EBM_DATASET_SCHEMA, EBM_PAD_ORDER, EBM_SIGNAL_ORDER

    return {
        "schema_version": EBM_DATASET_SCHEMA,
        "signal_order": list(EBM_SIGNAL_ORDER),
        "pad_order": list(EBM_PAD_ORDER),
        "feature_config": {"version": "fdc_spectrogram_v1", "log1p": True},
        "samples": [
            {
                "sample_id": f"sample-{i}",
                "lot_id": f"lot-{i}",
                "wafer_id": "1",
                "recipe_id": "recipe-a",
                "segment_id": 1,
                "event_time": f"2026-01-{i + 1:02d}T00:00:00Z",
                "available_at": None,
                "cdf_target": 0.1 + i * 0.08,
                "pad_counts": [float(i + 1)] * 4,
            }
            for i in range(10)
        ],
        "splits": {"train": list(range(6)), "validation": [6, 7], "test": [8, 9]},
    }


def dataset(root: Path) -> dict:
    import numpy as np

    from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
    from src.infrastructure.persistence.ml_database import Database
    from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

    store = EBMFileDatasetSource(root / "datasets")
    arrays = np.random.default_rng(30).uniform(0, 2, (10, 7, 4, 9, 31)).astype(np.float32)
    reference = store.publish(dataset_metadata(), arrays)
    require(
        store.publish(dataset_metadata(), arrays) == reference,
        "Dataset publication is not idempotent",
    )
    loaded = store.load(reference)
    np.testing.assert_array_equal(loaded.spectrograms, arrays)
    database = Database("sqlite://")
    try:
        publisher = LocalEBMDatasetPublisher(
            SQLDatasetCatalog(database, root / "catalog"), root / "builder"
        )
        receipt = {"qualification": CONTRACT, "dataset_digest": loaded.content_digest}
        digest, location = publisher._write_receipt(receipt)
        require(
            publisher._write_receipt(receipt) == (digest, location),
            "Receipt retry changed identity",
        )
    finally:
        database.close()
    return {"reference": reference, "receipt_digest": digest, "receipt_ref": location}


def _dataset_child(root, connection, cut):
    import numpy as np

    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource

    original = Path.rename

    def rename(source, destination):
        if cut == "before":
            _pause(connection)
        result = original(source, destination)
        if cut == "after":
            _pause(connection)
        return result

    arrays = np.random.default_rng(30).uniform(0, 2, (10, 7, 4, 9, 31)).astype(np.float32)
    with patch.object(Path, "rename", rename):
        EBMFileDatasetSource(Path(root)).publish(dataset_metadata(), arrays)


def crash_dataset(root: Path, cut: str) -> None:
    import numpy as np

    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource

    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(target=_dataset_child, args=(str(root), child, cut))
    process.start()
    receive(process, parent, kill=True)
    visible = [path for path in root.iterdir() if not path.name.startswith(".")]
    require(
        len(visible) == (0 if cut == "before" else 1), "Dataset exposed a partial public identity"
    )
    arrays = np.random.default_rng(30).uniform(0, 2, (10, 7, 4, 9, 31)).astype(np.float32)
    source = EBMFileDatasetSource(root)
    reference = source.publish(dataset_metadata(), arrays)
    np.testing.assert_array_equal(source.load(reference).spectrograms, arrays)
    require(
        source.publish(dataset_metadata(), arrays) == reference,
        "Dataset recovery changed content identity",
    )


def _receipt(root: Path, value: dict):
    from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
    from src.infrastructure.persistence.ml_database import Database
    from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

    database = Database("sqlite://")
    try:
        return LocalEBMDatasetPublisher(
            SQLDatasetCatalog(database, root / "catalog"), root
        )._write_receipt(value)
    finally:
        database.close()


def _receipt_child(root, connection, cut):
    original = os.link

    def link(*arguments, **kwargs):
        if cut == "before":
            _pause(connection)
        original(*arguments, **kwargs)
        if cut == "after":
            _pause(connection)

    with patch.object(os, "link", link):
        _receipt(Path(root), {"qualification": CONTRACT})


def crash_receipt(root: Path, cut: str) -> None:
    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(target=_receipt_child, args=(str(root), child, cut))
    process.start()
    receive(process, parent, kill=True)
    require(
        len(list((root / "receipts").glob("*.json"))) == (0 if cut == "before" else 1),
        "Receipt exposed an unexpected identity",
    )
    digest, location = _receipt(root, {"qualification": CONTRACT})
    require(
        hashlib.sha256(Path(location).read_bytes()).hexdigest() == digest,
        "Receipt recovery changed bytes",
    )
    require(
        _receipt(root, {"qualification": CONTRACT}) == (digest, location),
        "Receipt recovery is not idempotent",
    )


def _json_child(path, connection, cut, value):
    original = Path.replace

    def replace(source, destination):
        if cut == "before":
            _pause(connection)
        result = original(source, destination)
        if cut == "after":
            _pause(connection)
        return result

    with patch.object(Path, "replace", replace):
        atomic_json(Path(path), value)


def crash_json(root: Path, filename: str, cut: str) -> None:
    root.mkdir()
    path = root / filename
    atomic_json(path, {"revision": 1})
    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(target=_json_child, args=(str(path), child, cut, {"revision": 2}))
    process.start()
    receive(process, parent, kill=True)
    require(
        json.loads(path.read_text()) == {"revision": 1 if cut == "before" else 2},
        "Atomic JSON exposed partial bytes",
    )


def training_spec() -> dict:
    return {
        "model": {"config": {"hidden_units": [4]}},
        "training": {"seed": 7, "epochs": 2, "batch_size": 128, "learning_rate": 0.01},
        "dataset": {},
        "code_revision": CODE,
        "training_image_digest": "sha256:" + "a" * 64,
    }


def train(kind: str, directory: Path, dataset_ref: dict, progress, resume=None):
    if kind == "mlp":
        from src.plugins.training.pytorch_trainer import PyTorchTrainer

        return PyTorchTrainer().train(training_spec(), directory, progress, resume=resume)
    from src.domain.training.ebm import EBMTrainingConfig
    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
    from src.plugins.training.ebm_trainer_v1 import EBMTrainerV1

    source = EBMFileDatasetSource(Path(dataset_ref["snapshot_ref"]).parent.parent)
    return EBMTrainerV1().train(
        EBMTrainingConfig(epochs=2, batch_size=3, pair_sample_size=8),
        directory,
        progress,
        dataset=source.load(dataset_ref),
        code_digest=CODE,
        resume=resume,
    )


def _training_child(kind, directory, dataset_ref, connection):
    def progress(step, metrics):
        # MLP callback precedes the current checkpoint; EBM callback follows it.
        if step == (1 if kind == "mlp" else 0):
            _pause(connection)

    train(kind, Path(directory), dataset_ref, progress)


def weights_digest(directory: Path) -> str:
    import torch

    value = torch.load(directory / "bundle/weights.pt", map_location="cpu", weights_only=True)
    checksum = hashlib.sha256()
    for key in sorted(value):
        checksum.update(key.encode())
        checksum.update(value[key].contiguous().numpy().tobytes())
    return checksum.hexdigest()


def checkpoint(root: Path, kind: str, reference: dict) -> dict:
    interrupted, baseline = root / (kind + "-interrupted"), root / (kind + "-baseline")
    interrupted.mkdir()
    baseline.mkdir()
    parent, child = CONTEXT.Pipe()
    process = CONTEXT.Process(
        target=_training_child, args=(kind, str(interrupted), reference, child)
    )
    process.start()
    receive(process, parent, kill=True)
    path = interrupted / "checkpoint.pt"
    require(path.is_file(), "Interrupted trainer left no completed checkpoint")
    train(kind, baseline, reference, lambda *_: None)
    return {
        "checkpoint": str(path),
        "checkpoint_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "weights_digest": weights_digest(baseline),
    }


def run_suite(root: Path, cache: Path) -> dict:
    require(
        not any(path.is_symlink() for path in (root, *root.parents)),
        "Qualification paths must not contain symbolic links",
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    cache.mkdir(parents=True, exist_ok=True)
    require(not any(cache.iterdir()), "Qualification cache must start empty")
    primitives(root / "primitives")
    sync_failures(root / "sync-failures")
    snapshots = []
    for cut in ("before", "after"):
        snapshots.append(asdict(crash_raw(root / ("raw-crash-" + cut), cut)))
    for conflict in (False, True):
        snapshots.append(asdict(race_raw(root / ("raw-race-" + str(conflict)), conflict)))
    snapshots.append(asdict(raw_sync_failure(root / "raw-sync-failure")))
    corrupt, intent = intent_for(root / "raw-corrupt")
    snapshot = corrupt.publish(intent)
    (Path(snapshot.snapshot_ref).parent / "data.jsonl").write_bytes(b'{"corrupt":true}\n')
    try:
        corrupt.verify(snapshot)
    except SnapshotInvalid:
        pass
    else:
        raise RuntimeError("Corrupt raw bytes passed verification")
    result = dataset(root)
    for cut in ("before", "after"):
        crash_dataset(root / ("dataset-crash-" + cut), cut)
        crash_receipt(root / ("receipt-crash-" + cut), cut)
    for filename in ("progress.json", "result.json"):
        for cut in ("before", "after"):
            crash_json(root / (filename + "-" + cut), filename, cut)
    checkpoints = {kind: checkpoint(root, kind, result["reference"]) for kind in ("mlp", "ebm")}
    atomic_json(cache / "writer-only.json", {"disposable": True})
    report = {
        "contract_version": CONTRACT,
        "phase": "WRITTEN",
        "raw_snapshots": snapshots,
        "dataset": result,
        "checkpoints": checkpoints,
        "checks": [
            "rename_no_replace",
            "hard_links",
            "file_and_directory_fsync",
            "checkpoint_enospc_preserves_prior",
            "receipt_sync_failure_reconciliation",
            "raw_concurrent_identical",
            "raw_concurrent_conflict",
            "raw_sigkill_before_rename",
            "raw_sigkill_after_rename",
            "fsync_failure_reconciliation",
            "raw_corruption_rejected",
            "dataset_digest",
            "receipt_idempotency",
            "dataset_sigkill_before_after_rename",
            "receipt_sigkill_before_after_link",
            "atomic_progress_result_sigkill",
            "mlp_checkpoint_sigkill",
            "ebm_checkpoint_sigkill",
        ],
        "power_loss_qualified": False,
        "cross_node_qualified": False,
        "production_qualified": False,
    }
    atomic_json(root / "written.json", report)
    return report


def pod_crash_boundary(root: Path) -> None:
    store, intent = intent_for(root / "pod-crash")
    atomic_json(root / "pod-intent.json", asdict(intent))
    original = raw._rename_exclusive

    def rename(*arguments):
        original(*arguments)
        print("POD_CRASH_READY", flush=True)
        while True:
            time.sleep(1)

    with patch.object(raw, "_rename_exclusive", rename):
        store.publish(intent)


def recover_pod(root: Path) -> None:
    intent = saved_intent(json.loads((root / "pod-intent.json").read_text()))
    store = raw.LocalRawSnapshotStore(root / "pod-crash")
    snapshot = store.publish(intent)
    atomic_json(root / "pod-recovered.json", asdict(snapshot))


def verify_suite(root: Path, cache: Path) -> dict:
    import shutil

    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
    from src.infrastructure.serving.materializer import verify_bundle

    report = json.loads((root / "written.json").read_text())
    require(report["contract_version"] == CONTRACT, "Unknown storage qualification contract")
    require(cache.is_dir() and not any(cache.iterdir()), "New Pod inherited a disposable cache")
    for value in report["raw_snapshots"]:
        snapshot = RawSnapshot(**value)
        store_root = Path(snapshot.snapshot_ref).parents[3]
        require(
            raw.LocalRawSnapshotStore(store_root).verify(snapshot) == snapshot,
            "Reader observed changed raw content",
        )
    info = report["dataset"]
    source = EBMFileDatasetSource(Path(info["reference"]["snapshot_ref"]).parent.parent)
    source.load(info["reference"])
    require(
        hashlib.sha256(Path(info["receipt_ref"]).read_bytes()).hexdigest()
        == info["receipt_digest"],
        "Receipt changed between Pods",
    )
    with tempfile.TemporaryDirectory(prefix="storage-resume-") as temporary:
        for kind, expected in report["checkpoints"].items():
            checkpoint_path = Path(expected["checkpoint"])
            require(
                hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
                == expected["checkpoint_digest"],
                "Checkpoint changed between Pods",
            )
            destination = Path(temporary) / kind
            destination.mkdir()
            result = train(
                kind, destination, info["reference"], lambda *_: None, resume=checkpoint_path
            )
            require(
                weights_digest(destination) == expected["weights_digest"],
                "Resumed weights differ from uninterrupted training",
            )
            verify_bundle(Path(result["bundle_path"]), result["manifest_digest"])
            baseline = root / (kind + "-baseline")
            metadata = json.loads((baseline / "result.json").read_text())
            verify_bundle(baseline / "bundle", metadata["manifest_digest"])
            shutil.copytree(baseline / "bundle", cache / kind)
            verify_bundle(cache / kind, metadata["manifest_digest"])
    recovered = RawSnapshot(**json.loads((root / "pod-recovered.json").read_text()))
    raw.LocalRawSnapshotStore(root / "pod-crash").verify(recovered)
    return {
        "status": "STORAGE_CONTRACTS_PASSED",
        "contract_version": CONTRACT,
        "checks": report["checks"]
        + [
            "pod_recreation_recovery",
            "readonly_pvc_reader",
            "mlp_resume_equal",
            "ebm_resume_equal",
            "cache_rebuild",
        ],
        "power_loss_qualified": False,
        "cross_node_qualified": False,
        "production_qualified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("write", "recover", "verify"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("/cache"))
    args = parser.parse_args()
    require(
        args.root.is_absolute() and ".." not in args.root.parts,
        "Absolute qualification path required",
    )
    if args.operation == "write":
        run_suite(args.root, args.cache)
        pod_crash_boundary(args.root)
    elif args.operation == "recover":
        recover_pod(args.root)
        print("POD_RECOVERED", flush=True)
    else:
        print(json.dumps(verify_suite(args.root, args.cache)), flush=True)


if __name__ == "__main__":
    main()
