"""Durable identities for the approved EBM DatasetBuilder."""

import json
import re
from dataclasses import dataclass
from typing import Literal

from src.domain.datasets.ebm_builder import (
    EBM_DATASET_BUILDER_ID,
    EBM_DATASET_BUILDER_VERSION,
    EBM_RAW_SNAPSHOT_ROLES,
    EBMDatasetBuilderConfig,
    EBMRawSnapshotReference,
)
from src.domain.ml import SchemaMismatch, canonical_json, digest

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_RECORDS = 8192
MAX_INPUT_SNAPSHOTS = 32
BuildState = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]


@dataclass(frozen=True)
class DatasetBuildSpec:
    builder_id: str
    builder_version: str
    dataset_id: str
    version: str
    inputs: tuple[EBMRawSnapshotReference, ...]
    config: EBMDatasetBuilderConfig

    def __post_init__(self) -> None:
        if (self.builder_id, self.builder_version) != (
            EBM_DATASET_BUILDER_ID,
            EBM_DATASET_BUILDER_VERSION,
        ):
            raise SchemaMismatch("Unsupported DatasetBuilder version")
        if self.dataset_id == "dataset-demo" or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key)
            or key == "latest"
            for key in (self.dataset_id, self.version)
        ):
            raise SchemaMismatch("Invalid or reserved dataset ID/version")
        validate_build_inputs(self.inputs)
        if not isinstance(self.config, EBMDatasetBuilderConfig):
            raise SchemaMismatch("Invalid DatasetBuilder configuration")

    def to_json(self) -> str:
        return canonical_json(
            {
                "builder_id": self.builder_id,
                "builder_version": self.builder_version,
                "dataset_id": self.dataset_id,
                "version": self.version,
                "inputs": [
                    item.to_dict()
                    for item in sorted(self.inputs, key=lambda item: (item.role, item.snapshot_id))
                ],
                "configuration": self.config.to_dict(),
            }
        )

    @classmethod
    def verified(cls, raw: str, expected_digest: str) -> "DatasetBuildSpec":
        if len(raw) > 16384 or digest(["dataset-build-spec-v1", raw]) != expected_digest:
            raise SchemaMismatch("Invalid stored dataset build specification")
        try:
            value = json.loads(raw)
            spec = cls(
                value["builder_id"],
                value["builder_version"],
                value["dataset_id"],
                value["version"],
                tuple(EBMRawSnapshotReference(**item) for item in value["inputs"]),
                EBMDatasetBuilderConfig(**value["configuration"]),
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise SchemaMismatch("Invalid stored dataset build specification") from None
        if spec.to_json() != raw:
            raise SchemaMismatch("Invalid stored dataset build specification")
        return spec


def validate_build_inputs(references: tuple[EBMRawSnapshotReference, ...]) -> None:
    if (
        not isinstance(references, tuple)
        or not 2 <= len(references) <= MAX_INPUT_SNAPSHOTS
        or any(not isinstance(item, EBMRawSnapshotReference) for item in references)
    ):
        raise SchemaMismatch("EBM DatasetBuilder requires a bounded snapshot reference set")
    if len({item.snapshot_id for item in references}) != len(references):
        raise SchemaMismatch("Duplicate EBM RawSnapshot references are forbidden")
    if {item.role for item in references} != EBM_RAW_SNAPSHOT_ROLES:
        raise SchemaMismatch("EBM DatasetBuilder requires both fdc and wafer RawSnapshots")


@dataclass(frozen=True)
class DatasetBuildJob:
    job_id: str
    tenant_id: str
    owner_id: str
    dataset_id: str
    version: str
    state: BuildState
    spec_json: str
    spec_digest: str
    created_at: float
    updated_at: float
    deadline: float
    attempt_count: int
    error_code: str | None
    content_digest: str | None
    receipt_digest: str | None


@dataclass(frozen=True)
class DatasetBuildClaim:
    job: DatasetBuildJob
    token: str
