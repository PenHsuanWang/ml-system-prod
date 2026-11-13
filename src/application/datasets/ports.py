from typing import Any, Protocol

from src.domain.datasets.ebm import EBMDatasetMetadata
from src.domain.datasets.ebm_builder import (
    EBMDatasetBuilderConfig,
    EBMDatasetBuildResult,
    EBMRawSnapshotSet,
)
from src.domain.datasets.models import DatasetVersion
from src.domain.ingestion.models import RawSnapshot
from src.domain.ml import Actor


class DatasetCatalog(Protocol):
    def resolve(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion: ...


class PublishedRawSnapshotCatalog(Protocol):
    def snapshot(self, actor: Actor, snapshot_id: str) -> RawSnapshot: ...


class RawSnapshotReader(Protocol):
    def read_records(self, snapshot: RawSnapshot) -> tuple[dict[str, Any], ...]: ...


class EBMDatasetTransformer(Protocol):
    def build(
        self, snapshot_set: EBMRawSnapshotSet, config: EBMDatasetBuilderConfig
    ) -> tuple[EBMDatasetMetadata, Any]: ...


class EBMDatasetPublisher(Protocol):
    def publish(
        self,
        actor: Actor,
        dataset_id: str,
        version: str,
        snapshot_set: EBMRawSnapshotSet,
        config: EBMDatasetBuilderConfig,
        metadata: EBMDatasetMetadata,
        spectrograms: Any,
    ) -> EBMDatasetBuildResult: ...
