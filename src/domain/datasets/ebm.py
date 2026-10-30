"""Framework-free metadata for the first, explicitly retrospective EBM CPU slice."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.domain.ml import MLError

EBM_DATASET_SCHEMA = "ebm-feature-snapshot-v1"
EBM_FEATURE_SHAPE = (7, 4, 9, 31)
EBM_SIGNAL_ORDER = (
    "HeadPressureZone1", "HeadPressureZone2", "HeadPressureZone3",
    "HeadPressureZone4", "HeadPressureZone5", "PadCondDownForce",
    "PadConditionerSweepTorque",
)
EBM_PAD_ORDER = ("P1", "P2", "P3", "P4")
EBM_MAX_ROWS = 4096


def _identifier(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 256 or any(ord(char) < 32 for char in value)):
        raise MLError(f"EBM {name} must be a nonempty identifier of at most 256 characters")
    return value


def _timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise MLError(f"EBM {name} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MLError(f"EBM {name} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MLError(f"EBM {name} must include a timezone")
    return value


@dataclass(frozen=True)
class EBMSample:
    sample_id: str
    lot_id: str
    recipe_id: str
    segment_id: int
    wafer_id: str
    event_time: str
    available_at: str | None
    cdf_target: float
    pad_counts: tuple[float, float, float, float] | None = None

    @classmethod
    def parse(cls, value: Any) -> "EBMSample":
        fields = {"sample_id", "lot_id", "recipe_id", "segment_id", "wafer_id",
                  "event_time", "available_at", "cdf_target"}
        if (not isinstance(value, dict) or not fields <= set(value)
                or set(value) - fields - {"pad_counts"}):
            raise MLError("EBM sample metadata fields do not match the versioned schema")
        segment = value["segment_id"]
        if type(segment) is not int or not 1 <= segment <= 2**31 - 1:
            raise MLError("EBM segment_id must be a positive integer")
        target = value["cdf_target"]
        if (isinstance(target, bool) or not isinstance(target, (int, float))
                or not math.isfinite(target) or not 0 <= target <= 1):
            raise MLError("EBM cdf_target must be finite and within [0,1]")
        event = _timestamp(value["event_time"], "event_time")
        available = value["available_at"]
        if available is not None:
            available = _timestamp(available, "available_at")
            if datetime.fromisoformat(available.replace("Z", "+00:00")) < datetime.fromisoformat(
                event.replace("Z", "+00:00")
            ):
                raise MLError("EBM available_at cannot precede event_time")
        counts = value.get("pad_counts")
        if counts is not None:
            if (not isinstance(counts, (list, tuple)) or len(counts) != 4 or any(
                isinstance(count, bool) or not isinstance(count, (int, float))
                or not math.isfinite(count) or count < 0 for count in counts
            )):
                raise MLError("EBM pad_counts must contain four finite nonnegative values")
            counts = (float(counts[0]), float(counts[1]), float(counts[2]), float(counts[3]))
        return cls(_identifier(value["sample_id"], "sample_id"),
            _identifier(value["lot_id"], "lot_id"),
            _identifier(value["recipe_id"], "recipe_id"), segment,
            _identifier(value["wafer_id"], "wafer_id"), event, available, float(target), counts)


@dataclass(frozen=True)
class EBMDatasetMetadata:
    """One row per wafer target; repeated Lot features retain PoC sample weighting.

    ``available_at=None`` explicitly means availability is unknown. This schema
    describes a retrospective replay and never establishes online eligibility.
    """

    samples: tuple[EBMSample, ...]
    train: tuple[int, ...]
    validation: tuple[int, ...]
    test: tuple[int, ...]
    schema_version: str = EBM_DATASET_SCHEMA
    signal_order: tuple[str, ...] = EBM_SIGNAL_ORDER
    pad_order: tuple[str, ...] = EBM_PAD_ORDER
    spectrogram_log1p: bool = True

    @classmethod
    def parse(cls, value: Any) -> "EBMDatasetMetadata":
        fields = {"schema_version", "signal_order", "pad_order", "feature_config",
                  "samples", "splits"}
        if not isinstance(value, dict) or set(value) != fields:
            raise MLError("EBM dataset metadata fields do not match the versioned schema")
        if value["schema_version"] != EBM_DATASET_SCHEMA:
            raise MLError("Unsupported EBM dataset schema")
        if value["signal_order"] != list(EBM_SIGNAL_ORDER):
            raise MLError("EBM signal order does not match the approved feature contract")
        if value["pad_order"] != list(EBM_PAD_ORDER):
            raise MLError("EBM pad order does not match the approved feature contract")
        feature = value["feature_config"]
        if (not isinstance(feature, dict) or set(feature) != {"version", "log1p"}
                or feature["version"] != "fdc_spectrogram_v1"
                or type(feature["log1p"]) is not bool):
            raise MLError("EBM requires approved feature_config with an explicit boolean log1p")
        rows = value["samples"]
        if not isinstance(rows, list) or not 3 <= len(rows) <= EBM_MAX_ROWS:
            raise MLError(f"EBM snapshot must contain 3 to {EBM_MAX_ROWS} samples")
        samples = tuple(EBMSample.parse(row) for row in rows)
        if len({row.sample_id for row in samples}) != len(samples):
            raise MLError("EBM sample_id values must be unique")
        if len({(row.lot_id, row.wafer_id) for row in samples}) != len(samples):
            raise MLError("EBM Lot/wafer sample mappings must be unique")
        splits = value["splits"]
        if not isinstance(splits, dict) or set(splits) != {"train", "validation", "test"}:
            raise MLError("EBM requires explicit train, validation and test splits")
        parsed_splits = []
        for name in ("train", "validation", "test"):
            raw = splits[name]
            if not isinstance(raw, list) or not raw:
                raise MLError("EBM splits must be nonempty index lists or boolean masks")
            if all(type(index) is bool for index in raw):
                if len(raw) != len(samples):
                    raise MLError("EBM split masks must match the sample count")
                indices = tuple(index for index, include in enumerate(raw) if include)
            elif all(type(index) is int for index in raw):
                indices = tuple(raw)
            else:
                raise MLError("EBM split entries must be all integers or all booleans")
            if not indices or any(index < 0 or index >= len(samples) for index in indices):
                raise MLError("EBM split indices must be nonempty and in range")
            parsed_splits.append(indices)
        combined = [index for split in parsed_splits for index in split]
        if len(combined) != len(samples) or set(combined) != set(range(len(samples))):
            raise MLError("EBM splits must be disjoint and cover every sample exactly once")
        lot_groups: dict[str, tuple[str, int, int]] = {}
        for split_number, indices in enumerate(parsed_splits):
            for index in indices:
                row = samples[index]
                group = (row.recipe_id, row.segment_id, split_number)
                if lot_groups.setdefault(row.lot_id, group) != group:
                    raise MLError("An EBM Lot must have one recipe, segment and split")
        return cls(samples, parsed_splits[0], parsed_splits[1], parsed_splits[2],
                   spectrogram_log1p=feature["log1p"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_order": list(self.signal_order),
            "pad_order": list(self.pad_order),
            "feature_config": {"version": "fdc_spectrogram_v1", "log1p": self.spectrogram_log1p},
            "samples": [dict(sample_id=row.sample_id, lot_id=row.lot_id,
                             recipe_id=row.recipe_id, segment_id=row.segment_id,
                             wafer_id=row.wafer_id, event_time=row.event_time,
                             available_at=row.available_at, cdf_target=row.cdf_target,
                             pad_counts=(list(row.pad_counts)
                                         if row.pad_counts is not None else None))
                        for row in self.samples],
            "splits": {"train": list(self.train), "validation": list(self.validation),
                       "test": list(self.test)},
        }
