"""Versioned contracts for the EBM RawSnapshot-to-Dataset builder."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER, EBMSample
from src.domain.datasets.models import DatasetVersion
from src.domain.ingestion.models import RawSnapshot
from src.domain.ml import MLError

EBM_RAW_INPUT_SCHEMA = "ebm-raw-input-v1"
EBM_DATASET_BUILDER_ID = "ebm-dataset-builder"
EBM_DATASET_BUILDER_VERSION = "1.0.0"
EBM_RAW_SNAPSHOT_ROLES = frozenset({"fdc", "wafer"})

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise MLError(f"Invalid EBM raw {name}")
    return value


def _segment(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise MLError("EBM raw segment_id must be a positive integer")
    return value


def _utc_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise MLError(f"EBM raw {name} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MLError(f"EBM raw {name} must be a timezone-aware ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MLError(f"EBM raw {name} must include a timezone")
    return parsed.astimezone(UTC)


def utc_text(value: datetime) -> str:
    """Return one canonical representation for metadata and digest stability."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EBMRawSnapshotReference:
    role: str
    snapshot_id: str
    manifest_digest: str

    def __post_init__(self) -> None:
        if self.role not in EBM_RAW_SNAPSHOT_ROLES:
            raise MLError("EBM raw snapshot role must be fdc or wafer")
        _identifier(self.snapshot_id, "snapshot_id")
        if not isinstance(self.manifest_digest, str) or not _DIGEST.fullmatch(self.manifest_digest):
            raise MLError("Invalid EBM raw snapshot manifest digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "snapshot_id": self.snapshot_id,
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True)
class EBMVerifiedRawSnapshot:
    """Raw metadata and decoded records returned by the trusted resolver."""

    reference: EBMRawSnapshotReference
    snapshot: RawSnapshot
    records: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reference, EBMRawSnapshotReference) or not isinstance(
            self.snapshot, RawSnapshot
        ):
            raise MLError("Invalid verified EBM RawSnapshot")
        if (
            self.reference.snapshot_id != self.snapshot.snapshot_id
            or self.reference.manifest_digest != self.snapshot.manifest_digest
            or self.snapshot.data_format != "jsonl"
            or self.snapshot.schema_version != "raw-json-object-v1"
        ):
            raise MLError("Resolved RawSnapshot does not match its approved reference")
        if (
            not isinstance(self.records, tuple)
            or len(self.records) != self.snapshot.record_count
            or not self.records
            or any(not isinstance(record, Mapping) for record in self.records)
        ):
            raise MLError("Resolved RawSnapshot records are incomplete")
        object.__setattr__(
            self,
            "records",
            tuple(deepcopy(dict(record)) for record in self.records),
        )


@dataclass(frozen=True)
class EBMRawSnapshotSet:
    """A small, tenant-consistent set containing both approved input roles."""

    snapshots: tuple[EBMVerifiedRawSnapshot, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshots, tuple)
            or not 2 <= len(self.snapshots) <= 32
            or any(not isinstance(item, EBMVerifiedRawSnapshot) for item in self.snapshots)
        ):
            raise MLError("EBM builder requires a bounded verified RawSnapshot set")
        if {item.reference.role for item in self.snapshots} != EBM_RAW_SNAPSHOT_ROLES:
            raise MLError("EBM builder requires both fdc and wafer RawSnapshots")
        identifiers = [item.snapshot.snapshot_id for item in self.snapshots]
        if len(set(identifiers)) != len(identifiers):
            raise MLError("Duplicate EBM RawSnapshot references are forbidden")
        if len({item.snapshot.tenant_id for item in self.snapshots}) != 1:
            raise MLError("EBM RawSnapshots must belong to one tenant")

    @property
    def tenant_id(self) -> str:
        tenant_id = self.snapshots[0].snapshot.tenant_id
        if not isinstance(tenant_id, str):
            raise MLError("Resolved RawSnapshot tenant is invalid")
        return tenant_id

    def references(self) -> tuple[dict[str, str], ...]:
        return tuple(
            item.reference.to_dict()
            for item in sorted(
                self.snapshots,
                key=lambda value: (value.reference.role, value.reference.snapshot_id),
            )
        )


@dataclass(frozen=True)
class EBMDatasetBuilderConfig:
    recipe_id: str
    segment_id: int
    split_seed: int = 43
    log1p: bool = True

    def __post_init__(self) -> None:
        _identifier(self.recipe_id, "recipe_id")
        _segment(self.segment_id)
        if type(self.split_seed) is not int or not 0 <= self.split_seed <= 2**31 - 1:
            raise MLError("EBM builder split_seed is invalid")
        if type(self.log1p) is not bool:
            raise MLError("EBM builder log1p must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "segment_id": self.segment_id,
            "split_seed": self.split_seed,
            "log1p": self.log1p,
        }


@dataclass(frozen=True)
class EBMDatasetBuildResult:
    dataset: DatasetVersion
    receipt_digest: str
    receipt_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset, DatasetVersion):
            raise MLError("Invalid EBM DatasetBuilder result")
        if not isinstance(self.receipt_digest, str) or not _DIGEST.fullmatch(self.receipt_digest):
            raise MLError("Invalid EBM DatasetBuilder receipt digest")
        if (
            not isinstance(self.receipt_ref, str)
            or not self.receipt_ref
            or len(self.receipt_ref) > 4096
            or "\x00" in self.receipt_ref
        ):
            raise MLError("Invalid EBM DatasetBuilder receipt reference")


@dataclass(frozen=True)
class FDCObservation:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class FDCTraceRecord:
    lot_id: str
    recipe_id: str
    segment_id: int
    signal: str
    pad: str
    observations: tuple[FDCObservation, ...]

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> FDCTraceRecord:
        fields = {
            "schema_version",
            "record_type",
            "lot_id",
            "recipe_id",
            "segment_id",
            "signal",
            "pad",
            "observations",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise MLError("FDC trace fields do not match ebm-raw-input-v1")
        if (
            value["schema_version"] != EBM_RAW_INPUT_SCHEMA
            or value["record_type"] != "fdc_trace"
            or value["signal"] not in EBM_SIGNAL_ORDER
            or value["pad"] not in EBM_PAD_ORDER
        ):
            raise MLError("Unsupported EBM FDC trace contract")
        raw_observations = value["observations"]
        if not isinstance(raw_observations, list) or not 2 <= len(raw_observations) <= 100_000:
            raise MLError("EBM FDC trace observations are empty or unbounded")
        observations = []
        previous: datetime | None = None
        for raw in raw_observations:
            if not isinstance(raw, Mapping) or set(raw) != {"timestamp", "value"}:
                raise MLError("EBM FDC observation fields are invalid")
            timestamp = _utc_timestamp(raw["timestamp"], "observation timestamp")
            number = raw["value"]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
            ):
                raise MLError("EBM FDC observation values must be finite numbers")
            if previous is not None and timestamp <= previous:
                raise MLError("EBM FDC observation timestamps must be strictly ordered")
            observations.append(FDCObservation(timestamp, float(number)))
            previous = timestamp
        return cls(
            _identifier(value["lot_id"], "lot_id"),
            _identifier(value["recipe_id"], "recipe_id"),
            _segment(value["segment_id"]),
            value["signal"],
            value["pad"],
            tuple(observations),
        )


@dataclass(frozen=True)
class WaferTargetRecord:
    sample: EBMSample
    event_timestamp: datetime

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> WaferTargetRecord:
        required = {
            "schema_version",
            "record_type",
            "sample_id",
            "lot_id",
            "recipe_id",
            "segment_id",
            "wafer_id",
            "event_time",
            "available_at",
            "cdf_target",
        }
        if (
            not isinstance(value, Mapping)
            or not required <= set(value)
            or set(value) - required - {"pad_counts"}
        ):
            raise MLError("Wafer target fields do not match ebm-raw-input-v1")
        if (
            value["schema_version"] != EBM_RAW_INPUT_SCHEMA
            or value["record_type"] != "wafer_target"
        ):
            raise MLError("Unsupported EBM wafer target contract")
        for name in ("sample_id", "lot_id", "recipe_id", "wafer_id"):
            _identifier(value[name], name)
        _segment(value["segment_id"])
        event = _utc_timestamp(value["event_time"], "event_time")
        available = value["available_at"]
        available_text = None
        if available is not None:
            available_timestamp = _utc_timestamp(available, "available_at")
            available_text = utc_text(available_timestamp)
        sample = EBMSample.parse(
            {
                "sample_id": value["sample_id"],
                "lot_id": value["lot_id"],
                "recipe_id": value["recipe_id"],
                "segment_id": value["segment_id"],
                "wafer_id": value["wafer_id"],
                "event_time": utc_text(event),
                "available_at": available_text,
                "cdf_target": value["cdf_target"],
                **({"pad_counts": value["pad_counts"]} if "pad_counts" in value else {}),
            }
        )
        return cls(sample, event)
