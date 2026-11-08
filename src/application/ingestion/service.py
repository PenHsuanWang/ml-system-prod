"""Admission and metadata access; external acquisition never runs in HTTP."""

import json

from src.application.ingestion.ports import (
    IngestionJobRepository,
    SourceAdapterResolver,
    SourceResolver,
)
from src.domain.ingestion.models import IngestionJob, IngestionRequest, RawSnapshot
from src.domain.ml import Actor, Conflict, Forbidden, MLError, NotFound, digest


class IngestionService:
    def __init__(
        self, sources: SourceResolver, adapters: SourceAdapterResolver, jobs: IngestionJobRepository
    ) -> None:
        self.sources, self.adapters, self.jobs = sources, adapters, jobs

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
        source = self.sources.resolve(actor, request.source_id, request.source_version)
        self.adapters.validate_source(source)
        limits = request.bounded_by(source.limits)
        return self.jobs.submit(actor, key, fingerprint, source, limits)

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
