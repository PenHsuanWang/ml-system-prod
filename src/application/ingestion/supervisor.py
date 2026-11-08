"""Durable batch execution, independent of adapter kind and model workloads."""

import json
import time

from src.application.ingestion.ports import (
    IngestionJobRepository,
    RawSnapshotPublisher,
    SourceAdapterResolver,
    SourceResolver,
)
from src.domain.ingestion.errors import (
    AcquisitionDeadline,
    IngestionCancelled,
    IngestionError,
    SnapshotConflict,
)
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    IngestionClaim,
    IngestionJob,
    PublicationIntent,
    RawSnapshot,
    StagedBatch,
)
from src.domain.ml import Conflict, MLError


class _AcquisitionControl:
    def __init__(
        self, jobs: IngestionJobRepository, claim: IngestionClaim, timeout_seconds: float
    ) -> None:
        self.jobs, self.claim = jobs, claim
        self.deadline = time.monotonic() + timeout_seconds
        self.next_heartbeat = 0.0

    def check(self) -> None:
        now = time.monotonic()
        if now >= self.deadline or time.time() >= self.claim.job.deadline:
            raise AcquisitionDeadline("Ingestion acquisition deadline exceeded")
        if now >= self.next_heartbeat:
            job = self.jobs.heartbeat(self.claim)
            if job.cancel_requested:
                raise IngestionCancelled("Ingestion was cancelled before publication")
            remaining = max(0.1, job.lease_until - time.time())
            self.next_heartbeat = now + min(1.0, remaining / 3)


class IngestionSupervisor:
    def __init__(
        self,
        sources: SourceResolver,
        adapters: SourceAdapterResolver,
        jobs: IngestionJobRepository,
        publisher: RawSnapshotPublisher,
    ) -> None:
        self.sources, self.adapters, self.jobs, self.publisher = sources, adapters, jobs, publisher

    def run_once(self, *, job_id: str | None = None) -> IngestionJob | None:
        claim = self.jobs.claim_next(job_id=job_id)
        if claim is None:
            return None
        staged = None
        try:
            if claim.job.publication_intent_json is not None:
                intent = PublicationIntent.parse(json.loads(claim.job.publication_intent_json))
                return self._publish(claim, intent)
            # Approval/tenant/grants are checked again at actual execution, not just HTTP.
            source = self.sources.for_execution(claim.job)
            self.adapters.validate_source(source)
            adapter = self.adapters.resolve(
                source.adapter_type, source.adapter_version, "batch_extract"
            )
            limits = ExtractionLimits.parse(json.loads(claim.job.limits_json))
            extraction = BatchExtraction(source, claim.execution, limits)
            control = _AcquisitionControl(self.jobs, claim, limits.timeout_seconds)
            control.check()
            staged = adapter.acquire(extraction, control)
            self._validate_stage(extraction, staged)
            control.check()
            claim = self.jobs.stage_complete(claim, staged)
            intent = self.publisher.prepare(extraction, staged)
            if (
                intent.snapshot_id != claim.job.snapshot_id
                or intent.tenant_id != claim.job.tenant_id
                or intent.job_id != claim.job.job_id
                or intent.staged != staged
            ):
                raise IngestionError("Publisher returned a mismatched publication intent")
            control.check()
            claim = self.jobs.prepare_publication(claim, intent)
            return self._publish(claim, intent)
        except SnapshotConflict:
            self.jobs.reconcile_later(claim, "RAW_SNAPSHOT_CONFLICT")
            return self.jobs.get(claim.job.job_id)
        except Conflict:
            # Another owner may already be reconciling. Never fail or overwrite its job.
            self._discard_uncommitted(claim, staged)
            return self.jobs.get(claim.job.job_id)
        except Exception as error:
            current = self.jobs.get(claim.job.job_id)
            if current.publication_intent_json is not None:
                # Filesystem and SQL outcomes may disagree. Retain exact candidate evidence.
                self.jobs.reconcile_later(claim, "PUBLICATION_RECONCILIATION_REQUIRED")
                return self.jobs.get(claim.job.job_id)
            code = error.code if isinstance(error, MLError) else "INGESTION_EXECUTION_FAILED"
            retryable = isinstance(error, MLError) and error.retryable
            self._discard_uncommitted(claim, staged)
            return self.jobs.fail(claim, code, retryable=retryable)

    @staticmethod
    def _validate_stage(extraction: BatchExtraction, staged: StagedBatch) -> None:
        execution = extraction.execution
        if (
            not isinstance(staged, StagedBatch)
            or staged.tenant_id != execution.tenant_id
            or staged.job_id != execution.job_id
            or staged.attempt_id != execution.attempt_id
            or staged.data_format != extraction.source.data_format
            or staged.schema_version != extraction.source.schema_version
            or staged.size_bytes > extraction.limits.max_bytes
            or staged.record_count > extraction.limits.max_records
        ):
            raise IngestionError("Source adapter returned incompatible staged evidence")

    def _publish(self, claim: IngestionClaim, intent: PublicationIntent) -> IngestionJob:
        snapshot = self.publisher.lookup(
            intent.tenant_id, intent.snapshot_id, intent.manifest_digest
        )
        if snapshot is None:
            snapshot = self.publisher.publish(intent)
        self._validate_snapshot(intent, snapshot)
        if (
            snapshot.source_id != claim.job.source_id
            or snapshot.source_version != claim.job.source_version
            or snapshot.source_configuration_digest != claim.job.source_digest
            or snapshot.created_at != claim.job.created_at
        ):
            raise IngestionError("Published snapshot source lineage does not match the job")
        final = self.jobs.finish(claim, snapshot)
        try:
            self.publisher.discard(intent.staged)
        except Exception:
            # Publication already committed. A bounded maintenance sweep can remove this stage.
            pass
        return final

    @staticmethod
    def _validate_snapshot(intent: PublicationIntent, snapshot: RawSnapshot) -> None:
        if (
            not isinstance(snapshot, RawSnapshot)
            or snapshot.tenant_id != intent.tenant_id
            or snapshot.snapshot_id != intent.snapshot_id
            or snapshot.job_id != intent.job_id
            or snapshot.manifest_digest != intent.manifest_digest
            or snapshot.content_digest != intent.staged.content_digest
            or snapshot.size_bytes != intent.staged.size_bytes
            or snapshot.record_count != intent.staged.record_count
        ):
            raise IngestionError("Published raw snapshot does not match its durable intent")

    def _discard_uncommitted(self, claim: IngestionClaim, staged: StagedBatch | None) -> None:
        if staged is None or staged.attempt_id != claim.job.attempt_id:
            return
        try:
            current = self.jobs.get(claim.job.job_id)
            if current.publication_intent_json is not None:
                intent = PublicationIntent.parse(json.loads(current.publication_intent_json))
                if intent.staged.staging_ref == staged.staging_ref:
                    return
            self.publisher.discard(staged)
        except Exception:
            # Unknown durable state is never a reason to remove recovery evidence.
            pass
