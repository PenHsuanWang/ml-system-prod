"""Tenant-scoped immutable dataset metadata; import is an explicit operator action."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from uuid import uuid4

from sqlalchemy import insert, or_, select

from src.domain.datasets.models import DatasetSnapshot, DatasetVersion
from src.domain.ml import Actor, Conflict, MLError, NotFound, canonical_json, digest
from src.infrastructure.persistence.ml_database import Database, datasets
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit


class SQLDatasetCatalog:
    def __init__(self, database: Database, root: Path) -> None:
        self.database, self.root = database, root.resolve()

    def seed_builtin(self) -> None:
        checksum = "0d3b1bc213b97137c2046846ae72f600739146fdb61f8d2b0215558804ae1885"
        source = Path(__file__).parents[1]/"datasets"/"dataset-demo.json"
        if hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
            raise MLError("Packaged fixture no longer matches its immutable version")
        with self.database.transaction() as connection:
            if connection.execute(select(datasets.c.id).where(datasets.c.id == "builtin-dataset-demo-v1")).first():
                return
            connection.execute(insert(datasets).values(id="builtin-dataset-demo-v1", tenant_id="__public__",
                                                       dataset_key="dataset-demo", version="2026-10-28-v1", content_digest=checksum,
                                                       snapshot_ref="builtin:dataset-demo-v1", created_at=0.0,
                                                       summary_json=canonical_json({"features": ["sensor_a", "sensor_b"], "feature_schema_version": "1",
                                                                                    "samples": {"training": 384, "validation": 64, "test": 64}})))

    def resolve(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion:
        with self.database.transaction() as connection:
            rows = connection.execute(select(datasets).where(datasets.c.dataset_key == dataset_id,
                                                             datasets.c.version == version, or_(datasets.c.tenant_id == actor.tenant_id,
                                                                                                datasets.c.tenant_id == "__public__"))).mappings().all()
            if not rows:
                raise NotFound("Approved dataset version not found")
            row = next((item for item in rows if item["tenant_id"] == actor.tenant_id), rows[0])
            return DatasetVersion(row["dataset_key"], row["version"], row["tenant_id"],
                                  row["content_digest"], row["snapshot_ref"], row["summary_json"])

    def register(self, actor: Actor, dataset_id: str, version: str, source: Path) -> DatasetVersion:
        actor.require("ml:dataset:manage")
        if dataset_id == "dataset-demo" or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
                                                                item) or item == "latest" for item in (dataset_id, version)):
            raise MLError("Invalid or reserved dataset ID/version")
        if source.stat().st_size > 16*1024*1024:
            raise MLError("Dataset exceeds the CPU profile size limit")
        value = json.loads(source.read_bytes())
        snapshot = DatasetSnapshot.parse(value)
        data = canonical_json(value).encode()
        checksum = hashlib.sha256(data).hexdigest()
        directory = self.root/digest(actor.tenant_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory/(checksum+".json")
        if not destination.exists():
            temporary = directory/(uuid4().hex+".tmp")
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                destination.hardlink_to(temporary)
            except FileExistsError:
                pass
            finally:
                temporary.unlink()
        if hashlib.sha256(destination.read_bytes()).hexdigest() != checksum:
            raise MLError("Existing dataset content is corrupt")
        summary = canonical_json({"features": ["sensor_a", "sensor_b"], "feature_schema_version": "1",
                                  "samples": {"training": len(snapshot.train), "validation": len(snapshot.validation), "test": len(snapshot.test)}})
        result = DatasetVersion(dataset_id, version, actor.tenant_id,
                                checksum, str(destination), summary)
        with self.database.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            existing = connection.execute(select(datasets).where(datasets.c.tenant_id == actor.tenant_id,
                                                                 datasets.c.dataset_key == dataset_id, datasets.c.version == version)).mappings().first()
            if existing:
                if existing["content_digest"] != checksum:
                    raise Conflict("Dataset version is immutable")
                return result
            connection.execute(insert(datasets).values(id=str(uuid4()), tenant_id=actor.tenant_id,
                                                       dataset_key=dataset_id, version=version, content_digest=checksum,
                                                       snapshot_ref=str(destination), summary_json=summary, created_at=time.time()))
            record_audit(connection, actor, "dataset.register", dataset_id+"@"+version)
        return result

    @staticmethod
    def _copy_ebm_input(source: Path, destination: Path, maximum: int) -> None:
        source = source.absolute()
        if any(component.is_symlink() for component in (source, *source.parents)):
            raise MLError("EBM import symbolic links are forbidden")
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as incoming:
                info = os.fstat(incoming.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                    raise MLError("EBM import requires bounded regular files")
                with destination.open("xb") as outgoing:
                    total = 0
                    while chunk := incoming.read(min(1024 * 1024, maximum - total + 1)):
                        total += len(chunk)
                        if total > maximum:
                            raise MLError("EBM import exceeds its file size limit")
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
        except OSError as error:
            raise MLError("EBM import source cannot be read") from error

    def register_ebm(self, actor: Actor, dataset_id: str, version: str,
                     metadata_path: Path, npz_path: Path) -> DatasetVersion:
        """Import operator-selected files into an immutable, tenant-scoped snapshot."""
        actor.require("ml:dataset:manage")
        if dataset_id == "dataset-demo" or any(not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item
        ) or item == "latest" for item in (dataset_id, version)):
            raise MLError("Invalid or reserved dataset ID/version")
        # Scientific libraries load only for this explicit operator import.
        from src.infrastructure.datasets.ebm_snapshots import (
            MAX_ARRAY_BYTES, MAX_METADATA_BYTES, EBMFileDatasetSource,
        )
        from src.domain.datasets.ebm import EBM_DATASET_SCHEMA

        directory = self.root / digest(actor.tenant_id) / "ebm"
        store = EBMFileDatasetSource(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=directory) as staging_name:
            staging = Path(staging_name)
            self._copy_ebm_input(metadata_path, staging / "metadata.json", MAX_METADATA_BYTES)
            self._copy_ebm_input(npz_path, staging / "spectrograms.npz", MAX_ARRAY_BYTES + 32768)
            reference = store.import_files(staging / "metadata.json", staging / "spectrograms.npz")
        snapshot = store.load(reference)
        metadata = snapshot.metadata
        summary = canonical_json({"dataset_schema_version": EBM_DATASET_SCHEMA,
            "feature_schema_version": "ebm-spectrogram-input-v1",
            "feature_shape": [7, 4, 9, 31], "signal_order": metadata.signal_order,
            "pad_order": metadata.pad_order,
            "feature_config": metadata.to_dict()["feature_config"],
            "semantics": "retrospective_replay",
            "samples": {"training": len(metadata.train), "validation": len(metadata.validation),
                        "test": len(metadata.test)}})
        result = DatasetVersion(dataset_id, version, actor.tenant_id, reference["content_digest"],
                                reference["snapshot_ref"], summary)
        with self.database.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            existing = connection.execute(select(datasets).where(
                datasets.c.tenant_id == actor.tenant_id, datasets.c.dataset_key == dataset_id,
                datasets.c.version == version)).mappings().first()
            if existing:
                if (existing["content_digest"] != result.content_digest
                        or existing["summary_json"] != result.summary_json):
                    raise Conflict("Dataset version is immutable")
                return result
            connection.execute(insert(datasets).values(id=str(uuid4()), tenant_id=actor.tenant_id,
                dataset_key=dataset_id, version=version, content_digest=result.content_digest,
                snapshot_ref=result.snapshot_ref, summary_json=summary, created_at=time.time()))
            record_audit(connection, actor, "dataset.register", dataset_id + "@" + version)
        return result
