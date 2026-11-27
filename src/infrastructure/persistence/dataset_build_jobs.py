"""Durable SQL queue for deterministic, immutable dataset builds."""

import time
from collections.abc import Callable, Mapping
from dataclasses import fields
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement

from src.domain.datasets.build_jobs import (
    BuildState,
    DatasetBuildClaim,
    DatasetBuildJob,
    DatasetBuildSpec,
)
from src.domain.datasets.lineage import DatasetBuildReceipt
from src.domain.datasets.models import DatasetVersion
from src.domain.ml import Actor, CapacityExceeded, Conflict, IdempotencyConflict, NotFound, digest
from src.domain.pagination import CollectionWindow
from src.infrastructure.persistence.dataset_build_schema import dataset_build_jobs as builds
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit

BUILD_LEASE_SECONDS = 300
BUILD_DEADLINE_SECONDS = 1800
BUILD_MAX_ATTEMPTS = 3
BUILD_MAX_PENDING = 100
PENDING = ("QUEUED", "RUNNING")


def _job(row: Mapping[str, Any]) -> DatasetBuildJob:
    return DatasetBuildJob(
        **{
            field.name: row["id" if field.name == "job_id" else field.name]
            for field in fields(DatasetBuildJob)
        }
    )


class SQLDatasetBuildJobs:
    def __init__(self, database: Database, *, clock: Callable[[], float] = time.time) -> None:
        self.db, self.clock = database, clock

    def submit(
        self, actor: Actor, key: str, spec: DatasetBuildSpec, raw: str, spec_digest: str
    ) -> DatasetBuildJob:
        key_digest = digest(["dataset-build-submit-v1", actor.tenant_id, actor.actor_id, key])
        with self.db.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            row = (
                connection.execute(
                    select(builds).where(
                        builds.c.key_digest == key_digest,
                        builds.c.tenant_id == actor.tenant_id,
                        builds.c.owner_id == actor.actor_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["spec_digest"] != spec_digest or row["spec_json"] != raw:
                    raise IdempotencyConflict("Key already used for a different dataset build")
                return _job(row)
            pending = (
                connection.scalar(
                    select(func.count())
                    .select_from(builds)
                    .where(builds.c.tenant_id == actor.tenant_id, builds.c.state.in_(PENDING))
                )
                or 0
            )
            if pending >= BUILD_MAX_PENDING:
                raise CapacityExceeded("Tenant dataset build backlog is full")
            now, job_id = self.clock(), str(uuid4())
            row = (
                connection.execute(
                    insert(builds)
                    .values(
                        id=job_id,
                        tenant_id=actor.tenant_id,
                        owner_id=actor.actor_id,
                        dataset_id=spec.dataset_id,
                        version=spec.version,
                        key_digest=key_digest,
                        state="QUEUED",
                        spec_json=raw,
                        spec_digest=spec_digest,
                        created_at=now,
                        updated_at=now,
                        deadline=now + BUILD_DEADLINE_SECONDS,
                        eligible_at=now,
                        attempt_count=0,
                    )
                    .returning(*builds.c)
                )
                .mappings()
                .one()
            )
            record_audit(connection, actor, "dataset.build.submit", job_id)
            return _job(row)

    @staticmethod
    def _visible(actor: Actor) -> ColumnElement[bool]:
        tenant = builds.c.tenant_id == actor.tenant_id
        if "ml:jobs:read_all" in actor.permissions:
            return tenant
        return and_(tenant, builds.c.owner_id == actor.actor_id)

    def get(self, actor: Actor, job_id: str) -> DatasetBuildJob:
        with self.db.transaction() as connection:
            row = (
                connection.execute(
                    select(builds).where(
                        self._visible(actor),
                        builds.c.id == job_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("Dataset build job not found")
            return _job(row)

    def list_jobs(
        self, actor: Actor, window: CollectionWindow, state: BuildState | None
    ) -> list[DatasetBuildJob]:
        statement = select(builds).where(self._visible(actor))
        if state is not None:
            statement = statement.where(builds.c.state == state)
        if window.created_after is not None:
            statement = statement.where(builds.c.created_at > window.created_after)
        if window.created_before is not None:
            statement = statement.where(builds.c.created_at < window.created_before)
        statement = (
            statement.order_by(builds.c.created_at.desc(), builds.c.id.desc())
            .limit(window.limit + 1)
            .offset(window.offset)
        )
        with self.db.transaction() as connection:
            return [_job(row) for row in connection.execute(statement).mappings()]

    def _expire(self, connection: Connection, now: float) -> None:
        expired = connection.execute(
            select(
                builds.c.id,
                builds.c.tenant_id,
                builds.c.owner_id,
                builds.c.deadline,
            )
            .where(
                builds.c.state.in_(PENDING),
                or_(
                    builds.c.deadline <= now,
                    and_(
                        builds.c.state == "RUNNING",
                        builds.c.lease_until <= now,
                        builds.c.attempt_count >= BUILD_MAX_ATTEMPTS,
                    ),
                ),
            )
            .order_by(builds.c.deadline, builds.c.id)
            .limit(25)
            .with_for_update(skip_locked=True)
        ).mappings()
        for row in expired:
            code = (
                "BUILD_DEADLINE_EXCEEDED" if row["deadline"] <= now else "BUILD_ATTEMPTS_EXHAUSTED"
            )
            connection.execute(
                update(builds)
                .where(builds.c.id == row["id"])
                .values(
                    state="FAILED",
                    error_code=code,
                    updated_at=now,
                    lease_token=None,
                    lease_until=None,
                )
            )
            record_audit(
                connection,
                Actor(row["tenant_id"], row["owner_id"], frozenset()),
                "dataset.build.fail",
                row["id"],
            )

    def claim(self, job_id: str | None = None) -> DatasetBuildClaim | None:
        with self.db.transaction() as connection:
            now = self.clock()
            self._expire(connection, now)
            query = select(builds.c.id, builds.c.deadline).where(
                builds.c.deadline > now,
                builds.c.attempt_count < BUILD_MAX_ATTEMPTS,
                or_(
                    and_(builds.c.state == "QUEUED", builds.c.eligible_at <= now),
                    and_(builds.c.state == "RUNNING", builds.c.lease_until <= now),
                ),
            )
            if job_id is not None:
                query = query.where(builds.c.id == job_id)
            selected = connection.execute(
                query.order_by(builds.c.created_at, builds.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if selected is None:
                return None
            now = self.clock()
            if selected.deadline <= now:
                return None
            token = uuid4().hex
            row = (
                connection.execute(
                    update(builds)
                    .where(builds.c.id == selected.id)
                    .values(
                        state="RUNNING",
                        lease_token=token,
                        lease_until=min(now + BUILD_LEASE_SECONDS, selected.deadline),
                        updated_at=now,
                        attempt_count=builds.c.attempt_count + 1,
                        error_code=None,
                    )
                    .returning(*builds.c)
                )
                .mappings()
                .one()
            )
            return DatasetBuildClaim(_job(row), token)

    @staticmethod
    def _owned(claim: DatasetBuildClaim, now: float) -> ColumnElement[bool]:
        return and_(
            builds.c.id == claim.job.job_id,
            builds.c.tenant_id == claim.job.tenant_id,
            builds.c.owner_id == claim.job.owner_id,
            builds.c.state == "RUNNING",
            builds.c.spec_json == claim.job.spec_json,
            builds.c.spec_digest == claim.job.spec_digest,
            builds.c.lease_token == claim.token,
            builds.c.lease_until > now,
            builds.c.deadline > now,
        )

    @staticmethod
    def _lock_claim(connection: Connection, claim: DatasetBuildClaim) -> float | None:
        deadline = connection.scalar(
            select(builds.c.deadline)
            .where(builds.c.id == claim.job.job_id, builds.c.lease_token == claim.token)
            .with_for_update()
        )
        return cast(float | None, deadline)

    def renew(self, claim: DatasetBuildClaim) -> bool:
        with self.db.transaction() as connection:
            deadline = self._lock_claim(connection, claim)
            if deadline is None:
                return False
            now = self.clock()
            renewed = connection.execute(
                update(builds)
                .where(self._owned(claim, now))
                .values(lease_until=min(deadline, now + BUILD_LEASE_SECONDS))
            )
            return cast(int, renewed.rowcount) == 1

    def complete(
        self,
        claim: DatasetBuildClaim,
        connection: Connection,
        actor: Actor,
        result: DatasetVersion,
        receipt: DatasetBuildReceipt,
    ) -> None:
        """Called from the same transaction that verifies and registers the dataset."""
        job = claim.job
        spec = DatasetBuildSpec.verified(job.spec_json, job.spec_digest)
        document = receipt.document()
        receipt_refs = {
            (item["role"], item["snapshot_id"], item["manifest_digest"])
            for item in document["inputs"]
        }
        if (
            (result.dataset_id, result.version, result.tenant_id)
            != (job.dataset_id, job.version, job.tenant_id)
            or (actor.tenant_id, actor.actor_id) != (job.tenant_id, job.owner_id)
            or document["builder"] != {"id": spec.builder_id, "version": spec.builder_version}
            or receipt_refs
            != {(ref.role, ref.snapshot_id, ref.manifest_digest) for ref in spec.inputs}
            or any(
                document["configuration"].get(key) != value
                for key, value in spec.config.to_dict().items()
            )
        ):
            raise Conflict("Dataset build publication does not match its immutable intent")
        if self._lock_claim(connection, claim) is None:
            raise Conflict("Dataset build lease no longer owns publication")
        now = self.clock()
        changed = connection.execute(
            update(builds)
            .where(self._owned(claim, now))
            .values(
                state="SUCCEEDED",
                content_digest=result.content_digest,
                receipt_digest=receipt.receipt_digest,
                updated_at=now,
                lease_token=None,
                lease_until=None,
            )
        )
        if changed.rowcount != 1:
            raise Conflict("Dataset build lease no longer owns publication")
        record_audit(connection, actor, "dataset.build.succeed", job.job_id)

    def fail(self, claim: DatasetBuildClaim, error_code: str, *, retry: bool = False) -> None:
        with self.db.transaction() as connection:
            if self._lock_claim(connection, claim) is None:
                return
            now = self.clock()
            requeue = (
                retry
                and claim.job.attempt_count < BUILD_MAX_ATTEMPTS
                and now + 5 < claim.job.deadline
            )
            updated = connection.execute(
                update(builds)
                .where(self._owned(claim, now))
                .values(
                    state="QUEUED" if requeue else "FAILED",
                    updated_at=now,
                    error_code=error_code,
                    eligible_at=now + 5,
                    lease_token=None,
                    lease_until=None,
                )
            )
            if updated.rowcount == 1:
                record_audit(
                    connection,
                    Actor(claim.job.tenant_id, claim.job.owner_id, frozenset()),
                    "dataset.build.retry" if requeue else "dataset.build.fail",
                    claim.job.job_id,
                )
