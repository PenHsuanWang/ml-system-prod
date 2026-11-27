"""Compose the existing DatasetBuilder for operator and queue execution."""

from collections.abc import Callable
from functools import partial
from pathlib import Path

from bootstrap.ml_config import MLSettings
from src.application.datasets.build_jobs import DatasetBuildWorker
from src.application.datasets.builder import EBMDatasetPreparationService, RawSnapshotSetResolver
from src.domain.datasets.build_jobs import DatasetBuildClaim
from src.domain.ingestion.errors import SnapshotInvalid
from src.domain.ml import Actor
from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.persistence.dataset_build_jobs import SQLDatasetBuildJobs
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_datasets import BuildPublication, SQLDatasetCatalog
from src.infrastructure.storage_errors import (
    classify_storage_errors,
    raise_if_transient_storage_error,
)
from src.plugins.features.ebm_dataset_builder_v1 import EBMDatasetBuilderV1


def build_preparation(
    settings: MLSettings,
    database: Database,
    raw_root: Path,
    *,
    complete_build: BuildPublication | None = None,
) -> EBMDatasetPreparationService:
    catalog = SQLDatasetCatalog(database, settings.dataset_root, complete_build=complete_build)
    return EBMDatasetPreparationService(
        RawSnapshotSetResolver(
            SQLIngestionJobRepository(database),
            LocalRawSnapshotStore(raw_root, create_directories=False),
        ),
        EBMDatasetBuilderV1(),
        LocalEBMDatasetPublisher(catalog, settings.execution_root / "dataset-builder"),
    )


def build_worker(
    settings: MLSettings,
    queue: SQLDatasetBuildJobs,
    raw_root: Path,
    resolve_actor: Callable[[str, str], Actor],
) -> DatasetBuildWorker:
    def preparation(claim: DatasetBuildClaim) -> EBMDatasetPreparationService:
        with classify_storage_errors():
            try:
                return build_preparation(
                    settings, queue.db, raw_root, complete_build=partial(queue.complete, claim)
                )
            except SnapshotInvalid as error:
                raise_if_transient_storage_error(error.__cause__)
                raise

    return DatasetBuildWorker(queue, preparation, resolve_actor)
