"""Explicit source construction and registration; no source selection in use cases."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.application.ingestion.ports import AdapterRegistration
from src.application.ingestion.registry import ApprovedSourceAdapterRegistry
from src.application.ingestion.service import IngestionService
from src.application.ingestion.supervisor import IngestionSupervisor
from src.domain.ingestion.models import AdapterDescriptor
from src.domain.ingestion.runtime_limits import validate_job_limits, validate_poll_seconds
from src.infrastructure.ingestion.adapters.file_batch import FileBatchAdapter
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository
from src.infrastructure.ingestion.persistence.source_catalog import SQLSourceCatalog
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.persistence.ml_database import Database


def _unique_root_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    roots = {}
    for key, value in pairs:
        if key in roots:
            raise ValueError("Duplicate approved ingestion root key")
        roots[key] = value
    return roots


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
        roots = json.loads(raw, object_pairs_hook=_unique_root_keys)
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
        validate_job_limits(
            lease_seconds=self.lease_seconds,
            retry_delay=self.retry_delay,
            queue_timeout=self.queue_timeout,
            max_attempts=self.max_attempts,
            reconcile_limit=self.reconcile_limit,
            max_pending=self.max_pending,
        )
        validate_poll_seconds(self.poll_seconds)
        if (
            not isinstance(self.database_url, str)
            or not self.database_url
            or len(self.database_url) > 16384
            or not isinstance(self.raw_root, Path)
            or ".." in self.raw_root.absolute().parts
            or not isinstance(self.file_roots, tuple)
            or len(self.file_roots) > 128
        ):
            raise ValueError("Invalid approved ingestion storage configuration")
        seen = set()
        for item in self.file_roots:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", item[0])
                or item[0] in seen
                or not isinstance(item[1], Path)
                or ".." in item[1].absolute().parts
            ):
                raise ValueError("Invalid approved ingestion file root")
            seen.add(item[0])


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
