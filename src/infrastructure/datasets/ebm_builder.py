"""Verified EBM artifact publication, immutable receipt, then catalog registration."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from src.domain.datasets.ebm import EBMDatasetMetadata
from src.domain.datasets.ebm_builder import (
    EBM_DATASET_BUILDER_ID,
    EBM_DATASET_BUILDER_VERSION,
    EBM_RAW_INPUT_SCHEMA,
    EBMDatasetBuilderConfig,
    EBMDatasetBuildResult,
    EBMRawSnapshotSet,
)
from src.domain.ml import Actor, MLError, canonical_json
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.ingestion.storage.raw_snapshot_writer import (
    directory_fd,
    relative_file_fd,
)
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

_DATASET_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_MAX_RECEIPT_BYTES = 64 * 1024


class LocalEBMDatasetPublisher:
    """Keep build evidence separate from the existing strict EBM snapshot schema."""

    def __init__(self, catalog: SQLDatasetCatalog, build_root: Path) -> None:
        self.catalog = catalog
        self.build_root = build_root.absolute()
        self.artifacts = EBMFileDatasetSource(self.build_root / "artifacts")
        self.receipt_root = self.build_root / "receipts"

    @staticmethod
    def _validate_identity(dataset_id: str, version: str) -> None:
        if dataset_id == "dataset-demo" or any(
            not isinstance(item, str) or not _DATASET_IDENTIFIER.fullmatch(item) or item == "latest"
            for item in (dataset_id, version)
        ):
            raise MLError("Invalid or reserved dataset ID/version")

    def _write_receipt(self, value: dict[str, Any]) -> tuple[str, str]:
        data = canonical_json(value).encode("utf-8")
        if len(data) > _MAX_RECEIPT_BYTES:
            raise MLError("EBM DatasetBuilder receipt exceeds its size limit")
        receipt_digest = hashlib.sha256(data).hexdigest()
        final_name = receipt_digest + ".json"
        temporary_name = ".receipt-" + uuid4().hex + ".tmp"
        with directory_fd(self.receipt_root, create=True) as descriptor:
            temporary = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            try:
                with os.fdopen(temporary, "wb", closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary_name,
                        final_name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                    os.fsync(descriptor)
                except FileExistsError:
                    with relative_file_fd(self.receipt_root, final_name) as existing:
                        with os.fdopen(os.dup(existing), "rb") as stream:
                            if stream.read(_MAX_RECEIPT_BYTES + 1) != data:
                                raise MLError("Existing EBM build receipt is corrupt") from None
            finally:
                os.close(temporary)
                try:
                    os.unlink(temporary_name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
        return receipt_digest, str(self.receipt_root / final_name)

    @staticmethod
    def _receipt(
        actor: Actor,
        dataset_id: str,
        version: str,
        snapshot_set: EBMRawSnapshotSet,
        config: EBMDatasetBuilderConfig,
        content_digest: str,
    ) -> dict[str, Any]:
        inputs = []
        for item in sorted(
            snapshot_set.snapshots,
            key=lambda value: (value.reference.role, value.snapshot.snapshot_id),
        ):
            snapshot = item.snapshot
            inputs.append(
                {
                    "role": item.reference.role,
                    "snapshot_id": snapshot.snapshot_id,
                    "manifest_digest": snapshot.manifest_digest,
                    "content_digest": snapshot.content_digest,
                    "source_id": snapshot.source_id,
                    "source_version": snapshot.source_version,
                    "source_configuration_digest": snapshot.source_configuration_digest,
                    "source_revision": snapshot.source_revision,
                    "data_format": snapshot.data_format,
                    "schema_version": snapshot.schema_version,
                    "size_bytes": snapshot.size_bytes,
                    "record_count": snapshot.record_count,
                }
            )
        return {
            "receipt_version": "ebm-dataset-build-receipt-v1",
            "builder": {
                "id": EBM_DATASET_BUILDER_ID,
                "version": EBM_DATASET_BUILDER_VERSION,
            },
            "tenant_id": actor.tenant_id,
            "dataset": {
                "id": dataset_id,
                "version": version,
                "content_digest": content_digest,
            },
            "inputs": inputs,
            "configuration": {
                **config.to_dict(),
                "raw_input_schema": EBM_RAW_INPUT_SCHEMA,
                "alignment_policy": "common_interval_utc_1hz_linear_no_extrapolation_v1",
                "split_policy": "sha256_lot_approximately_60_20_20_v1",
                "feature_version": "fdc_spectrogram_v1",
            },
        }

    def publish(
        self,
        actor: Actor,
        dataset_id: str,
        version: str,
        snapshot_set: EBMRawSnapshotSet,
        config: EBMDatasetBuilderConfig,
        metadata: EBMDatasetMetadata,
        spectrograms: NDArray[np.float32],
    ) -> EBMDatasetBuildResult:
        self._validate_identity(dataset_id, version)
        if snapshot_set.tenant_id != actor.tenant_id:
            raise MLError("EBM build inputs do not belong to the registering tenant")
        reference = self.artifacts.publish(metadata.to_dict(), spectrograms)
        verified = self.artifacts.load(reference)
        if verified.metadata != metadata:
            raise MLError("Published EBM build metadata failed verification")
        receipt_digest, receipt_ref = self._write_receipt(
            self._receipt(
                actor,
                dataset_id,
                version,
                snapshot_set,
                config,
                verified.content_digest,
            )
        )
        source = Path(reference["snapshot_ref"]).parent
        dataset = self.catalog.register_ebm(
            actor,
            dataset_id,
            version,
            source / "metadata.json",
            source / "spectrograms.npz",
        )
        # Registration is deliberately the terminal side effect: register_ebm
        # validates the same files and commits the catalog row atomically, so no
        # later operation can turn a visible DatasetVersion into a failed build.
        return EBMDatasetBuildResult(dataset, receipt_digest, receipt_ref)
