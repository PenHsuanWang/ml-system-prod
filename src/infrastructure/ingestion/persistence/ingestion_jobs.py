"""Durable SQL polling queue with fenced acquisition and immutable publication intent."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import fields
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.engine import Connection

from src.domain.ingestion.errors import IngestionCancelled, IngestionError, SnapshotConflict
from src.domain.ingestion.models import (
    ExtractionLimits,
    IngestionClaim,
    IngestionJob,
    PublicationIntent,
    RawSnapshot,
    SourceDefinition,
    StagedBatch,
)
from src.domain.ingestion.runtime_limits import validate_job_limits
from src.domain.ingestion.states import TERMINAL, validate_transition
from src.domain.ml import (
    Actor,
    CapacityExceeded,
    Conflict,
    IdempotencyConflict,
    NotFound,
    canonical_json,
    digest,
)
from src.infrastructure.ingestion.persistence.schema import (
    ingestion_attempts,
    ingestion_jobs,
    ingestion_keys,
    raw_snapshots,
)
from src.infrastructure.ingestion.persistence.source_catalog import read_source
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit


def job_from_row(row: Mapping[str, Any]) -> IngestionJob:
    values = {
        field.name: row["id"] if field.name == "job_id" else row[field.name]
        for field in fields(IngestionJob)
    }
    return IngestionJob(**values)


def _scope(actor: Actor, key: str) -> str:
    return digest([actor.tenant_id, actor.actor_id, "ingestion.submit.v1", key])


def _visible(actor: Actor, row: Mapping[str, Any]) -> None:
    if row["tenant_id"] != actor.tenant_id or (
        row["owner_id"] != actor.actor_id and "ml:ingestion:operate" not in actor.permissions
    ):
        raise NotFound("Ingestion job not found")


def _error_code(value: str) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", value)
        else "INGESTION_FAILED"
    )


class SQLIngestionJobRepository:
    def __init__(
        self,
        database: Database,
        *,
        lease_seconds: float = 30,
        max_attempts: int = 3,
        reconcile_limit: int = 10,
        retry_delay: float = 1,
        queue_timeout: float = 600,
        max_pending: int = 100,
    ) -> None:
        validate_job_limits(
            lease_seconds=lease_seconds,
            retry_delay=retry_delay,
            queue_timeout=queue_timeout,
            max_attempts=max_attempts,
            reconcile_limit=reconcile_limit,
            max_pending=max_pending,
        )
        self.db = database
        self.lease_seconds, self.retry_delay, self.queue_timeout = (
            lease_seconds,
            retry_delay,
            queue_timeout,
        )
        self.max_attempts, self.reconcile_limit, self.max_pending = (
            max_attempts,
            reconcile_limit,
            max_pending,
        )

    @staticmethod
    def _job(connection: Connection, job_id: str) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(ingestion_jobs).where(ingestion_jobs.c.id == job_id).with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Ingestion job not found")
        return cast(Mapping[str, Any], row)

    def get(self, job_id: str) -> IngestionJob:
        with self.db.transaction() as connection:
            return job_from_row(self._job(connection, job_id))

    def find_submission(self, actor: Actor, key: str, fingerprint: str) -> IngestionJob | None:
        with self.db.transaction() as connection:
            row = (
                connection.execute(
                    select(ingestion_keys).where(
                        ingestion_keys.c.scope_digest == _scope(actor, key)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict("Key already used for a different ingestion request")
            job = self._job(connection, row["job_id"])
            _visible(actor, job)
            return job_from_row(job)

    def submit(
        self,
        actor: Actor,
        key: str,
        fingerprint: str,
        source: SourceDefinition,
        limits: ExtractionLimits,
    ) -> IngestionJob:
        if source.tenant_id != actor.tenant_id:
            raise NotFound("Approved source not found")
        if (
            limits.max_bytes > source.limits.max_bytes
            or limits.max_records > source.limits.max_records
            or limits.max_line_bytes > source.limits.max_line_bytes
            or limits.timeout_seconds > source.limits.timeout_seconds
        ):
            raise IngestionError("Ingestion bounds exceed source approval")
        now = time.time()
        with self.db.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            existing = (
                connection.execute(
                    select(ingestion_keys).where(
                        ingestion_keys.c.scope_digest == _scope(actor, key)
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise IdempotencyConflict("Key already used for a different ingestion request")
                return job_from_row(self._job(connection, existing["job_id"]))
            read_source(
                connection,
                actor.tenant_id,
                source.source_id,
                source.version,
                actor.actor_id,
                expected_digest=source.configuration_digest,
            )
            pending = connection.scalar(
                select(func.count())
                .select_from(ingestion_jobs)
                .where(
                    ingestion_jobs.c.tenant_id == actor.tenant_id,
                    ingestion_jobs.c.state.not_in(TERMINAL),
                )
            )
            if pending is not None and pending >= self.max_pending:
                raise CapacityExceeded("Tenant ingestion backlog is full")
            job_id, snapshot_id = str(uuid4()), str(uuid4())
            connection.execute(
                insert(ingestion_jobs).values(
                    id=job_id,
                    tenant_id=actor.tenant_id,
                    owner_id=actor.actor_id,
                    source_id=source.source_id,
                    source_version=source.version,
                    source_digest=source.configuration_digest,
                    limits_json=canonical_json(limits.to_dict()),
                    state="QUEUED",
                    revision=0,
                    snapshot_id=snapshot_id,
                    created_at=now,
                    deadline=now + self.queue_timeout + limits.timeout_seconds,
                    attempt_count=0,
                    acquisition_count=0,
                    reconciliation_count=0,
                    lease_until=0,
                    next_retry_at=0,
                    cancel_requested=False,
                )
            )
            connection.execute(
                insert(ingestion_keys).values(
                    scope_digest=_scope(actor, key), request_fingerprint=fingerprint, job_id=job_id
                )
            )
            record_audit(connection, actor, "ingestion.submit", job_id)
            return job_from_row(self._job(connection, job_id))

    def cancel(self, actor: Actor, job_id: str) -> IngestionJob:
        with self.db.transaction() as connection:
            row = self._job(connection, job_id)
            _visible(actor, row)
            if row["state"] in TERMINAL:
                raise Conflict("Ingestion job is already terminal")
            if row["publication_intent_json"] is not None:
                raise Conflict("Snapshot publication has begun and must be reconciled")
            immediate = row["state"] in {"QUEUED", "RETRY_WAIT"}
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == job_id)
                .values(
                    cancel_requested=True,
                    state="CANCELLED" if immediate else row["state"],
                    revision=ingestion_jobs.c.revision + 1,
                )
            )
            if immediate and row["attempt_id"]:
                connection.execute(
                    update(ingestion_attempts)
                    .where(ingestion_attempts.c.id == row["attempt_id"])
                    .values(state="CANCELLED")
                )
            record_audit(connection, actor, "ingestion.cancel", job_id)
            return job_from_row(self._job(connection, job_id))

    def _owned(self, connection: Connection, claim: IngestionClaim) -> Mapping[str, Any]:
        row = self._job(connection, claim.job.job_id)
        if (
            row["state"] in TERMINAL
            or row["owner_token"] != claim.owner_token
            or row["attempt_id"] != claim.job.attempt_id
            or row["lease_until"] <= time.time()
        ):
            raise Conflict("Expired ingestion attempt ownership")
        return row

    def _close_attempt(
        self,
        connection: Connection,
        row: Mapping[str, Any],
        state: str,
        error_code: str | None = None,
    ) -> None:
        if row["attempt_id"]:
            connection.execute(
                update(ingestion_attempts)
                .where(ingestion_attempts.c.id == row["attempt_id"])
                .values(state=state, error_code=error_code, heartbeat_at=time.time())
            )

    def claim_next(self, *, job_id: str | None = None) -> IngestionClaim | None:
        now = time.time()
        with self.db.transaction() as connection:
            exhausted = select(ingestion_jobs).where(
                ingestion_jobs.c.state.not_in(TERMINAL),
                ingestion_jobs.c.publication_intent_json.is_not(None),
                ingestion_jobs.c.reconciliation_count >= self.reconcile_limit,
                ingestion_jobs.c.lease_until <= now,
                or_(
                    ingestion_jobs.c.error_code.is_(None),
                    ingestion_jobs.c.error_code != "RECONCILIATION_BUDGET_EXHAUSTED",
                ),
            )
            if job_id is not None:
                exhausted = exhausted.where(ingestion_jobs.c.id == job_id)
            for row in connection.execute(
                exhausted.limit(100).with_for_update(skip_locked=True)
            ).mappings():
                self._close_attempt(
                    connection, row, "NEEDS_RECONCILIATION", "RECONCILIATION_BUDGET_EXHAUSTED"
                )
                connection.execute(
                    update(ingestion_jobs)
                    .where(ingestion_jobs.c.id == row["id"])
                    .values(
                        state="NEEDS_RECONCILIATION",
                        error_code="RECONCILIATION_BUDGET_EXHAUSTED",
                        revision=ingestion_jobs.c.revision + 1,
                    )
                )
            query = select(ingestion_jobs).where(
                ingestion_jobs.c.state.not_in(TERMINAL),
                ingestion_jobs.c.lease_until <= now,
                ingestion_jobs.c.next_retry_at <= now,
                or_(
                    ingestion_jobs.c.publication_intent_json.is_(None),
                    ingestion_jobs.c.reconciliation_count < self.reconcile_limit,
                ),
            )
            if job_id is not None:
                query = query.where(ingestion_jobs.c.id == job_id)
            rows = (
                connection.execute(
                    query.order_by(ingestion_jobs.c.created_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            for row in rows:
                publishing = row["publication_intent_json"] is not None
                if publishing and row["reconciliation_count"] >= self.reconcile_limit:
                    connection.execute(
                        update(ingestion_jobs)
                        .where(ingestion_jobs.c.id == row["id"])
                        .values(
                            state="NEEDS_RECONCILIATION",
                            error_code="RECONCILIATION_BUDGET_EXHAUSTED",
                        )
                    )
                    continue
                if not publishing and (
                    row["cancel_requested"]
                    or row["deadline"] <= now
                    or row["acquisition_count"] >= self.max_attempts
                ):
                    state = "CANCELLED" if row["cancel_requested"] else "FAILED"
                    code = (
                        "INGESTION_CANCELLED"
                        if row["cancel_requested"]
                        else "INGESTION_DEADLINE"
                        if row["deadline"] <= now
                        else "ACQUISITION_RETRIES_EXHAUSTED"
                    )
                    self._close_attempt(connection, row, state, code)
                    connection.execute(
                        update(ingestion_jobs)
                        .where(ingestion_jobs.c.id == row["id"])
                        .values(
                            state=state,
                            error_code=code,
                            lease_until=0,
                            revision=ingestion_jobs.c.revision + 1,
                        )
                    )
                    record_audit(
                        connection,
                        Actor(row["tenant_id"], "ingestion-worker", frozenset()),
                        "ingestion." + state.lower(),
                        row["id"],
                    )
                    continue
                self._close_attempt(connection, row, "SUPERSEDED")
                token, attempt_id = str(uuid4()), str(uuid4())
                number = row["attempt_count"] + 1
                state = "PUBLISHING" if publishing else "ACQUIRING"
                changed = connection.execute(
                    update(ingestion_jobs)
                    .where(
                        ingestion_jobs.c.id == row["id"],
                        ingestion_jobs.c.revision == row["revision"],
                        ingestion_jobs.c.lease_until <= now,
                    )
                    .values(
                        state=state,
                        owner_token=token,
                        attempt_id=attempt_id,
                        attempt_count=number,
                        lease_until=now + self.lease_seconds,
                        revision=ingestion_jobs.c.revision + 1,
                        acquisition_count=row["acquisition_count"] + (0 if publishing else 1),
                        reconciliation_count=row["reconciliation_count"] + (1 if publishing else 0),
                        staged_json=row["staged_json"] if publishing else None,
                        next_retry_at=0,
                        error_code=None,
                    )
                )
                if changed.rowcount != 1:
                    continue
                connection.execute(
                    insert(ingestion_attempts).values(
                        id=attempt_id,
                        job_id=row["id"],
                        number=number,
                        owner_token=token,
                        state=state,
                        created_at=now,
                        heartbeat_at=now,
                        staged_json=row["staged_json"] if publishing else None,
                    )
                )
                record_audit(
                    connection,
                    Actor(row["tenant_id"], "ingestion-worker", frozenset()),
                    "ingestion.claim",
                    row["id"],
                )
                return IngestionClaim(job_from_row(self._job(connection, row["id"])), token)
        return None

    def heartbeat(self, claim: IngestionClaim) -> IngestionJob:
        with self.db.transaction() as connection:
            row = self._owned(connection, claim)
            now = time.time()
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == row["id"])
                .values(lease_until=now + self.lease_seconds)
            )
            connection.execute(
                update(ingestion_attempts)
                .where(ingestion_attempts.c.id == row["attempt_id"])
                .values(heartbeat_at=now)
            )
            return job_from_row(self._job(connection, row["id"]))

    def stage_complete(self, claim: IngestionClaim, staged: StagedBatch) -> IngestionClaim:
        with self.db.transaction() as connection:
            row = self._owned(connection, claim)
            if row["cancel_requested"]:
                raise IngestionCancelled("Cancellation prevents staging completion")
            validate_transition(row["state"], "VALIDATING")
            if (staged.tenant_id, staged.job_id, staged.attempt_id) != (
                row["tenant_id"],
                row["id"],
                row["attempt_id"],
            ):
                raise IngestionError("Staged result does not match its ingestion attempt")
            limits = ExtractionLimits.parse(json.loads(row["limits_json"]))
            source = read_source(
                connection,
                row["tenant_id"],
                row["source_id"],
                row["source_version"],
                row["owner_id"],
                expected_digest=row["source_digest"],
            )
            if (
                staged.size_bytes > limits.max_bytes
                or staged.record_count > limits.max_records
                or staged.data_format != source.data_format
                or staged.schema_version != source.schema_version
            ):
                raise IngestionError("Staged result exceeds the approved source contract")
            evidence = canonical_json(staged.to_dict())
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == row["id"])
                .values(
                    state="VALIDATING", staged_json=evidence, revision=ingestion_jobs.c.revision + 1
                )
            )
            connection.execute(
                update(ingestion_attempts)
                .where(ingestion_attempts.c.id == row["attempt_id"])
                .values(state="VALIDATING", staged_json=evidence)
            )
            return IngestionClaim(job_from_row(self._job(connection, row["id"])), claim.owner_token)

    def prepare_publication(
        self, claim: IngestionClaim, intent: PublicationIntent
    ) -> IngestionClaim:
        with self.db.transaction() as connection:
            row = self._owned(connection, claim)
            if row["cancel_requested"]:
                raise IngestionCancelled("Cancellation prevents publication")
            validate_transition(row["state"], "PUBLISHING")
            if (intent.snapshot_id, intent.tenant_id, intent.job_id) != (
                row["snapshot_id"],
                row["tenant_id"],
                row["id"],
            ) or canonical_json(intent.staged.to_dict()) != row["staged_json"]:
                raise IngestionError("Publication intent does not match frozen staging evidence")
            # The point of no return is durable before the first visible filesystem side effect.
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == row["id"])
                .values(
                    state="PUBLISHING",
                    publication_intent_json=canonical_json(intent.to_dict()),
                    revision=ingestion_jobs.c.revision + 1,
                )
            )
            connection.execute(
                update(ingestion_attempts)
                .where(ingestion_attempts.c.id == row["attempt_id"])
                .values(state="PUBLISHING")
            )
            record_audit(
                connection,
                Actor(row["tenant_id"], row["owner_id"], frozenset()),
                "ingestion.publication_intent",
                row["id"],
            )
            return IngestionClaim(job_from_row(self._job(connection, row["id"])), claim.owner_token)

    def finish(self, claim: IngestionClaim, snapshot: RawSnapshot) -> IngestionJob:
        with self.db.transaction() as connection:
            row = self._owned(connection, claim)
            if row["publication_intent_json"] is None:
                raise IngestionError(
                    "Raw snapshot completion requires a durable publication intent"
                )
            validate_transition(row["state"], "PUBLISHED")
            intent = PublicationIntent.parse(json.loads(row["publication_intent_json"]))
            expected = (
                row["snapshot_id"],
                row["tenant_id"],
                row["id"],
                row["source_id"],
                row["source_version"],
                row["source_digest"],
                intent.manifest_digest,
                intent.staged.content_digest,
                intent.staged.size_bytes,
                intent.staged.record_count,
                intent.staged.data_format,
                intent.staged.schema_version,
                intent.staged.source_revision,
                row["created_at"],
            )
            actual = (
                snapshot.snapshot_id,
                snapshot.tenant_id,
                snapshot.job_id,
                snapshot.source_id,
                snapshot.source_version,
                snapshot.source_configuration_digest,
                snapshot.manifest_digest,
                snapshot.content_digest,
                snapshot.size_bytes,
                snapshot.record_count,
                snapshot.data_format,
                snapshot.schema_version,
                snapshot.source_revision,
                snapshot.created_at,
            )
            if actual != expected:
                raise SnapshotConflict(
                    "Raw snapshot evidence does not match the frozen publication intent"
                )
            frozen = canonical_json(snapshot.to_dict())
            existing = (
                connection.execute(
                    select(raw_snapshots).where(raw_snapshots.c.id == snapshot.snapshot_id)
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if existing["snapshot_json"] != frozen:
                    raise SnapshotConflict(
                        "Raw snapshot identity is already bound to another result"
                    )
            else:
                connection.execute(
                    insert(raw_snapshots).values(
                        id=snapshot.snapshot_id,
                        tenant_id=snapshot.tenant_id,
                        job_id=snapshot.job_id,
                        manifest_digest=snapshot.manifest_digest,
                        content_digest=snapshot.content_digest,
                        snapshot_json=frozen,
                        created_at=snapshot.created_at,
                    )
                )
            self._close_attempt(connection, row, "PUBLISHED")
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == row["id"])
                .values(
                    state="PUBLISHED",
                    result_json=frozen,
                    error_code=None,
                    lease_until=0,
                    revision=ingestion_jobs.c.revision + 1,
                )
            )
            record_audit(
                connection,
                Actor(row["tenant_id"], row["owner_id"], frozenset()),
                "ingestion.published",
                row["id"],
            )
            return job_from_row(self._job(connection, row["id"]))

    def fail(self, claim: IngestionClaim, error_code: str, *, retryable: bool) -> IngestionJob:
        with self.db.transaction() as connection:
            row = self._owned(connection, claim)
            if row["publication_intent_json"] is not None:
                self._reconcile(connection, row, error_code)
            else:
                now = time.time()
                state = (
                    "CANCELLED"
                    if row["cancel_requested"]
                    else "RETRY_WAIT"
                    if retryable
                    and row["acquisition_count"] < self.max_attempts
                    and row["deadline"] > now
                    else "FAILED"
                )
                validate_transition(row["state"], state)
                safe_code = (
                    "INGESTION_CANCELLED" if state == "CANCELLED" else _error_code(error_code)
                )
                self._close_attempt(connection, row, state, safe_code)
                connection.execute(
                    update(ingestion_jobs)
                    .where(ingestion_jobs.c.id == row["id"])
                    .values(
                        state=state,
                        error_code=safe_code,
                        lease_until=0,
                        next_retry_at=now
                        + self.retry_delay * 2 ** min(row["acquisition_count"] - 1, 8),
                        revision=ingestion_jobs.c.revision + 1,
                    )
                )
                record_audit(
                    connection,
                    Actor(row["tenant_id"], "ingestion-worker", frozenset()),
                    "ingestion." + state.lower(),
                    row["id"],
                )
            return job_from_row(self._job(connection, row["id"]))

    def _reconcile(self, connection: Connection, row: Mapping[str, Any], error_code: str) -> None:
        if row["publication_intent_json"] is None:
            raise IngestionError("No publication intent is available for reconciliation")
        validate_transition(row["state"], "NEEDS_RECONCILIATION")
        safe_code = (
            _error_code(error_code)
            if row["reconciliation_count"] < self.reconcile_limit
            else "RECONCILIATION_BUDGET_EXHAUSTED"
        )
        self._close_attempt(connection, row, "NEEDS_RECONCILIATION", safe_code)
        connection.execute(
            update(ingestion_jobs)
            .where(ingestion_jobs.c.id == row["id"])
            .values(
                state="NEEDS_RECONCILIATION",
                error_code=safe_code,
                lease_until=0,
                next_retry_at=time.time()
                + self.retry_delay * 2 ** min(row["reconciliation_count"], 8),
                revision=ingestion_jobs.c.revision + 1,
            )
        )
        record_audit(
            connection,
            Actor(row["tenant_id"], "ingestion-worker", frozenset()),
            "ingestion.reconciliation_required",
            row["id"],
        )

    def reconcile_later(self, claim: IngestionClaim, error_code: str) -> None:
        with self.db.transaction() as connection:
            self._reconcile(connection, self._owned(connection, claim), error_code)

    def snapshot(self, actor: Actor, snapshot_id: str) -> RawSnapshot:
        with self.db.transaction() as connection:
            snapshot = (
                connection.execute(
                    select(raw_snapshots).where(
                        raw_snapshots.c.id == snapshot_id,
                        raw_snapshots.c.tenant_id == actor.tenant_id,
                    )
                )
                .mappings()
                .first()
            )
            if snapshot is None:
                raise NotFound("Raw snapshot not found")
            job = self._job(connection, snapshot["job_id"])
            _visible(actor, job)
            if job["state"] != "PUBLISHED":
                raise NotFound("Raw snapshot publication is not durable")
            return RawSnapshot.parse(json.loads(snapshot["snapshot_json"]))

    def protected_job_ids(self) -> frozenset[str]:
        """Maintenance must retain every nonterminal job, even an exhausted/expired one."""
        with self.db.transaction() as connection:
            return frozenset(
                connection.execute(
                    select(ingestion_jobs.c.id).where(ingestion_jobs.c.state.not_in(TERMINAL))
                ).scalars()
            )

    def requeue_reconciliation(self, actor: Actor, job_id: str) -> IngestionJob:
        """Explicit operator retry preserves the exact intent after bounded automatic retries."""
        actor.require("ml:ingestion:operate")
        with self.db.transaction() as connection:
            row = self._job(connection, job_id)
            _visible(actor, row)
            if (
                row["state"] != "NEEDS_RECONCILIATION"
                or row["publication_intent_json"] is None
                or row["lease_until"] > time.time()
            ):
                raise Conflict("Ingestion is not awaiting operator reconciliation")
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == job_id)
                .values(
                    reconciliation_count=0,
                    next_retry_at=0,
                    owner_token=None,
                    lease_until=0,
                    error_code=None,
                    revision=ingestion_jobs.c.revision + 1,
                )
            )
            record_audit(connection, actor, "ingestion.reconcile.requested", job_id)
            return job_from_row(self._job(connection, job_id))
