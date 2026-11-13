"""Explicit EBM dataset preparation; no background work or source acquisition."""

from __future__ import annotations

from src.application.datasets.ports import (
    EBMDatasetPublisher,
    EBMDatasetTransformer,
    PublishedRawSnapshotCatalog,
    RawSnapshotReader,
)
from src.domain.datasets.ebm_builder import (
    EBM_RAW_SNAPSHOT_ROLES,
    EBMDatasetBuilderConfig,
    EBMDatasetBuildResult,
    EBMRawSnapshotReference,
    EBMRawSnapshotSet,
    EBMVerifiedRawSnapshot,
)
from src.domain.ml import Actor, Conflict, Forbidden, MLError

_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_INPUT_RECORDS = 8192


class RawSnapshotSetResolver:
    """Resolve SQL-published metadata before reading verified immutable payloads."""

    def __init__(self, catalog: PublishedRawSnapshotCatalog, reader: RawSnapshotReader) -> None:
        self.catalog = catalog
        self.reader = reader

    def resolve(
        self, actor: Actor, references: tuple[EBMRawSnapshotReference, ...]
    ) -> EBMRawSnapshotSet:
        if not actor.permissions.intersection(
            {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
        ):
            raise Forbidden("Ingestion read permission is required")
        if (
            not isinstance(references, tuple)
            or not 2 <= len(references) <= 32
            or any(not isinstance(item, EBMRawSnapshotReference) for item in references)
        ):
            raise MLError("EBM DatasetBuilder requires a bounded snapshot reference set")
        if len({item.snapshot_id for item in references}) != len(references):
            raise MLError("Duplicate EBM RawSnapshot references are forbidden")
        if {item.role for item in references} != EBM_RAW_SNAPSHOT_ROLES:
            raise MLError("EBM DatasetBuilder requires both fdc and wafer RawSnapshots")

        approved = []
        total_bytes = 0
        total_records = 0
        for reference in sorted(references, key=lambda item: (item.role, item.snapshot_id)):
            snapshot = self.catalog.snapshot(actor, reference.snapshot_id)
            if snapshot.manifest_digest != reference.manifest_digest:
                raise Conflict("RawSnapshot manifest digest does not match its approved reference")
            if snapshot.tenant_id != actor.tenant_id:
                raise MLError("RawSnapshot tenant does not match the DatasetBuilder actor")
            total_bytes += snapshot.size_bytes
            total_records += snapshot.record_count
            if total_bytes > _MAX_INPUT_BYTES or total_records > _MAX_INPUT_RECORDS:
                raise MLError("EBM DatasetBuilder snapshot set exceeds its bounded input profile")
            approved.append((reference, snapshot))

        resolved = []
        for reference, snapshot in approved:
            records = self.reader.read_records(snapshot)
            resolved.append(EBMVerifiedRawSnapshot(reference, snapshot, records))
        return EBMRawSnapshotSet(tuple(resolved))


class EBMDatasetPreparationService:
    def __init__(
        self,
        resolver: RawSnapshotSetResolver,
        transformer: EBMDatasetTransformer,
        publisher: EBMDatasetPublisher,
    ) -> None:
        self.resolver = resolver
        self.transformer = transformer
        self.publisher = publisher

    def build(
        self,
        actor: Actor,
        dataset_id: str,
        version: str,
        references: tuple[EBMRawSnapshotReference, ...],
        config: EBMDatasetBuilderConfig,
    ) -> EBMDatasetBuildResult:
        actor.require("ml:dataset:manage")
        if not isinstance(config, EBMDatasetBuilderConfig):
            raise MLError("Invalid EBM DatasetBuilder configuration")
        snapshot_set = self.resolver.resolve(actor, references)
        if snapshot_set.tenant_id != actor.tenant_id:
            raise MLError("EBM DatasetBuilder inputs must belong to the actor tenant")
        metadata, spectrograms = self.transformer.build(snapshot_set, config)
        return self.publisher.publish(
            actor,
            dataset_id,
            version,
            snapshot_set,
            config,
            metadata,
            spectrograms,
        )
