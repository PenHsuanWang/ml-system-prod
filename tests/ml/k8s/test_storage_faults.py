"""Real Linux filesystem/process tests, also executed on the qualification PVC."""

import errno
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import storage_contracts as contracts
from src.plugins.training.pytorch_trainer import atomic_json, durable_replace


def test_required_filesystem_operations(tmp_path):
    contracts.primitives(tmp_path / "filesystem")


@pytest.mark.parametrize("cut", ["before", "after"])
def test_raw_process_death_recovers_exact_intent(tmp_path, cut):
    contracts.crash_raw(tmp_path / "raw", cut)


@pytest.mark.parametrize("conflict", [False, True])
def test_separate_publishers_preserve_one_immutable_winner(tmp_path, conflict):
    contracts.race_raw(tmp_path / "raw", conflict)


def test_raw_retry_does_not_acknowledge_failed_directory_sync(tmp_path):
    contracts.raw_sync_failure(tmp_path / "raw")


@pytest.mark.parametrize("filename", ["progress.json", "result.json"])
@pytest.mark.parametrize("cut", ["before", "after"])
def test_progress_and_result_remain_complete_after_sigkill(tmp_path, filename, cut):
    contracts.crash_json(tmp_path / "json", filename, cut)


@pytest.mark.parametrize("cut", ["before", "after"])
def test_dataset_process_death_never_exposes_partial_publication(tmp_path, cut):
    contracts.crash_dataset(tmp_path / "dataset", cut)


@pytest.mark.parametrize("cut", ["before", "after"])
def test_receipt_process_death_preserves_immutable_identity(tmp_path, cut):
    contracts.crash_receipt(tmp_path / "receipt", cut)


@pytest.mark.torch
@pytest.mark.parametrize("kind", ["mlp", "ebm"])
def test_sigkill_checkpoint_resumes_to_identical_weights(tmp_path, kind):
    reference = contracts.dataset(tmp_path)["reference"]
    expected = contracts.checkpoint(tmp_path, kind, reference)
    destination = tmp_path / "resumed"
    destination.mkdir()
    contracts.train(
        kind, destination, reference, lambda *_: None, resume=Path(expected["checkpoint"])
    )
    assert contracts.weights_digest(destination) == expected["weights_digest"]


def test_durable_replace_orders_file_and_directory_sync(tmp_path, monkeypatch):
    source, target = tmp_path / "temporary", tmp_path / "checkpoint.pt"
    source.write_bytes(b"complete-checkpoint")
    observed = []
    original = os.fsync

    def sync(descriptor):
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        observed.append((is_directory, target.exists()))
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    durable_replace(source, target)
    assert observed == [(False, False), (True, True)]


def test_file_sync_failure_preserves_previous_checkpoint(tmp_path, monkeypatch):
    source, target = tmp_path / "temporary", tmp_path / "checkpoint.pt"
    source.write_bytes(b"new")
    target.write_bytes(b"previous")
    monkeypatch.setattr(
        os, "fsync", lambda _: (_ for _ in ()).throw(OSError(errno.ENOSPC, "injected"))
    )
    with pytest.raises(OSError):
        durable_replace(source, target)
    assert target.read_bytes() == b"previous"


def test_json_directory_sync_failure_never_reports_success(tmp_path):
    original = os.fsync

    def sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected")
        return original(descriptor)

    with patch.object(os, "fsync", sync), pytest.raises(OSError):
        atomic_json(tmp_path / "result.json", {"complete": True})
    assert (tmp_path / "result.json").read_text() == '{"complete":true}'


def test_receipt_retry_requires_successful_directory_sync(tmp_path):
    original = os.fsync

    def sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected")
        return original(descriptor)

    for _ in range(2):
        with patch.object(os, "fsync", sync), pytest.raises(OSError):
            contracts._receipt(tmp_path, {"complete": True})
    first = contracts._receipt(tmp_path, {"complete": True})
    assert contracts._receipt(tmp_path, {"complete": True}) == first


def test_dataset_retry_requires_successful_parent_directory_sync(tmp_path, monkeypatch):
    import numpy as np

    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource

    store = EBMFileDatasetSource(tmp_path / "dataset")
    arrays = np.zeros((10, 7, 4, 9, 31), dtype=np.float32)
    reference = store.publish(contracts.dataset_metadata(), arrays)
    original = store._sync_directory

    def sync(path):
        if path == store.root:
            raise OSError(errno.EIO, "injected")
        original(path)

    monkeypatch.setattr(store, "_sync_directory", sync)
    with pytest.raises(OSError):
        store.publish(contracts.dataset_metadata(), arrays)
    assert store.load(reference).content_digest == reference["content_digest"]
