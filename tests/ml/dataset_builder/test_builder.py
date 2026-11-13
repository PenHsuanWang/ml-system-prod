from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from src.domain.datasets.ebm import EBM_FEATURE_SHAPE, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.datasets.ebm_builder import EBMDatasetBuilderConfig
from src.domain.ml import MLError
from src.plugins.features.ebm_dataset_builder_v1 import EBMDatasetBuilderV1
from src.plugins.features.fdc_spectrogram_v1 import FdcSpectrogramV1
from tests.ml.dataset_builder.conftest import raw_records, verified_snapshot_set


def test_builder_aligns_maps_splits_and_reuses_exact_lot_features():
    fdc, wafers = raw_records()
    builder = EBMDatasetBuilderV1()
    config = EBMDatasetBuilderConfig("recipe-a", 1, split_seed=17)
    metadata, features = builder.build(verified_snapshot_set(fdc, wafers), config)
    reversed_metadata, reversed_features = builder.build(
        verified_snapshot_set(fdc, wafers, reverse=True), config
    )

    assert metadata == reversed_metadata
    np.testing.assert_array_equal(features, reversed_features)
    assert features.dtype == np.dtype("float32")
    assert features.shape == (10, *EBM_FEATURE_SHAPE)
    assert np.isfinite(features).all()
    assert [sample.sample_id for sample in metadata.samples] == [
        f"sample-{lot:02d}-{wafer:02d}" for lot in range(5) for wafer in range(2)
    ]
    for index in range(0, len(features), 2):
        np.testing.assert_array_equal(features[index], features[index + 1])

    split_lots = [
        {metadata.samples[index].lot_id for index in indices}
        for indices in (metadata.train, metadata.validation, metadata.test)
    ]
    assert all(split_lots)
    assert not (split_lots[0] & split_lots[1] or split_lots[0] & split_lots[2])
    assert not split_lots[1] & split_lots[2]
    assert set.union(*split_lots) == {f"lot-{index:02d}" for index in range(5)}

    aligned = {
        signal: {
            pad: np.asarray(
                [1.0 + signal_index / 10 + pad_index / 100 + second / 4 for second in range(5)],
                dtype=np.float32,
            )
            for pad_index, pad in enumerate(EBM_PAD_ORDER)
        }
        for signal_index, signal in enumerate(EBM_SIGNAL_ORDER)
    }
    expected = FdcSpectrogramV1(EBM_SIGNAL_ORDER).transform_batch([aligned])[0]
    np.testing.assert_array_equal(features[0], expected)


@pytest.mark.parametrize(
    "case",
    [
        "missing_trace",
        "duplicate_time",
        "naive_time",
        "nonfinite",
        "unknown_record",
        "wrong_schema",
        "extra_field",
        "no_common_interval",
        "duplicate_target",
        "conflicting_target",
        "orphan_lot",
        "early_target",
    ],
)
def test_builder_rejects_ambiguous_or_incomplete_raw_contracts(case):
    fdc, wafers = raw_records()
    fdc, wafers = copy.deepcopy(fdc), copy.deepcopy(wafers)
    if case == "missing_trace":
        fdc.pop(0)
    elif case == "duplicate_time":
        fdc[0]["observations"][1]["timestamp"] = fdc[0]["observations"][0]["timestamp"]
    elif case == "naive_time":
        fdc[0]["observations"][0]["timestamp"] = "2026-01-01T00:00:00"
    elif case == "nonfinite":
        fdc[0]["observations"][0]["value"] = float("nan")
    elif case == "unknown_record":
        fdc[0]["record_type"] = "unknown"
    elif case == "wrong_schema":
        fdc[0]["schema_version"] = "ebm-raw-input-v0"
    elif case == "extra_field":
        wafers[0]["guessed_mapping"] = True
    elif case == "no_common_interval":
        shifted = datetime(2027, 1, 1, tzinfo=UTC)
        fdc[0]["observations"] = [
            {"timestamp": (shifted + timedelta(seconds=offset)).isoformat(), "value": 1.0}
            for offset in (0, 2, 4)
        ]
    elif case == "duplicate_target":
        wafers.append(copy.deepcopy(wafers[0]))
    elif case == "conflicting_target":
        conflict = copy.deepcopy(wafers[0])
        conflict["sample_id"] = "different-sample"
        conflict["cdf_target"] = 0.9
        wafers.append(conflict)
    elif case == "orphan_lot":
        wafers[:] = [record for record in wafers if record["lot_id"] != "lot-04"]
    else:
        wafers[0]["event_time"] = "2025-12-31T23:59:59Z"

    with pytest.raises(MLError):
        EBMDatasetBuilderV1().build(
            verified_snapshot_set(fdc, wafers), EBMDatasetBuilderConfig("recipe-a", 1)
        )


def test_builder_uses_the_exact_common_integer_utc_interval_without_extrapolation():
    fdc, wafers = raw_records()
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    fdc[0]["observations"] = [
        {
            "timestamp": (origin + timedelta(seconds=offset)).isoformat(),
            "value": 1.0 + offset / 4,
        }
        for offset in (0.8, 2.8, 4.8)
    ]

    metadata, features = EBMDatasetBuilderV1().build(
        verified_snapshot_set(fdc, wafers), EBMDatasetBuilderConfig("recipe-a", 1)
    )
    grid = np.asarray([origin.timestamp() + second for second in range(1, 5)])
    aligned: dict[str, dict[str, np.ndarray]] = {signal: {} for signal in EBM_SIGNAL_ORDER}
    for record in (item for item in fdc if item["lot_id"] == "lot-00"):
        observations = record["observations"]
        timestamps = np.asarray(
            [datetime.fromisoformat(item["timestamp"]).timestamp() for item in observations]
        )
        values = np.asarray([item["value"] for item in observations])
        aligned[record["signal"]][record["pad"]] = np.interp(grid, timestamps, values).astype(
            np.float32
        )
    expected = FdcSpectrogramV1(EBM_SIGNAL_ORDER).transform_batch([aligned])[0]
    sample_index = next(
        index for index, sample in enumerate(metadata.samples) if sample.lot_id == "lot-00"
    )
    np.testing.assert_array_equal(features[sample_index], expected)


def test_builder_preserves_an_explicitly_missing_optional_pad_count():
    fdc, wafers = raw_records()
    wafers[0].pop("pad_counts")
    metadata, _ = EBMDatasetBuilderV1().build(
        verified_snapshot_set(fdc, wafers), EBMDatasetBuilderConfig("recipe-a", 1)
    )
    assert metadata.samples[0].pad_counts is None


def test_builder_requires_three_selected_lots_and_an_explicit_matching_filter():
    fdc, wafers = raw_records(lot_count=2)
    with pytest.raises(MLError, match="at least three"):
        EBMDatasetBuilderV1().build(
            verified_snapshot_set(fdc, wafers), EBMDatasetBuilderConfig("recipe-a", 1)
        )
    fdc, wafers = raw_records()
    with pytest.raises(MLError, match="selected no complete data"):
        EBMDatasetBuilderV1().build(
            verified_snapshot_set(fdc, wafers), EBMDatasetBuilderConfig("other-recipe", 1)
        )
