"""Immutable, framework-free ingress DTOs. No paths or SDK objects come from HTTP."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any

from src.domain.ingestion.errors import IngestionError
from src.domain.ml import canonical_json, digest


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise IngestionError("Invalid ingress identifier")
    return value


def sha256(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise IngestionError("Invalid ingress content digest")
    return value


@dataclass(frozen=True)
class ExtractionLimits:
    max_bytes: int = 16 * 1024 * 1024
    max_records: int = 100_000
    max_line_bytes: int = 1024 * 1024
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_bytes, 128 * 1024 * 1024),
            (self.max_records, 1_000_000),
            (self.max_line_bytes, 4 * 1024 * 1024),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise IngestionError("Invalid ingestion extraction limits")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise IngestionError("Invalid ingestion timeout")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: dict[str, Any]) -> ExtractionLimits:
        if not isinstance(value, dict) or set(value) != set(cls().to_dict()):
            raise IngestionError("Invalid versioned extraction bounds")
        return cls(**value)


@dataclass(frozen=True)
class IngestionRequest:
    source_id: str
    source_version: str
    max_bytes: int | None = None
    max_records: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        identifier(self.source_id)
        identifier(self.source_version)
        for value, maximum in ((self.max_bytes, 128 * 1024 * 1024), (self.max_records, 1_000_000)):
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise IngestionError("Invalid requested extraction bounds")
        if self.timeout_seconds is not None:
            ExtractionLimits(timeout_seconds=self.timeout_seconds)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def bounded_by(self, approved: ExtractionLimits) -> ExtractionLimits:
        bounds = ExtractionLimits(
            self.max_bytes if self.max_bytes is not None else approved.max_bytes,
            self.max_records if self.max_records is not None else approved.max_records,
            approved.max_line_bytes,
            self.timeout_seconds if self.timeout_seconds is not None else approved.timeout_seconds,
        )
        if (
            bounds.max_bytes > approved.max_bytes
            or bounds.max_records > approved.max_records
            or bounds.timeout_seconds > approved.timeout_seconds
        ):
            raise IngestionError("Requested bounds exceed the approved source budget")
        return bounds


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    version: str
    tenant_id: str
    adapter_type: str
    adapter_version: str
    configuration_json: str
    limits: ExtractionLimits = ExtractionLimits()
    data_format: str = "jsonl"
    schema_version: str = "raw-json-object-v1"
    allowed_actor_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.source_id,
            self.version,
            self.tenant_id,
            self.adapter_type,
            self.adapter_version,
            self.data_format,
            self.schema_version,
        ):
            identifier(value)
        if (
            not isinstance(self.configuration_json, str)
            or len(self.configuration_json.encode()) > 16_384
        ):
            raise IngestionError("Source configuration exceeds its size limit")
        try:
            config = json.loads(self.configuration_json)
            if not isinstance(config, dict):
                raise ValueError
            object.__setattr__(self, "configuration_json", canonical_json(config))
        except (ValueError, TypeError, RecursionError) as error:
            raise IngestionError("Invalid approved source configuration") from error
        if not isinstance(self.limits, ExtractionLimits) or not isinstance(
            self.allowed_actor_ids, tuple
        ):
            raise IngestionError("Invalid source definition")
        for actor_id in self.allowed_actor_ids:
            identifier(actor_id)
        if len(self.allowed_actor_ids) > 256 or len(set(self.allowed_actor_ids)) != len(
            self.allowed_actor_ids
        ):
            raise IngestionError("Duplicate source actor grants")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "limits": self.limits.to_dict()}

    @property
    def configuration_digest(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def parse(cls, value: dict[str, Any]) -> SourceDefinition:
        expected = {
            "source_id",
            "version",
            "tenant_id",
            "adapter_type",
            "adapter_version",
            "configuration_json",
            "limits",
            "data_format",
            "schema_version",
            "allowed_actor_ids",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise IngestionError("Invalid versioned source definition")
        return cls(
            **{
                **value,
                "limits": ExtractionLimits.parse(value["limits"]),
                "allowed_actor_ids": tuple(value["allowed_actor_ids"]),
            }
        )


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_type: str
    version: str
    capabilities: tuple[str, ...] = ("batch_extract",)

    def __post_init__(self) -> None:
        identifier(self.adapter_type)
        identifier(self.version)
        if (
            not isinstance(self.capabilities, tuple)
            or not self.capabilities
            or len(set(self.capabilities)) != len(self.capabilities)
            or any(
                value not in {"batch_extract", "incremental_extract", "stream_consume"}
                for value in self.capabilities
            )
        ):
            raise IngestionError("Invalid source adapter capabilities")


@dataclass(frozen=True)
class IngestionExecution:
    tenant_id: str
    job_id: str
    attempt_id: str
    snapshot_id: str
    created_at: float

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.job_id, self.attempt_id, self.snapshot_id):
            identifier(value)
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
            or self.created_at <= 0
        ):
            raise IngestionError("Invalid ingestion creation time")


@dataclass(frozen=True)
class BatchExtraction:
    source: SourceDefinition
    execution: IngestionExecution
    limits: ExtractionLimits


@dataclass(frozen=True)
class StagedBatch:
    staging_ref: str
    tenant_id: str
    job_id: str
    attempt_id: str
    content_digest: str
    size_bytes: int
    record_count: int
    data_format: str
    schema_version: str
    source_revision: str

    def __post_init__(self) -> None:
        for value in (
            self.tenant_id,
            self.job_id,
            self.attempt_id,
            self.data_format,
            self.schema_version,
        ):
            identifier(value)
        sha256(self.content_digest)
        if (
            not isinstance(self.staging_ref, str)
            or not 1 <= len(self.staging_ref) <= 4096
            or type(self.size_bytes) is not int
            or self.size_bytes < 1
            or type(self.record_count) is not int
            or self.record_count < 1
            or not isinstance(self.source_revision, str)
            or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,256}", self.source_revision)
        ):
            raise IngestionError("Invalid staged batch evidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: dict[str, Any]) -> StagedBatch:
        try:
            return cls(**value)
        except (TypeError, ValueError) as error:
            raise IngestionError("Invalid staged batch evidence") from error


@dataclass(frozen=True)
class PublicationIntent:
    snapshot_id: str
    tenant_id: str
    job_id: str
    staged: StagedBatch
    manifest_json: str
    manifest_digest: str

    def __post_init__(self) -> None:
        for value in (self.snapshot_id, self.tenant_id, self.job_id):
            identifier(value)
        sha256(self.manifest_digest)
        if (
            len(self.manifest_json.encode()) > 32768
            or not isinstance(self.staged, StagedBatch)
            or self.staged.job_id != self.job_id
            or self.staged.tenant_id != self.tenant_id
        ):
            raise IngestionError("Invalid raw snapshot publication intent")
        try:
            manifest = json.loads(self.manifest_json)
        except (ValueError, TypeError) as error:
            raise IngestionError("Invalid raw snapshot manifest") from error
        if (
            not isinstance(manifest, dict)
            or canonical_json(manifest) != self.manifest_json
            or digest(manifest) != self.manifest_digest
        ):
            raise IngestionError("Raw snapshot manifest digest mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "staged": self.staged.to_dict()}

    @classmethod
    def parse(cls, value: dict[str, Any]) -> PublicationIntent:
        try:
            return cls(**{**value, "staged": StagedBatch.parse(value["staged"])})
        except (TypeError, KeyError) as error:
            raise IngestionError("Invalid publication intent") from error


@dataclass(frozen=True)
class RawSnapshot:
    snapshot_id: str
    tenant_id: str
    source_id: str
    source_version: str
    source_configuration_digest: str
    content_digest: str
    manifest_digest: str
    snapshot_ref: str
    data_format: str
    schema_version: str
    size_bytes: int
    record_count: int
    created_at: float
    job_id: str
    source_revision: str

    def __post_init__(self) -> None:
        for value in (
            self.snapshot_id,
            self.tenant_id,
            self.source_id,
            self.source_version,
            self.data_format,
            self.schema_version,
            self.job_id,
        ):
            identifier(value)
        for value in (self.source_configuration_digest, self.content_digest, self.manifest_digest):
            sha256(value)
        if (
            not isinstance(self.snapshot_ref, str)
            or not self.snapshot_ref
            or len(self.snapshot_ref) > 4096
            or type(self.size_bytes) is not int
            or self.size_bytes < 1
            or type(self.record_count) is not int
            or self.record_count < 1
            or isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(self.created_at)
            or self.created_at <= 0
            or not isinstance(self.source_revision, str)
            or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,256}", self.source_revision)
        ):
            raise IngestionError("Invalid raw snapshot metadata")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: dict[str, Any]) -> RawSnapshot:
        try:
            return cls(**value)
        except TypeError as error:
            raise IngestionError("Invalid raw snapshot metadata") from error


@dataclass(frozen=True)
class IngestionJob:
    job_id: str
    tenant_id: str
    owner_id: str
    source_id: str
    source_version: str
    source_digest: str
    limits_json: str
    state: str
    revision: int
    snapshot_id: str
    created_at: float
    deadline: float
    attempt_id: str | None = None
    attempt_count: int = 0
    owner_token: str | None = None
    lease_until: float = 0
    staged_json: str | None = None
    publication_intent_json: str | None = None
    result_json: str | None = None
    error_code: str | None = None
    next_retry_at: float = 0
    cancel_requested: bool = False


@dataclass(frozen=True)
class IngestionClaim:
    job: IngestionJob
    owner_token: str

    @property
    def execution(self) -> IngestionExecution:
        if self.job.attempt_id is None:
            raise IngestionError("Ingestion claim has no attempt identity")
        return IngestionExecution(
            self.job.tenant_id,
            self.job.job_id,
            self.job.attempt_id,
            self.job.snapshot_id,
            self.job.created_at,
        )
