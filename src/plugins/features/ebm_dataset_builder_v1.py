"""Deterministic EBM-specific conversion from validated RawSnapshot records."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING

from src.domain.datasets.ebm import (
    EBM_FEATURE_SHAPE,
    EBM_MAX_ROWS,
    EBM_PAD_ORDER,
    EBM_SIGNAL_ORDER,
    EBMDatasetMetadata,
    EBMSample,
)
from src.domain.datasets.ebm_builder import (
    EBM_DATASET_BUILDER_ID,
    EBM_DATASET_BUILDER_VERSION,
    EBMDatasetBuilderConfig,
    EBMRawSnapshotSet,
    FDCTraceRecord,
    WaferTargetRecord,
)
from src.domain.ml import MLError
from src.plugins.features.fdc_spectrogram_v1 import FdcSpectrogramV1, SpectrogramConfig

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class EBMDatasetBuilderV1:
    """Build one retrospective EBM dataset from the explicit v1 raw contract.

    Traces are aligned only inside the interval shared by all 28 signal/Pad
    series for a Lot. The interval is sampled at UTC integer seconds using
    linear interpolation without extrapolation. Only its final 256 seconds are
    materialized because ``FdcSpectrogramV1`` applies the same keep-tail rule.
    """

    builder_id = EBM_DATASET_BUILDER_ID
    builder_version = EBM_DATASET_BUILDER_VERSION

    @staticmethod
    def _lot_splits(lot_ids: tuple[str, ...], seed: int) -> dict[str, str]:
        if len(lot_ids) < 3:
            raise MLError("EBM DatasetBuilder requires at least three selected Lots")

        def order(lot_id: str) -> tuple[bytes, str]:
            value = hashlib.sha256()
            value.update(EBM_DATASET_BUILDER_VERSION.encode("utf-8"))
            value.update(b"\0")
            value.update(str(seed).encode("ascii"))
            value.update(b"\0")
            value.update(lot_id.encode("utf-8"))
            return value.digest(), lot_id

        ordered = tuple(sorted(lot_ids, key=order))
        validation_count = max(1, len(ordered) // 5)
        test_count = max(1, len(ordered) // 5)
        train_count = len(ordered) - validation_count - test_count
        if train_count < 1:
            raise MLError("EBM Lot split cannot make all three partitions nonempty")
        result = {lot_id: "train" for lot_id in ordered[:train_count]}
        validation_lots = ordered[train_count : train_count + validation_count]
        result.update({lot_id: "validation" for lot_id in validation_lots})
        result.update({lot_id: "test" for lot_id in ordered[train_count + validation_count :]})
        return result

    @staticmethod
    def _aligned_lot(
        lot_id: str, traces: Mapping[tuple[str, str], FDCTraceRecord]
    ) -> tuple[dict[str, dict[str, NDArray[np.float32]]], int]:
        import numpy as np

        expected = {(signal, pad) for signal in EBM_SIGNAL_ORDER for pad in EBM_PAD_ORDER}
        if set(traces) != expected:
            raise MLError(f"EBM Lot {lot_id} does not contain exactly seven-by-four traces")
        common_start = max(record.observations[0].timestamp for record in traces.values())
        common_end = min(record.observations[-1].timestamp for record in traces.values())
        start_second = math.ceil(common_start.timestamp())
        end_second = math.floor(common_end.timestamp())
        if start_second > end_second:
            raise MLError(f"EBM Lot {lot_id} has no common UTC one-Hz trace interval")
        # Equivalent to aligning the complete interval before the approved
        # feature transform keeps its final 256 seconds, without a large allocation.
        start_second = max(start_second, end_second - 255)
        grid: NDArray[np.float64] = np.arange(start_second, end_second + 1, dtype=np.float64)
        aligned: dict[str, dict[str, NDArray[np.float32]]] = {
            signal: {} for signal in EBM_SIGNAL_ORDER
        }
        for signal in EBM_SIGNAL_ORDER:
            for pad in EBM_PAD_ORDER:
                record = traces[(signal, pad)]
                timestamps: NDArray[np.float64] = np.asarray(
                    [item.timestamp.timestamp() for item in record.observations],
                    dtype=np.float64,
                )
                values: NDArray[np.float64] = np.asarray(
                    [item.value for item in record.observations], dtype=np.float64
                )
                if grid[0] < timestamps[0] or grid[-1] > timestamps[-1]:
                    raise MLError("EBM trace alignment would require extrapolation")
                interpolated = np.interp(grid, timestamps, values).astype(np.float32)
                if not np.isfinite(interpolated).all():
                    raise MLError("EBM one-Hz interpolation produced non-finite values")
                aligned[signal][pad] = interpolated
        return aligned, end_second

    def build(
        self, snapshot_set: EBMRawSnapshotSet, config: EBMDatasetBuilderConfig
    ) -> tuple[EBMDatasetMetadata, NDArray[np.float32]]:
        """Return validated metadata and unnormalized finite float32 features."""
        import numpy as np

        if not isinstance(snapshot_set, EBMRawSnapshotSet) or not isinstance(
            config, EBMDatasetBuilderConfig
        ):
            raise MLError("EBM DatasetBuilder requires validated inputs and configuration")
        traces_by_lot: dict[str, dict[tuple[str, str], FDCTraceRecord]] = {}
        targets_by_lot: dict[str, list[WaferTargetRecord]] = {}
        sample_ids: set[str] = set()
        lot_wafers: set[tuple[str, str]] = set()

        for resolved in sorted(
            snapshot_set.snapshots,
            key=lambda item: (item.reference.role, item.reference.snapshot_id),
        ):
            for raw in resolved.records:
                if not isinstance(raw, Mapping):
                    raise MLError("EBM raw records must be JSON objects")
                if resolved.reference.role == "fdc":
                    record = FDCTraceRecord.parse(raw)
                    if (
                        record.recipe_id != config.recipe_id
                        or record.segment_id != config.segment_id
                    ):
                        continue
                    key = (record.signal, record.pad)
                    lot = traces_by_lot.setdefault(record.lot_id, {})
                    if key in lot:
                        raise MLError("Duplicate or conflicting EBM FDC trace")
                    lot[key] = record
                elif resolved.reference.role == "wafer":
                    target = WaferTargetRecord.parse(raw)
                    sample = target.sample
                    if (
                        sample.recipe_id != config.recipe_id
                        or sample.segment_id != config.segment_id
                    ):
                        continue
                    lot_wafer = (sample.lot_id, sample.wafer_id)
                    if sample.sample_id in sample_ids or lot_wafer in lot_wafers:
                        raise MLError("Duplicate or conflicting EBM wafer target")
                    if len(sample_ids) >= EBM_MAX_ROWS:
                        raise MLError("EBM DatasetBuilder selected too many wafer targets")
                    sample_ids.add(sample.sample_id)
                    lot_wafers.add(lot_wafer)
                    targets_by_lot.setdefault(sample.lot_id, []).append(target)
                else:  # The validated set currently makes this unreachable.
                    raise MLError("Unsupported EBM RawSnapshot role")

        if not traces_by_lot or not targets_by_lot:
            raise MLError("EBM recipe/segment filtering selected no complete data")
        if set(traces_by_lot) != set(targets_by_lot):
            raise MLError("EBM selected traces and wafer targets contain orphan Lots")
        lot_ids = tuple(sorted(traces_by_lot))
        splits_by_lot = self._lot_splits(lot_ids, config.split_seed)

        aligned_lots = []
        for lot_id in lot_ids:
            aligned, aligned_end = self._aligned_lot(lot_id, traces_by_lot[lot_id])
            aligned_lots.append(aligned)
            for target in targets_by_lot[lot_id]:
                if target.event_timestamp.timestamp() < aligned_end:
                    raise MLError("EBM wafer target event_time precedes its aligned Lot trace")

        try:
            lot_features = FdcSpectrogramV1(
                EBM_SIGNAL_ORDER,
                config=SpectrogramConfig(log1p=config.log1p),
            ).transform_batch(aligned_lots)
        except (ValueError, OverflowError, FloatingPointError) as error:
            raise MLError("EBM FDC spectrogram generation failed") from error
        if (
            lot_features.dtype != np.dtype("float32")
            or lot_features.shape != (len(lot_ids), *EBM_FEATURE_SHAPE)
            or not np.isfinite(lot_features).all()
        ):
            raise MLError("EBM DatasetBuilder produced invalid Lot features")

        lot_feature_index = {lot_id: index for index, lot_id in enumerate(lot_ids)}
        samples: list[EBMSample] = []
        sample_features: list[NDArray[np.float32]] = []
        split_indices: dict[str, list[int]] = {"train": [], "validation": [], "test": []}
        for lot_id in lot_ids:
            targets = sorted(
                targets_by_lot[lot_id],
                key=lambda item: (item.sample.wafer_id, item.sample.sample_id),
            )
            for target in targets:
                index = len(samples)
                samples.append(target.sample)
                sample_features.append(lot_features[lot_feature_index[lot_id]])
                split_indices[splits_by_lot[lot_id]].append(index)

        metadata = EBMDatasetMetadata.parse(
            EBMDatasetMetadata(
                tuple(samples),
                tuple(split_indices["train"]),
                tuple(split_indices["validation"]),
                tuple(split_indices["test"]),
                spectrogram_log1p=config.log1p,
            ).to_dict()
        )
        spectrograms = np.ascontiguousarray(np.stack(sample_features), dtype=np.float32)
        if (
            spectrograms.shape != (len(samples), *EBM_FEATURE_SHAPE)
            or not np.isfinite(spectrograms).all()
        ):
            raise MLError("EBM DatasetBuilder produced invalid sample features")
        return metadata, spectrograms


__all__ = ["EBMDatasetBuilderV1"]
