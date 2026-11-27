"""Admission and metadata access; external acquisition never runs in HTTP."""

import json

from src.application.ingestion.ports import (
    IngestionDiscoveryRepository,
    IngestionJobRepository,
    SourceAdapterResolver,
    SourceCandidates,
    SourceResolver,
)
from src.domain.ingestion.discovery import IngestionCollectionQuery
from src.domain.ingestion.models import (
    IngestionJob,
    IngestionRequest,
    RawSnapshot,
    SourceDefinition,
)
from src.domain.ml import Actor, Conflict, Forbidden, MLError, NotFound, Unavailable, digest


class IngestionService:
    def __init__(
        self,
        sources: SourceResolver,
        adapters: SourceAdapterResolver,
        jobs: IngestionJobRepository,
        candidates: SourceCandidates | None = None,
        discovery: IngestionDiscoveryRepository | None = None,
    ) -> None:
        self.sources, self.adapters, self.jobs = sources, adapters, jobs
        self.candidates = candidates
        self.discovery = discovery

    def submit(self, actor: Actor, key: str, request: IngestionRequest) -> IngestionJob:
        actor.require("ml:ingest")
        if (
            not isinstance(key, str)
            or not 1 <= len(key.encode()) <= 128
            or any(ord(char) < 33 or ord(char) > 126 for char in key)
        ):
            raise MLError("Idempotency-Key must contain 1 to 128 visible ASCII characters")
        if not isinstance(request, IngestionRequest):
            raise MLError("Invalid versioned ingestion request")
        fingerprint = digest(["ingestion-request-v1", request.to_dict()])
        previous = self.jobs.find_submission(actor, key, fingerprint)
        if previous is not None:
            return self.get(actor, previous.job_id)
        source = self.approved_source(actor, request.source_id, request.source_version)
        limits = request.bounded_by(source.limits)
        return self.jobs.submit(actor, key, fingerprint, source, limits)

    def approved_source(self, actor: Actor, source_id: str, version: str) -> SourceDefinition:
        actor.require("ml:ingest")
        source = self.sources.resolve(actor, source_id, version)
        self.adapters.validate_source(source)
        return source

    def available_sources(
        self, actor: Actor, limit: int, offset: int
    ) -> tuple[list[SourceDefinition], bool]:
        actor.require("ml:ingest")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 1_000_000
        ):
            raise MLError("Invalid source pagination")
        if self.candidates is None:
            raise Unavailable("Available source listing is not configured")
        keys = self.candidates.candidate_keys(actor, limit + 1, offset)
        items = []
        for source_id, version in keys[:limit]:
            try:
                items.append(self.approved_source(actor, source_id, version))
            except (MLError, ValueError, TypeError, KeyError):
                # Unavailable, corrupt or unsupported definitions disclose no metadata.
                continue
        return items, len(keys) > limit

    def get(self, actor: Actor, job_id: str) -> IngestionJob:
        if not actor.permissions.intersection(
            {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
        ):
            raise Forbidden("Ingestion read permission is required")
        job = self.jobs.get(job_id)
        if job.tenant_id != actor.tenant_id or (
            job.owner_id != actor.actor_id and "ml:ingestion:operate" not in actor.permissions
        ):
            raise NotFound("Ingestion job not found")
        return job

    def cancel(self, actor: Actor, job_id: str) -> IngestionJob:
        if not actor.permissions.intersection({"ml:ingestion:cancel", "ml:ingestion:operate"}):
            raise Forbidden("Ingestion cancellation permission is required")
        # Cancellation grants do not implicitly grant inspection of unrelated jobs.
        job = self.jobs.get(job_id)
        if job.tenant_id != actor.tenant_id or (
            job.owner_id != actor.actor_id and "ml:ingestion:operate" not in actor.permissions
        ):
            raise NotFound("Ingestion job not found")
        return self.jobs.cancel(actor, job_id)

    def result(self, actor: Actor, job_id: str) -> RawSnapshot:
        job = self.get(actor, job_id)
        if job.state != "PUBLISHED" or job.result_json is None:
            raise Conflict("Raw snapshot is not durably published")
        result = RawSnapshot.parse(json.loads(job.result_json))
        return self.snapshot(actor, result.snapshot_id)

    def snapshot(self, actor: Actor, snapshot_id: str) -> RawSnapshot:
        if not actor.permissions.intersection(
            {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
        ):
            raise Forbidden("Ingestion read permission is required")
        # Metadata only. Builders must verify payload integrity via their snapshot reader.
        return self.jobs.snapshot(actor, snapshot_id)

    def reconcile(self, actor: Actor, job_id: str) -> IngestionJob:
        actor.require("ml:ingestion:operate")
        self.get(actor, job_id)
        return self.jobs.requeue_reconciliation(actor, job_id)

    def collections(
        self, actor: Actor, query: IngestionCollectionQuery, *, snapshots: bool = False
    ) -> tuple[list[IngestionJob] | list[RawSnapshot], bool]:
        if not actor.permissions.intersection(
            {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
        ):
            raise Forbidden("Ingestion read permission is required")
        if self.discovery is None:
            raise Unavailable("Ingestion metadata discovery is not configured")
        rows = (
            self.discovery.list_snapshots(actor, query)
            if snapshots
            else self.discovery.list_jobs(actor, query)
        )
        return rows[: query.window.limit], len(rows) > query.window.limit
