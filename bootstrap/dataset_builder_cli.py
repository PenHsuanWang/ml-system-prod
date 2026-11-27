"""Explicit operator-only RawSnapshot-to-EBM-DatasetVersion build command."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bootstrap.dataset_build_wiring import build_preparation
from bootstrap.ml_config import MLSettings
from src.application.datasets.builder import EBMDatasetPreparationService
from src.domain.datasets.ebm_builder import (
    EBMDatasetBuilderConfig,
    EBMRawSnapshotReference,
)
from src.domain.ml import Actor
from src.infrastructure.persistence.ml_database import Database


def _service(settings: MLSettings, raw_root: Path) -> tuple[EBMDatasetPreparationService, Database]:
    database = Database(settings.database_url)
    return build_preparation(settings, database, raw_root), database


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
