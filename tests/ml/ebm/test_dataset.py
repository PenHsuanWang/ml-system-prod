"""Trust-boundary and replay-mapping checks for local EBM feature snapshots."""

import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from src.domain.datasets.ebm import (
    EBM_DATASET_SCHEMA,
    EBM_FEATURE_SHAPE,
    EBM_PAD_ORDER,
    EBM_SIGNAL_ORDER,
    EBMDatasetMetadata,
)
from src.domain.ml import MLError
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource


def dataset_value():
    return {
        "schema_version": EBM_DATASET_SCHEMA,
        "signal_order": list(EBM_SIGNAL_ORDER),
        "pad_order": list(EBM_PAD_ORDER),
        "feature_config": {"version": "fdc_spectrogram_v1", "log1p": True},
        "samples": [
            {"sample_id": f"sample-{index}", "lot_id": f"lot-{index // 2}",
             "recipe_id": "recipe-a", "segment_id": index // 2 + 1,
             "wafer_id": str(index % 2), "cdf_target": 0.2 + index / 10,
             "event_time": f"2026-01-0{index // 2 + 1}T00:00:00Z", "available_at": None,
             "pad_counts": [index + 1.0] * 4}
            for index in range(6)
        ],
        "splits": {"train": [0, 1], "validation": [2, 3], "test": [4, 5]},
    }


def arrays():
    return np.repeat(np.arange(3, dtype=np.float32), 2).reshape(6, 1, 1, 1, 1) * np.ones(
        (6, *EBM_FEATURE_SHAPE), dtype=np.float32
    )


def test_publish_is_content_addressed_and_repeated_lots_keep_wafer_targets(tmp_path):
    source = EBMFileDatasetSource(tmp_path / "store")
    value = dataset_value()
    data = arrays()
    reference = source.publish(value, data)
    assert source.publish(value, data) == reference
    snapshot = source.load(reference)
    assert snapshot.metadata.train == (0, 1)
    assert snapshot.metadata.samples[0].lot_id == snapshot.metadata.samples[1].lot_id
    assert snapshot.metadata.samples[0].cdf_target != snapshot.metadata.samples[1].cdf_target
    assert snapshot.metadata.samples[0].pad_counts == (1.0, 1.0, 1.0, 1.0)
    assert snapshot.metadata.samples[0].available_at is None
    assert snapshot.metadata.spectrogram_log1p is True
    np.testing.assert_array_equal(snapshot.spectrograms, data)
    data[0] = 99
    assert snapshot.spectrograms[0].max() == 0
    with pytest.raises(ValueError):
        snapshot.spectrograms.setflags(write=True)
    changed = copy.deepcopy(value)
    changed["samples"][0]["cdf_target"] = 0.9
    assert source.publish(changed, arrays())["content_digest"] != reference["content_digest"]
    assert source.load(reference).metadata.samples[0].cdf_target == 0.2


def test_masks_and_indices_resolve_to_the_same_content(tmp_path):
    value = dataset_value()
    masked = copy.deepcopy(value)
    for name, indices in value["splits"].items():
        masked["splits"][name] = [index in indices for index in range(6)]
    source = EBMFileDatasetSource(tmp_path)
    assert source.publish(value, arrays()) == source.publish(masked, arrays())


def test_feature_transform_configuration_is_bound_to_snapshot_digest(tmp_path):
    source = EBMFileDatasetSource(tmp_path)
    value = dataset_value()
    original = source.publish(value, arrays())
    value["feature_config"]["log1p"] = False
    changed = source.publish(value, arrays())
    assert changed["content_digest"] != original["content_digest"]
    assert source.load(changed).metadata.spectrogram_log1p is False


@pytest.mark.parametrize("case", ["signal_order", "pad_order", "overlap", "incomplete",
                                  "same_lot_cross_split", "mixed_mask", "duplicate_wafer",
                                  "nan_target", "pad_counts", "naive_time", "future_event",
                                  "missing_feature_config", "invalid_log1p"])
def test_invalid_metadata_is_rejected(case):
    value = dataset_value()
    if case in {"signal_order", "pad_order"}:
        value[case].reverse()
    elif case == "overlap":
        value["splits"]["test"] = [0, 5]
    elif case == "incomplete":
        value["splits"]["test"] = [4]
    elif case == "same_lot_cross_split":
        value["splits"] = {"train": [0, 2], "validation": [1, 3], "test": [4, 5]}
    elif case == "mixed_mask":
        value["splits"]["train"] = [True, 1]
    elif case == "duplicate_wafer":
        value["samples"][1]["wafer_id"] = "0"
    elif case == "nan_target":
        value["samples"][0]["cdf_target"] = float("nan")
    elif case == "pad_counts":
        value["samples"][0]["pad_counts"] = [1, 2, float("inf"), 4]
    elif case == "naive_time":
        value["samples"][0]["event_time"] = "2026-01-01T00:00:00"
    elif case == "future_event":
        value["samples"][0]["available_at"] = "2025-12-31T23:00:00Z"
    elif case == "missing_feature_config":
        del value["feature_config"]
    elif case == "invalid_log1p":
        value["feature_config"]["log1p"] = 1
    with pytest.raises(MLError):
        EBMDatasetMetadata.parse(value)


@pytest.mark.parametrize("case", ["shape", "dtype", "nan", "lot_mapping"])
def test_array_contract_is_checked_before_publish(tmp_path, case):
    data = arrays()
    if case == "shape":
        data = data[:, :, :, :, :-1]
    elif case == "dtype":
        data = data.astype(np.float64)
    elif case == "nan":
        data[0, 0, 0, 0, 0] = np.nan
    else:
        data[1, 0, 0, 0, 0] = 12
    with pytest.raises(MLError):
        EBMFileDatasetSource(tmp_path).publish(dataset_value(), data)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["manifest.json", "metadata.json", "spectrograms.npz"])
def test_tampered_content_is_rejected(tmp_path, name):
    source = EBMFileDatasetSource(tmp_path)
    reference = source.publish(dataset_value(), arrays())
    path = Path(reference["snapshot_ref"]).parent / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(MLError, match="digest|size"):
        source.load(reference)
    with pytest.raises(MLError):
        source.publish(dataset_value(), arrays())


def test_outside_paths_and_symlinked_ancestors_are_rejected(tmp_path):
    source = EBMFileDatasetSource(tmp_path / "approved")
    reference = source.publish(dataset_value(), arrays())
    with pytest.raises(MLError, match="outside"):
        EBMFileDatasetSource(tmp_path / "other").load(reference)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "approved", target_is_directory=True)
    with pytest.raises(MLError, match="symbolic"):
        EBMFileDatasetSource(alias / "nested")
    directory = Path(reference["snapshot_ref"]).parent
    payload = directory / "metadata.json"
    outside = tmp_path / "outside.json"
    payload.rename(outside)
    payload.symlink_to(outside)
    with pytest.raises(MLError, match="symbolic"):
        source.load(reference)


@pytest.mark.parametrize("kind", ["object", "extra_member", "forged_shape"])
def test_import_rejects_unsafe_npz_before_array_allocation(tmp_path, kind):
    value = dataset_value()
    metadata = tmp_path / "input.json"
    metadata.write_text(json.dumps(value))
    npz = tmp_path / "input.npz"
    if kind == "object":
        np.savez(npz, spectrograms=np.empty((6, *EBM_FEATURE_SHAPE), dtype=object))
    elif kind == "extra_member":
        np.savez(npz, spectrograms=arrays(), unexpected=np.array([1]))
    else:
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, {
            "descr": "<f4", "fortran_order": False, "shape": (2**40, *EBM_FEATURE_SHAPE),
        })
        with zipfile.ZipFile(npz, "w") as archive:
            archive.writestr("spectrograms.npy", header.getvalue())
    with pytest.raises(MLError):
        EBMFileDatasetSource(tmp_path).import_files(metadata, npz)


def test_safe_npz_import_roundtrip_and_manifest_binding(tmp_path):
    metadata = tmp_path / "input.json"
    metadata.write_text(json.dumps(dataset_value()))
    npz = tmp_path / "input.npz"
    np.savez_compressed(npz, spectrograms=arrays())
    source = EBMFileDatasetSource(tmp_path)
    reference = source.import_files(metadata, npz)
    manifest = Path(reference["snapshot_ref"]).read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == reference["content_digest"]
    np.testing.assert_array_equal(source.load(reference).spectrograms, arrays())


def test_oversized_metadata_is_rejected_before_json_decode(tmp_path):
    source = EBMFileDatasetSource(tmp_path)
    metadata = tmp_path / "oversized.json"
    with metadata.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(MLError, match="size limit"):
        source.import_files(metadata, tmp_path / "unused.npz")
