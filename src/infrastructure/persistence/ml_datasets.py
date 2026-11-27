"""Tenant-scoped immutable dataset metadata; import is an explicit operator action."""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, exists, func, insert, or_, select
from sqlalchemy.engine import Connection

from src.domain.datasets.lineage import DatasetBuildReceipt
from src.domain.datasets.models import DatasetSnapshot, DatasetVersion
from src.domain.ml import Actor, Conflict, Forbidden, MLError, NotFound, canonical_json, digest
from src.domain.pagination import CollectionWindow
from src.infrastructure.ingestion.persistence.ingestion_jobs import (
    published_snapshot_query,
    snapshot_from_catalog_row,
)
from src.infrastructure.ingestion.persistence.schema import raw_snapshots
from src.infrastructure.persistence.dataset_lineage_schema import dataset_lineage
from src.infrastructure.persistence.ml_database import Database, datasets
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit
from src.infrastructure.storage_errors import raise_if_transient_storage_error


BuildPublication = Callable[[Connection, Actor, DatasetVersion, DatasetBuildReceipt], None]


class SQLDatasetCatalog:
    def __init__(
        self, database: Database, root: Path, *, complete_build: BuildPublication | None = None
    ) -> None:
        self.database, self.root = database, root.resolve()
        self.complete_build = complete_build

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
            raise_if_transient_storage_error(error)
            raise MLError("EBM import source cannot be read") from error

    def register_ebm(self, actor: Actor, dataset_id: str, version: str,
                     metadata_path: Path, npz_path: Path, *,
                     build_receipt: DatasetBuildReceipt | None = None) -> DatasetVersion:
        """Import operator-selected files into an immutable, tenant-scoped snapshot."""
        actor.require("ml:dataset:manage")
        if self.complete_build is not None and build_receipt is None:
            raise Conflict("A dataset build job requires verified lineage")
        if dataset_id == "dataset-demo" or any(not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", item
        ) or item == "latest" for item in (dataset_id, version)):
            raise MLError("Invalid or reserved dataset ID/version")
        # Scientific libraries load only for this explicit operator import.
        from src.domain.datasets.ebm import EBM_DATASET_SCHEMA
        from src.infrastructure.datasets.ebm_snapshots import (
            MAX_ARRAY_BYTES,
            MAX_METADATA_BYTES,
            EBMFileDatasetSource,
        )

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
                if build_receipt is not None:
                    established = connection.execute(select(dataset_lineage).where(
                        dataset_lineage.c.dataset_version_id == existing["id"])).mappings().first()
                    # Existing unqualified history is not assigned a new origin on retry.
                    if established is not None:
                        self._persist_lineage(connection, actor, existing["id"], result, build_receipt)
                        if self.complete_build is not None:
                            self.complete_build(connection, actor, result, build_receipt)
                    elif self.complete_build is not None:
                        raise Conflict("Existing dataset version has no verified build lineage")
                return result
            dataset_pk = str(uuid4())
            connection.execute(insert(datasets).values(id=dataset_pk, tenant_id=actor.tenant_id,
                dataset_key=dataset_id, version=version, content_digest=result.content_digest,
                snapshot_ref=result.snapshot_ref, summary_json=summary, created_at=time.time()))
            if build_receipt is not None:
                self._persist_lineage(connection, actor, dataset_pk, result, build_receipt)
                if self.complete_build is not None:
                    self.complete_build(connection, actor, result, build_receipt)
            record_audit(connection, actor, "dataset.register", dataset_id + "@" + version)
        return result


    @staticmethod
    def _receipt_identity(receipt: DatasetBuildReceipt, dataset: DatasetVersion) -> dict[str, Any]:
        value = receipt.document()
        if (value["tenant_id"] != dataset.tenant_id
                or value["dataset"] != {"id": dataset.dataset_id, "version": dataset.version,
                                        "content_digest": dataset.content_digest}):
            raise Conflict("Build receipt does not match the immutable dataset identity")
        return value

    @staticmethod
    def _published_inputs(connection: Connection, actor: Actor,
                          receipt: DatasetBuildReceipt) -> bool:
        inputs = receipt.document()["inputs"]
        identifiers = [item["snapshot_id"] for item in inputs]
        rows = connection.execute(published_snapshot_query(actor).where(
            raw_snapshots.c.id.in_(identifiers))).mappings()
        snapshots = {row["id"]: snapshot_from_catalog_row(row) for row in rows}
        if len(snapshots) != len(identifiers):
            return False
        for item in inputs:
            snapshot = snapshots[item["snapshot_id"]]
            if any(getattr(snapshot, key) != value for key, value in item.items() if key != "role"):
                raise Conflict("Build receipt disagrees with published input metadata")
        return True

    def _persist_lineage(self, connection: Connection, actor: Actor, dataset_pk: str,
                         dataset: DatasetVersion, receipt: DatasetBuildReceipt) -> None:
        value = self._receipt_identity(receipt, dataset)
        if not actor.permissions.intersection({"ml:ingest", "ml:ingestion:read",
                                               "ml:ingestion:operate"}):
            raise Forbidden("Published input visibility is required to register lineage")
        if not self._published_inputs(connection, actor, receipt):
            raise NotFound("Published build inputs are unavailable")
        existing = connection.execute(select(dataset_lineage).where(
            dataset_lineage.c.dataset_version_id == dataset_pk)).mappings().first()
        if existing is not None:
            if (existing["tenant_id"] != dataset.tenant_id
                    or existing["content_digest"] != dataset.content_digest
                    or existing["builder_id"] != value["builder"]["id"]
                    or existing["builder_version"] != value["builder"]["version"]
                    or existing["receipt_digest"] != receipt.receipt_digest
                    or existing["receipt_json"] != receipt.receipt_json):
                raise Conflict("Dataset version provenance is immutable")
            return
        connection.execute(insert(dataset_lineage).values(
            dataset_version_id=dataset_pk, tenant_id=actor.tenant_id,
            content_digest=dataset.content_digest,
            builder_id=value["builder"]["id"], builder_version=value["builder"]["version"],
            receipt_digest=receipt.receipt_digest, receipt_json=receipt.receipt_json,
            created_at=time.time()))
        record_audit(connection, actor, "dataset.lineage.register", receipt.receipt_digest)

    @staticmethod
    def _visible_versions(actor: Actor) -> Any:
        shadow = datasets.alias("private_dataset_shadow")
        private_exists = exists(select(shadow.c.id).where(
            shadow.c.tenant_id == actor.tenant_id, shadow.c.dataset_key == datasets.c.dataset_key,
            shadow.c.version == datasets.c.version))
        return or_(datasets.c.tenant_id == actor.tenant_id,
                   and_(datasets.c.tenant_id == "__public__", ~private_exists))

    @staticmethod
    def _time_window(statement: Any, window: CollectionWindow) -> Any:
        if window.created_after is not None:
            statement = statement.where(datasets.c.created_at > window.created_after)
        if window.created_before is not None:
            statement = statement.where(datasets.c.created_at < window.created_before)
        return statement

    def list_families(self, actor: Actor, window: CollectionWindow,
                      dataset_id: str | None = None) -> list[dict[str, Any]]:
        statement = select(datasets.c.dataset_key, func.count().label("version_count"),
                           func.max(datasets.c.created_at).label("last_published_at")).where(
                               self._visible_versions(actor))
        if dataset_id is not None:
            statement = statement.where(datasets.c.dataset_key == dataset_id)
        statement = self._time_window(statement, window).group_by(datasets.c.dataset_key).order_by(
            datasets.c.dataset_key).limit(window.limit + 1).offset(window.offset)
        with self.database.transaction() as connection:
            return [{"dataset_id": row.dataset_key, "version_count": row.version_count,
                     "last_published_at": row.last_published_at}
                    for row in connection.execute(statement)]

    def _metadata_query(self, actor: Actor) -> Any:
        return select(
            datasets.c.id, datasets.c.tenant_id, datasets.c.dataset_key, datasets.c.version,
            datasets.c.content_digest, datasets.c.created_at, datasets.c.summary_json,
            dataset_lineage.c.builder_id, dataset_lineage.c.builder_version,
            dataset_lineage.c.receipt_digest, dataset_lineage.c.receipt_json,
            dataset_lineage.c.content_digest.label("lineage_content_digest"),
        ).select_from(datasets.outerjoin(dataset_lineage, and_(
            dataset_lineage.c.dataset_version_id == datasets.c.id,
            dataset_lineage.c.tenant_id == datasets.c.tenant_id,
        ))).where(self._visible_versions(actor))

    @staticmethod
    def _public_metadata(row: Any) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        result = {
            "dataset_id": row["dataset_key"], "version": row["version"],
            "visibility": "public" if row["tenant_id"] == "__public__" else "tenant",
            "content_digest": row["content_digest"], "created_at": row["created_at"],
            "lineage_status": "recorded" if row["receipt_digest"] is not None else "incomplete",
            "builder": {"id": row["builder_id"], "version": row["builder_version"]}
                       if row["receipt_digest"] is not None else None,
        }
        for key in ("dataset_schema_version", "feature_schema_version", "feature_shape",
                    "features", "signal_order", "pad_order", "feature_config", "samples"):
            result[key] = summary.get(key)
        return result

    def list_versions(self, actor: Actor, dataset_id: str,
                      window: CollectionWindow) -> list[dict[str, Any]]:
        statement = self._time_window(self._metadata_query(actor), window).where(
            datasets.c.dataset_key == dataset_id).order_by(datasets.c.version).limit(
                window.limit + 1).offset(window.offset)
        with self.database.transaction() as connection:
            return [self._public_metadata(row) for row in connection.execute(statement).mappings()]

    def version_metadata(self, actor: Actor, dataset_id: str, version: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            row = connection.execute(self._metadata_query(actor).where(
                datasets.c.dataset_key == dataset_id, datasets.c.version == version)
                .limit(1)).mappings().first()
            if row is None:
                raise NotFound("Dataset version not found")
            return self._public_metadata(row)

    def lineage(self, actor: Actor, dataset_id: str, version: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            row = connection.execute(self._metadata_query(actor).where(
                datasets.c.dataset_key == dataset_id, datasets.c.version == version)
                .limit(1)).mappings().first()
            if row is None:
                raise NotFound("Dataset version not found")
            result: dict[str, Any] = {"dataset_id": dataset_id, "version": version,
                      "content_digest": row["content_digest"], "status": "incomplete",
                      "reason": "legacy_metadata_missing", "receipt_digest": None, "receipt": None}
            if row["receipt_json"] is None:
                return result
            try:
                receipt = DatasetBuildReceipt(row["receipt_json"], row["receipt_digest"])
                value = receipt.document()
                if (value["tenant_id"] != row["tenant_id"]
                        or value["dataset"] != {"id": dataset_id, "version": version,
                                               "content_digest": row["content_digest"]}
                        or row["lineage_content_digest"] != row["content_digest"]
                        or value["builder"] != {"id": row["builder_id"],
                                                "version": row["builder_version"]}):
                    raise Conflict("Receipt identity mismatch")
                if (not actor.permissions.intersection(
                        {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"})
                        or not self._published_inputs(connection, actor, receipt)):
                    return {**result, "status": "restricted", "reason": "input_access_required"}
            except (MLError, ValueError, TypeError, KeyError):
                return {**result, "reason": "receipt_integrity_failed"}
            return {**result, "status": "complete", "reason": None,
                    "receipt_digest": receipt.receipt_digest, "receipt": value}
