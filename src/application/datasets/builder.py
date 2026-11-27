"""Explicit EBM dataset preparation; no background work or source acquisition."""

from __future__ import annotations

from collections.abc import Callable

from src.application.datasets.ports import (
    EBMDatasetPublisher,
    EBMDatasetTransformer,
    PublishedRawSnapshotCatalog,
    RawSnapshotReader,
)
from src.domain.datasets.build_jobs import MAX_INPUT_BYTES, MAX_INPUT_RECORDS, validate_build_inputs
from src.domain.datasets.ebm_builder import (
    EBMDatasetBuilderConfig,
    EBMDatasetBuildResult,
    EBMRawSnapshotReference,
    EBMRawSnapshotSet,
    EBMVerifiedRawSnapshot,
)
from src.domain.ingestion.models import RawSnapshot
from src.domain.ml import Actor, Conflict, Forbidden, MLError


def approve_build_inputs(
    catalog: PublishedRawSnapshotCatalog,
    actor: Actor,
    references: tuple[EBMRawSnapshotReference, ...],
) -> list[tuple[EBMRawSnapshotReference, RawSnapshot]]:
    if not actor.permissions.intersection(
        {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
    ):
        raise Forbidden("Ingestion read permission is required")
    validate_build_inputs(references)
    approved = []
    total_bytes = total_records = 0
    for reference in sorted(references, key=lambda item: (item.role, item.snapshot_id)):
        snapshot = catalog.snapshot(actor, reference.snapshot_id)
        if snapshot.manifest_digest != reference.manifest_digest:
            raise Conflict("RawSnapshot manifest digest does not match its approved reference")
        if snapshot.tenant_id != actor.tenant_id:
            raise MLError("RawSnapshot tenant does not match the DatasetBuilder actor")
        total_bytes += snapshot.size_bytes
        total_records += snapshot.record_count
        if total_bytes > MAX_INPUT_BYTES or total_records > MAX_INPUT_RECORDS:
            raise MLError("EBM DatasetBuilder snapshot set exceeds its bounded input profile")
        approved.append((reference, snapshot))
    return approved


class RawSnapshotSetResolver:
    """Resolve SQL-published metadata before reading verified immutable payloads."""

    def __init__(self, catalog: PublishedRawSnapshotCatalog, reader: RawSnapshotReader) -> None:
        self.catalog = catalog
        self.reader = reader

    def resolve(
        self, actor: Actor, references: tuple[EBMRawSnapshotReference, ...]
    ) -> EBMRawSnapshotSet:
        resolved = []
        for reference, snapshot in approve_build_inputs(self.catalog, actor, references):
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
        *,
        authorize_publication: Callable[[], Actor] | None = None,
    ) -> EBMDatasetBuildResult:
        actor.require("ml:dataset:manage")
        if not isinstance(config, EBMDatasetBuilderConfig):
            raise MLError("Invalid EBM DatasetBuilder configuration")
        snapshot_set = self.resolver.resolve(actor, references)
        if snapshot_set.tenant_id != actor.tenant_id:
            raise MLError("EBM DatasetBuilder inputs must belong to the actor tenant")
        metadata, spectrograms = self.transformer.build(snapshot_set, config)
        if authorize_publication is not None:
            current = authorize_publication()
            if (current.tenant_id, current.actor_id) != (actor.tenant_id, actor.actor_id):
                raise Forbidden("DatasetBuilder principal changed")
            current.require("ml:dataset:manage")
            actor = current
        return self.publisher.publish(
            actor,
            dataset_id,
            version,
            snapshot_set,
            config,
            metadata,
            spectrograms,
        )
