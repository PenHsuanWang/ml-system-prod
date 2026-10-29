"""Transactional job acceptance, atomic claims, cancellation and terminal fencing."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection

from src.domain.ml import Actor, CapacityExceeded, Conflict, digest, IdempotencyConflict, NotFound, canonical_json
from src.infrastructure.ml_tracing import capture_context
from src.domain.training.models import Attempt, Claim, Job, TERMINAL, TrainingCommand
from src.infrastructure.persistence.ml_database import attempts, audit, Database, evaluations, jobs, keys, models, outbox, tenants


def job_from_row(row: Mapping[str, Any]) -> Job:
    return Job(
        job_id=row["id"], tenant_id=row["tenant_id"], owner_id=row["owner_id"],
        state=row["state"], phase=row["phase"], revision=row["revision"],
        attempt_id=row["current_attempt_id"], spec_json=row["spec_json"],
        spec_digest=row["spec_digest"], created_at=row["created_at"], deadline=row["deadline"],
        cancel_requested=row["cancel_requested"], error_code=row["error_code"],
        model_binding_id=row["model_binding_id"],
    )


def record_audit(c: Connection, actor: Actor, action: str, subject: str) -> None:
    c.execute(insert(audit).values(id=str(uuid4()), tenant_id=actor.tenant_id,
              actor_id=actor.actor_id, action=action, subject_id=subject, created_at=time.time()))


def lock_tenant(c: Connection, tenant: str) -> None:
    if c.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    c.execute(dialect_insert(tenants).values(tenant_id=tenant, revision=0)
              .on_conflict_do_nothing(index_elements=["tenant_id"]))
    c.execute(update(tenants).where(tenants.c.tenant_id == tenant)
              .values(revision=tenants.c.revision + 1))


class SQLTrainingRepository:
    def __init__(self, database: Database, *, retention: int = 86400,
                 max_pending: int = 100, queue_timeout: int = 600,
                 training_timeout: int = 300) -> None:
        self.db = database
        self.retention = retention
        self.max_pending = max_pending
        self.queue_timeout = queue_timeout
        self.training_timeout = training_timeout

    def find_submission(self, actor: Actor, key: str, fingerprint: str) -> Job | None:
        scope = digest([actor.tenant_id, actor.actor_id, "training.submit.v1", key])
        with self.db.transaction() as c:
            value = c.execute(select(keys).where(keys.c.scope_digest == scope,
                              keys.c.expires_at > time.time())).mappings().first()
            if value is None:
                return None
            if value["request_fingerprint"] != fingerprint:
                raise IdempotencyConflict("Key already used for a different request")
            return job_from_row(self._job(c, value["job_id"]))

    def submit(self, actor: Actor, key: str, request_fingerprint: str,
               spec_json: str, spec_digest: str) -> Job:
        scope = digest([actor.tenant_id, actor.actor_id, "training.submit.v1", key])
        now = time.time()
        with self.db.transaction() as c:
            lock_tenant(c, actor.tenant_id)
            existing = c.execute(select(keys).where(
                keys.c.scope_digest == scope)).mappings().first()
            if existing and existing["expires_at"] > now:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise IdempotencyConflict("Key already used for a different request")
                return job_from_row(self._job(c, existing["job_id"]))
            if existing:
                c.execute(delete(keys).where(keys.c.scope_digest == scope))
            pending = c.scalar(select(func.count()).select_from(jobs).where(
                jobs.c.tenant_id == actor.tenant_id, jobs.c.state.not_in([s.value for s in TERMINAL])))
            if pending >= self.max_pending:
                raise CapacityExceeded("Tenant training backlog is full")
            job_id, attempt_id, command_id = (str(uuid4()) for _ in range(3))
            c.execute(insert(jobs).values(
                id=job_id, tenant_id=actor.tenant_id, owner_id=actor.actor_id,
                state="ACCEPTED", phase="PENDING", revision=0, current_attempt_id=attempt_id,
                spec_json=spec_json, spec_digest=spec_digest, created_at=now,
                queue_deadline=now+self.queue_timeout,
                deadline=now+self.queue_timeout+self.training_timeout,
                cancel_requested=False))
            c.execute(insert(keys).values(scope_digest=scope, request_fingerprint=request_fingerprint,
                      job_id=job_id, expires_at=now+self.retention))
            c.execute(insert(attempts).values(id=attempt_id, job_id=job_id, number=1,
                      state="PENDING", phase="PENDING", revision=0))
            c.execute(insert(outbox).values(id=command_id, job_id=job_id, attempt_id=attempt_id,
                      context_json=canonical_json(capture_context()),
                      spec_digest=spec_digest, state="PENDING", lease_until=0, tries=0,
                      next_retry_at=0))
            record_audit(c, actor, "training.submit", job_id)
            return job_from_row(self._job(c, job_id))

    @staticmethod
    def _job(c: Connection, job_id: str) -> Mapping[str, Any]:
        row = c.execute(select(jobs).where(
            jobs.c.id == job_id).with_for_update()).mappings().first()
        if row is None:
            raise NotFound("Job not found")
        return cast(Mapping[str, Any], row)

    def get(self, job_id: str) -> Job:
        with self.db.transaction() as c:
            return job_from_row(self._job(c, job_id))

    def attempts(self, job_id: str) -> tuple[Attempt, ...]:
        with self.db.transaction() as c:
            rows = c.execute(select(attempts).where(attempts.c.job_id == job_id)
                             .order_by(attempts.c.number)).mappings()
            return tuple(Attempt(r["id"], r["job_id"], r["number"], r["state"],
                         r["owner_token"], r["execution_ref"], r["heartbeat_at"],
                         r["phase"], r["revision"]) for r in rows)

    def cancel(self, job_id: str, actor: Actor) -> Job:
        with self.db.transaction() as c:
            row = self._job(c, job_id)
            if row["tenant_id"] != actor.tenant_id or (
                row["owner_id"] != actor.actor_id and "ml:jobs:read_all" not in actor.permissions
            ):
                raise NotFound("Job not found")
            if row["state"] in TERMINAL:
                raise Conflict("Job is already terminal")
            attempt = c.execute(select(attempts).where(
                attempts.c.id == row["current_attempt_id"])).mappings().one()
            never_started = attempt["state"] == "PENDING"
            if never_started:
                c.execute(update(attempts).where(attempts.c.id == attempt["id"])
                          .values(state="CANCELLED", revision=attempts.c.revision+1))
            c.execute(update(jobs).where(jobs.c.id == job_id).values(
                cancel_requested=True, state="CANCELLED" if never_started else "CANCEL_REQUESTED",
                revision=jobs.c.revision+1))
            record_audit(c, actor, "training.cancel", job_id)
            return job_from_row(self._job(c, job_id))

    def claim(self, command: TrainingCommand) -> Claim:
        with self.db.transaction() as c:
            row = self._job(c, command.job_id)
            intent = c.execute(select(outbox).where(
                outbox.c.id == command.command_id)).mappings().first()
            if not intent or command.schema_version != "1" or (
                row["current_attempt_id"], row["spec_digest"], row["id"]
            ) != (command.attempt_id, command.spec_digest, intent["job_id"]) or (
                intent["attempt_id"] != command.attempt_id
            ):
                raise Conflict("Command does not match durable intent")
            if row["state"] in TERMINAL:
                return Claim(job_from_row(row), "", already_complete=True)
            if row["cancel_requested"]:
                raise Conflict("Cancellation is pending")
            if time.time() > row["queue_deadline"] and row["state"] in {"ACCEPTED", "QUEUED"}:
                c.execute(update(jobs).where(jobs.c.id == row["id"])
                          .values(state="FAILED", error_code="QUEUE_DEADLINE", revision=jobs.c.revision+1))
                c.execute(update(attempts).where(attempts.c.id == command.attempt_id,
                          attempts.c.state == "PENDING").values(state="FAILED"))
                return Claim(job_from_row(self._job(c, row["id"])), "", already_complete=True)
            owner = str(uuid4())
            result = c.execute(update(attempts).where(attempts.c.id == command.attempt_id,
                                                      attempts.c.state == "PENDING").values(state="STARTING", owner_token=owner,
                                                                                            heartbeat_at=time.time(), phase="STARTING", revision=attempts.c.revision+1))
            if result.rowcount != 1:
                raise Conflict("Attempt already owned; reconcile before replay")
            c.execute(update(jobs).where(jobs.c.id == row["id"]).values(
                state="RUNNING", phase="STARTING", revision=jobs.c.revision+1))
            return Claim(job_from_row(self._job(c, row["id"])), owner)

    def _owned(self, c: Connection, claim: Claim) -> Mapping[str, Any]:
        row = self._job(c, claim.job.job_id)
        attempt = c.execute(select(attempts).where(attempts.c.id == row["current_attempt_id"]))
        value = attempt.mappings().one()
        if row["state"] in TERMINAL or row["current_attempt_id"] != claim.job.attempt_id or (
            value["owner_token"] != claim.owner_token
        ):
            raise Conflict("Expired attempt ownership")
        return row

    def execution_started(self, claim: Claim, execution_ref: str) -> None:
        with self.db.transaction() as c:
            row = self._owned(c, claim)
            c.execute(update(attempts).where(attempts.c.id == claim.job.attempt_id).values(
                state="RUNNING", phase="FIT", execution_ref=execution_ref,
                heartbeat_at=time.time(), revision=attempts.c.revision+1))
            c.execute(update(jobs).where(jobs.c.id == claim.job.job_id)
                      .values(state="CANCEL_REQUESTED" if row["cancel_requested"] else "RUNNING",
                              phase="FIT", revision=jobs.c.revision+1))

    def prepare_execution(self, claim: Claim, execution_ref: str) -> None:
        with self.db.transaction() as c:
            self._owned(c, claim)
            c.execute(update(attempts).where(attempts.c.id == claim.job.attempt_id).values(
                execution_ref=execution_ref, heartbeat_at=time.time(), phase="STARTING",
                revision=attempts.c.revision+1))

    def checkpoint_phase(self, claim: Claim, phase: str, evidence_json: str | None = None) -> None:
        if phase not in {"REGISTER", "EVALUATE"}:
            raise Conflict("Unknown postprocessing phase")
        with self.db.transaction() as c:
            row = self._owned(c, claim)
            if row["cancel_requested"]:
                raise Conflict("Cancellation prevents new postprocessing")
            c.execute(update(jobs).where(jobs.c.id == claim.job.job_id)
                      .values(phase=phase, revision=jobs.c.revision+1))
            values = {"phase": phase, "revision": attempts.c.revision+1}
            if evidence_json is not None:
                values["result_json"] = evidence_json
            c.execute(update(attempts).where(attempts.c.id ==
                      claim.job.attempt_id).values(**values))

    def recoverable(self, stale_after: int) -> tuple[tuple[Claim, str], ...]:
        result = []
        with self.db.transaction() as c:
            candidates = select(attempts.c.job_id).where(
                attempts.c.state.in_(["STARTING", "RUNNING", "LOST"]),
                attempts.c.heartbeat_at < time.time()-stale_after,
                attempts.c.reconciliation_count < 5)
            rows = c.execute(select(jobs).where(jobs.c.id.in_(candidates),
                                                jobs.c.state.not_in([s.value for s in TERMINAL])).limit(10)
                             .with_for_update(skip_locked=True)).mappings().all()
            for row in rows:
                attempt = c.execute(select(attempts).where(
                    attempts.c.id == row["current_attempt_id"]).with_for_update()).mappings().one()
                if attempt["heartbeat_at"] is not None and attempt["heartbeat_at"] >= time.time()-stale_after:
                    continue
                if row["state"] in TERMINAL:
                    continue
                owner = str(uuid4())
                c.execute(update(attempts).where(attempts.c.id == attempt["id"],
                                                 attempts.c.revision == attempt["revision"]).values(
                    owner_token=owner, state="LOST", heartbeat_at=time.time(),
                    reconciliation_count=attempts.c.reconciliation_count+1,
                    revision=attempts.c.revision+1))
                c.execute(update(jobs).where(jobs.c.id == row["id"]).values(
                    state="NEEDS_RECONCILIATION", revision=jobs.c.revision+1,
                    error_code="RECONCILIATION_BUDGET_EXHAUSTED" if attempt["reconciliation_count"] >= 4 else None))
                if attempt["execution_ref"]:
                    result.append((Claim(job_from_row(self._job(c, row["id"])), owner),
                                   attempt["execution_ref"]))
                else:
                    # No launch intent was ever committed: do not guess about death.
                    c.execute(update(jobs).where(jobs.c.id == row["id"])
                              .values(error_code="EXECUTION_IDENTITY_MISSING"))
        return tuple(result)

    def expire_queued(self, limit: int = 100) -> int:
        """Deadline enforcement is independent of whether RabbitMQ is reachable."""
        expired = 0
        with self.db.transaction() as c:
            rows = c.execute(select(jobs).where(jobs.c.state.in_(["ACCEPTED", "QUEUED"]),
                                                jobs.c.queue_deadline < time.time()).limit(limit)
                             .with_for_update(skip_locked=True)).mappings().all()
            for row in rows:
                closed = c.execute(update(attempts).where(attempts.c.id == row["current_attempt_id"],
                                                          attempts.c.state == "PENDING").values(state="FAILED", revision=attempts.c.revision+1))
                if closed.rowcount != 1:
                    continue
                c.execute(update(jobs).where(jobs.c.id == row["id"], jobs.c.revision == row["revision"])
                          .values(state="FAILED", error_code="QUEUE_DEADLINE", revision=jobs.c.revision+1))
                record_audit(c, Actor(row["tenant_id"], "reconciler",
                             frozenset()), "training.queue_deadline", row["id"])
                expired += 1
        return expired

    def heartbeat(self, claim: Claim) -> Job:
        with self.db.transaction() as c:
            row = self._owned(c, claim)
            c.execute(update(attempts).where(attempts.c.id == claim.job.attempt_id)
                      .values(heartbeat_at=time.time(), revision=attempts.c.revision+1))
            return job_from_row(row)

    def finish(self, claim: Claim, binding_id: str, evaluation_id: str) -> None:
        with self.db.transaction() as c:
            row = self._owned(c, claim)
            if row["cancel_requested"]:
                raise Conflict("Cancellation won the result commit race")
            binding = c.execute(select(models).where(models.c.id == binding_id)).mappings().one()
            evaluation = c.execute(select(evaluations).where(
                evaluations.c.id == evaluation_id)).mappings().one()
            if (binding["attempt_id"] != claim.job.attempt_id or
                evaluation["model_binding_id"] != binding_id or
                    evaluation["manifest_digest"] != binding["manifest_digest"]):
                raise Conflict("Result evidence does not match the attempt")
            c.execute(update(attempts).where(attempts.c.id == claim.job.attempt_id).values(
                state="SUCCEEDED", phase="COMPLETED", revision=attempts.c.revision+1))
            c.execute(update(jobs).where(jobs.c.id == row["id"]).values(state="SUCCEEDED",
                                                                        phase="COMPLETED", model_binding_id=binding_id, revision=jobs.c.revision+1))

    def stop(self, claim: Claim, code: str, confirmed: bool) -> None:
        with self.db.transaction() as c:
            row = self._owned(c, claim)
            state = ("CANCELLED" if row["cancel_requested"]
                     else "FAILED") if confirmed else "NEEDS_RECONCILIATION"
            c.execute(update(attempts).where(attempts.c.id == claim.job.attempt_id).values(
                state=state if confirmed else "LOST", revision=attempts.c.revision+1))
            c.execute(update(jobs).where(jobs.c.id == row["id"]).values(
                state=state, error_code=code, revision=jobs.c.revision+1))
