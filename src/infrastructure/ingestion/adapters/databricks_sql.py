"""Approved Databricks batch acquisition; model-specific preparation stays downstream."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Protocol

from src.domain.ingestion.errors import SourceRejected
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    SourceDefinition,
    StagedBatch,
)
from src.infrastructure.ingestion.adapters.databricks_contract import (
    ConnectionConfig,
    Template,
    validate_source,
)
from src.infrastructure.ingestion.storage.raw_snapshot_writer import CheckControl


class ByteStreamClient(Protocol):
    def stream(
        self,
        connection: ConnectionConfig,
        template: Template,
        limits: ExtractionLimits,
        control: CheckControl,
    ) -> Iterable[bytes]: ...


class BatchStager(Protocol):
    def stage(
        self, extraction: BatchExtraction, chunks: Iterable[bytes], control: CheckControl
    ) -> StagedBatch: ...
    def discard(self, staged: StagedBatch) -> None: ...


class DatabricksSQLBatchAdapter:
    def __init__(
        self,
        connections: Mapping[str, ConnectionConfig],
        templates: Mapping[str, Template],
        client: ByteStreamClient,
        stager: BatchStager,
    ) -> None:
        self.connections = MappingProxyType(dict(connections))
        self.templates = MappingProxyType(dict(templates))
        self.client, self.stager = client, stager

    def validate_source(self, source: SourceDefinition) -> None:
        validate_source(source, self.templates, self.connections)

    def acquire(self, extraction: BatchExtraction, control: CheckControl) -> StagedBatch:
        template = validate_source(extraction.source, self.templates, self.connections)
        approved, limits = extraction.source.limits, extraction.limits
        if (
            extraction.execution.tenant_id != extraction.source.tenant_id
            or limits.max_bytes > approved.max_bytes
            or limits.max_records > approved.max_records
            or limits.max_line_bytes > approved.max_line_bytes
            or limits.timeout_seconds > approved.timeout_seconds
        ):
            raise SourceRejected("Databricks extraction exceeds its approved source contract")
        connection = self.connections[template.connection_ref]
        control.check()
        chunks = iter(self.client.stream(connection, template, extraction.limits, control))
        staged = None
        try:
            staged = self.stager.stage(extraction, chunks, control)
            control.check()
            return replace(
                staged,
                source_revision="delta:"
                + template.digest(connection)
                + ":"
                + str(template.delta_version),
            )
        except BaseException:
            if staged is not None:
                self.stager.discard(staged)
            raise
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
