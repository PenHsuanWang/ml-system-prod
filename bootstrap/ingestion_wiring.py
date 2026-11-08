"""Explicit source construction and registration; no source selection in use cases."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.application.ingestion.ports import AdapterRegistration
from src.application.ingestion.registry import ApprovedSourceAdapterRegistry
from src.application.ingestion.service import IngestionService
from src.application.ingestion.supervisor import IngestionSupervisor
from src.domain.ingestion.models import AdapterDescriptor
from src.infrastructure.ingestion.adapters.file_batch import FileBatchAdapter
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository
from src.infrastructure.ingestion.persistence.source_catalog import SQLSourceCatalog
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.persistence.ml_database import Database


def ingress_enabled() -> bool:
    value = os.environ.get("ML_INGESTION_ENABLED", "false").lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("ML_INGESTION_ENABLED must be an explicit boolean")
    return value in {"true", "1"}


@dataclass(frozen=True)
class IngestionSettings:
    database_url: str = "sqlite:///./var/ml.db"
    raw_root: Path = Path("var/raw-snapshots")
    file_roots: tuple[tuple[str, Path], ...] = ()
    lease_seconds: float = 30
    retry_delay: float = 1
    max_attempts: int = 3
    reconcile_limit: int = 10
    queue_timeout: float = 600
    max_pending: int = 100
    poll_seconds: float = 1

    @classmethod
    def from_env(cls) -> IngestionSettings:
        raw = os.environ.get("ML_INGESTION_FILE_ROOTS_JSON", "{}")
        if len(raw.encode()) > 65536:
            raise ValueError("Approved file roots configuration is too large")
        roots = json.loads(raw)
        if (
            not isinstance(roots, dict)
            or len(roots) > 128
            or any(
                not isinstance(key, str) or not isinstance(value, str) or not value
                for key, value in roots.items()
            )
        ):
            raise ValueError("Approved file roots must be a server-side JSON mapping")
        return cls(
            database_url=os.environ.get("ML_DATABASE_URL", "sqlite:///./var/ml.db"),
            raw_root=Path(os.environ.get("ML_RAW_SNAPSHOT_ROOT", "var/raw-snapshots")),
            file_roots=tuple((key, Path(value)) for key, value in roots.items()),
            lease_seconds=float(os.environ.get("ML_INGESTION_LEASE_SECONDS", "30")),
            retry_delay=float(os.environ.get("ML_INGESTION_RETRY_DELAY", "1")),
            max_attempts=int(os.environ.get("ML_INGESTION_MAX_ATTEMPTS", "3")),
            reconcile_limit=int(os.environ.get("ML_INGESTION_RECONCILE_LIMIT", "10")),
            queue_timeout=float(os.environ.get("ML_INGESTION_QUEUE_TIMEOUT", "600")),
            max_pending=int(os.environ.get("ML_INGESTION_MAX_PENDING", "100")),
            poll_seconds=float(os.environ.get("ML_INGESTION_POLL_SECONDS", "1")),
        )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_roots, tuple)
            or len(dict(self.file_roots)) != len(self.file_roots)
            or any(
                not math.isfinite(value) or value <= 0
                for value in (self.lease_seconds, self.queue_timeout, self.poll_seconds)
            )
            or not math.isfinite(self.retry_delay)
            or self.retry_delay < 0
            or any(
                type(value) is not int or not 1 <= value <= 1000
                for value in (self.max_attempts, self.reconcile_limit, self.max_pending)
            )
        ):
            raise ValueError("Invalid bounded ingestion runtime settings")


@dataclass(frozen=True)
class IngestionComponents:
    database: Database
    sources: SQLSourceCatalog
    jobs: SQLIngestionJobRepository
    store: LocalRawSnapshotStore
    registry: ApprovedSourceAdapterRegistry
    service: IngestionService
    owns_database: bool

    def supervisor(self) -> IngestionSupervisor:
        return IngestionSupervisor(self.sources, self.registry, self.jobs, self.store)

    def close(self) -> None:
        if self.owns_database:
            self.database.close()


def build_ingestion(
    settings: IngestionSettings,
    *,
    database: Database | None = None,
    additional_registrations: Iterable[AdapterRegistration] = (),
) -> IngestionComponents:
    store = LocalRawSnapshotStore(settings.raw_root)
    file_adapter = FileBatchAdapter(dict(settings.file_roots), store)
    registry = ApprovedSourceAdapterRegistry(
        [
            (AdapterDescriptor("file", "1"), file_adapter, file_adapter.validate_source),
            *additional_registrations,
        ]
    )
    db = database or Database(settings.database_url)
    sources = SQLSourceCatalog(db)
    jobs = SQLIngestionJobRepository(
        db,
        lease_seconds=settings.lease_seconds,
        retry_delay=settings.retry_delay,
        max_attempts=settings.max_attempts,
        reconcile_limit=settings.reconcile_limit,
        queue_timeout=settings.queue_timeout,
        max_pending=settings.max_pending,
    )
    service = IngestionService(sources, registry, jobs)
    return IngestionComponents(db, sources, jobs, store, registry, service, database is None)
