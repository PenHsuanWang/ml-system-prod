"""Explicit operator-only RawSnapshot-to-EBM-DatasetVersion build command."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bootstrap.ingestion_wiring import IngestionSettings
from bootstrap.ml_config import MLSettings
from src.application.datasets.builder import (
    EBMDatasetPreparationService,
    RawSnapshotSetResolver,
)
from src.domain.datasets.ebm_builder import (
    EBMDatasetBuilderConfig,
    EBMRawSnapshotReference,
)
from src.domain.ml import Actor
from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.plugins.features.ebm_dataset_builder_v1 import EBMDatasetBuilderV1


def _service(settings: MLSettings, raw_root: Path) -> tuple[EBMDatasetPreparationService, Database]:
    ingestion = IngestionSettings(database_url=settings.database_url, raw_root=raw_root)
    database = Database(settings.database_url)
    jobs = SQLIngestionJobRepository(database)
    store = LocalRawSnapshotStore(ingestion.raw_root)
    catalog = SQLDatasetCatalog(database, settings.dataset_root)
    service = EBMDatasetPreparationService(
        RawSnapshotSetResolver(jobs, store),
        EBMDatasetBuilderV1(),
        LocalEBMDatasetPublisher(catalog, settings.execution_root / "dataset-builder"),
    )
    return service, database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--recipe-id", required=True)
    parser.add_argument("--segment-id", type=int, required=True)
    parser.add_argument("--split-seed", type=int, default=43)
    parser.add_argument("--fdc-snapshot-id", required=True)
    parser.add_argument("--fdc-manifest-digest", required=True)
    parser.add_argument("--wafer-snapshot-id", required=True)
    parser.add_argument("--wafer-manifest-digest", required=True)
    args = parser.parse_args()

    settings = MLSettings.from_env()
    raw_root = Path(os.environ.get("ML_RAW_SNAPSHOT_ROOT", "var/raw-snapshots"))
    service, database = _service(settings, raw_root)
    actor = Actor(
        args.tenant_id,
        "dataset-builder-operator",
        frozenset({"ml:dataset:manage", "ml:ingestion:operate"}),
    )
    try:
        result = service.build(
            actor,
            args.dataset_id,
            args.version,
            (
                EBMRawSnapshotReference("fdc", args.fdc_snapshot_id, args.fdc_manifest_digest),
                EBMRawSnapshotReference(
                    "wafer", args.wafer_snapshot_id, args.wafer_manifest_digest
                ),
            ),
            EBMDatasetBuilderConfig(
                args.recipe_id,
                args.segment_id,
                split_seed=args.split_seed,
            ),
        )
        print(
            json.dumps(
                {
                    "dataset_id": result.dataset.dataset_id,
                    "version": result.dataset.version,
                    "content_digest": result.dataset.content_digest,
                    "receipt_digest": result.receipt_digest,
                    "receipt_ref": result.receipt_ref,
                },
                sort_keys=True,
            )
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
