"""SQL schema shared by explicit migrations; never created during API startup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

from sqlalchemy import Boolean, Column, create_engine, event, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text, UniqueConstraint
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.domain.ml import Unavailable

metadata = MetaData()


def identity() -> Column[str]:
    return Column("id", String(64), primary_key=True)


datasets = Table("dataset_versions", metadata, identity(),
                 Column("tenant_id", String(128), nullable=False),
                 Column("dataset_key", String(128), nullable=False),
                 Column("version", String(64), nullable=False),
                 Column("content_digest", String(64), nullable=False),
                 Column("snapshot_ref", Text, nullable=False),
                 Column("summary_json", Text, nullable=False),
                 Column("created_at", Float, nullable=False),
                 UniqueConstraint("tenant_id", "dataset_key", "version"))


tenants = Table("ml_tenants", metadata,
                Column("tenant_id", String(128), primary_key=True),
                Column("revision", Integer, nullable=False, default=0))
jobs = Table("training_jobs", metadata, identity(),
             Column("tenant_id", String(128), nullable=False),
             Column("owner_id", String(128), nullable=False),
             Column("state", String(32), nullable=False),
             Column("phase", String(32), nullable=False),
             Column("revision", Integer, nullable=False),
             Column("current_attempt_id", String(64), nullable=False),
             Column("spec_json", Text, nullable=False),
             Column("spec_digest", String(64), nullable=False),
             Column("created_at", Float, nullable=False),
             Column("deadline", Float, nullable=False),
             Column("queue_deadline", Float, nullable=False),
             Column("cancel_requested", Boolean, nullable=False, default=False),
             Column("error_code", String(64)),
             Column("model_binding_id", String(64)))
Index("ix_jobs_tenant_created", jobs.c.tenant_id, jobs.c.created_at)
Index("ix_jobs_state_deadline", jobs.c.state, jobs.c.deadline)
Index("ix_jobs_queue_deadline", jobs.c.state, jobs.c.queue_deadline)
attempts = Table("training_attempts", metadata, identity(),
                 Column("job_id", ForeignKey("training_jobs.id"), nullable=False),
                 Column("number", Integer, nullable=False),
                 Column("state", String(32), nullable=False),
                 Column("owner_token", String(64)),
                 Column("execution_ref", Text),
                 Column("heartbeat_at", Float),
                 Column("phase", String(32), nullable=False),
                 Column("revision", Integer, nullable=False),
                 Column("run_id", String(128)),
                 Column("reconciliation_count", Integer, nullable=False, default=0),
                 Column("result_json", Text),
                 UniqueConstraint("job_id", "number"))
Index("uq_attempt_live_owner", attempts.c.job_id, unique=True,
      postgresql_where=attempts.c.state.in_(["STARTING", "RUNNING", "LOST"]),
      sqlite_where=attempts.c.state.in_(["STARTING", "RUNNING", "LOST"]))
keys = Table("idempotency_keys", metadata,
             Column("scope_digest", String(64), primary_key=True),
             Column("request_fingerprint", String(64), nullable=False),
             Column("job_id", ForeignKey("training_jobs.id"), nullable=False),
             Column("expires_at", Float, nullable=False))
outbox = Table("outbox_events", metadata, identity(),
               Column("context_json", Text, nullable=False, default="{}"),
               Column("job_id", ForeignKey("training_jobs.id"), nullable=False),
               Column("attempt_id", ForeignKey("training_attempts.id"), nullable=False),
               Column("spec_digest", String(64), nullable=False),
               Column("state", String(32), nullable=False),
               Column("lease_token", String(64)),
               Column("lease_until", Float, nullable=False, default=0),
               Column("tries", Integer, nullable=False, default=0),
               Column("next_retry_at", Float, nullable=False, default=0))
Index("ix_outbox_retry", outbox.c.state, outbox.c.next_retry_at)
models = Table("model_version_bindings", metadata, identity(),
               Column("tenant_id", String(128), nullable=False),
               Column("attempt_id", ForeignKey("training_attempts.id"), nullable=False, unique=True),
               Column("registered_name", String(256), nullable=False),
               Column("version", String(64), nullable=False),
               Column("run_id", String(128), nullable=False),
               Column("artifact_ref", Text, nullable=False),
               Column("manifest_digest", String(64), nullable=False),
               Column("binding_digest", String(64), nullable=False, unique=True),
               UniqueConstraint("registered_name", "version"))
evaluations = Table("evaluation_results", metadata, identity(),
                    Column("model_binding_id", ForeignKey(
                        "model_version_bindings.id"), nullable=False),
                    Column("manifest_digest", String(64), nullable=False),
                    Column("dataset_digest", String(64), nullable=False),
                    Column("policy_version", String(128), nullable=False),
                    Column("evaluator_version", String(128), nullable=False),
                    Column("decision", String(32), nullable=False),
                    Column("metrics_json", Text, nullable=False),
                    Column("report_digest", String(64), nullable=False),
                    Column("created_at", Float, nullable=False))
approvals = Table("promotion_approvals", metadata, identity(),
                  Column("tenant_id", String(128), nullable=False),
                  Column("model_binding_id", ForeignKey(
                      "model_version_bindings.id"), nullable=False),
                  Column("evaluation_id", ForeignKey("evaluation_results.id"), nullable=False),
                  Column("report_digest", String(64), nullable=False),
                  Column("binding_digest", String(64), nullable=False),
                  Column("environment", String(32), nullable=False),
                  Column("approver_id", String(128), nullable=False),
                  Column("created_at", Float, nullable=False))
deployments = Table("deployments", metadata, identity(),
                    Column("tenant_id", String(128), nullable=False),
                    Column("endpoint_id", String(128), nullable=False),
                    Column("generation", Integer, nullable=False),
                    Column("model_binding_id", ForeignKey(
                        "model_version_bindings.id"), nullable=False),
                    Column("approval_id", ForeignKey("promotion_approvals.id"), nullable=False),
                    Column("state", String(32), nullable=False),
                    Column("expected_route_revision", Integer, nullable=False),
                    Column("resources_released", Boolean, nullable=False, default=False),
                    Column("spec_json", Text, nullable=False),
                    Column("spec_digest", String(64), nullable=False),
                    UniqueConstraint("tenant_id", "endpoint_id", "generation"))
routes = Table("endpoint_routes", metadata,
               Column("tenant_id", String(128), primary_key=True),
               Column("endpoint_id", String(128), primary_key=True),
               Column("revision", Integer, nullable=False),
               Column("deployment_id", ForeignKey("deployments.id")),
               Column("generation", Integer, nullable=False))
audit = Table("audit_events", metadata, identity(),
              Column("tenant_id", String(128), nullable=False),
              Column("actor_id", String(128), nullable=False),
              Column("action", String(64), nullable=False),
              Column("subject_id", String(128), nullable=False),
              Column("created_at", Float, nullable=False))


class Database:
    def __init__(self, url: str) -> None:
        options: dict[str, object] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._sqlite_pragmas)
            event.listen(self.engine, "begin", self._sqlite_begin)

    @staticmethod
    def _sqlite_pragmas(connection: sqlite3.Connection, _: object) -> None:
        connection.isolation_level = None
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @staticmethod
    def _sqlite_begin(connection: Connection) -> None:
        # SQLite has no row locks: acquire the development UoW's write lock
        # before reading so a later writer cannot overwrite a stale snapshot.
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        try:
            with self.engine.begin() as connection:
                yield connection
        except IntegrityError:
            raise
        except SQLAlchemyError:
            raise Unavailable("Application storage is unavailable") from None

    def ready(self) -> bool:
        from sqlalchemy import select
        with self.engine.connect() as connection:
            # A reachable old schema is not ready for the newly deployed roles.
            for table in metadata.sorted_tables:
                connection.execute(select(table).limit(0))
        return True

    def close(self) -> None:
        self.engine.dispose()
