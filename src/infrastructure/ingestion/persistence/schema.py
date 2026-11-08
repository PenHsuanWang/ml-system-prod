"""Additive ingress schema; shared metadata is registered explicitly by Bootstrap."""

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from src.infrastructure.persistence.ml_database import metadata as metadata

source_definitions = Table(
    "ingestion_source_definitions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("source_id", String(128), nullable=False),
    Column("version", String(128), nullable=False),
    Column("definition_json", Text, nullable=False),
    Column("configuration_digest", String(64), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("tenant_id", "source_id", "version"),
)

ingestion_jobs = Table(
    "ingestion_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("source_id", String(128), nullable=False),
    Column("source_version", String(128), nullable=False),
    Column("source_digest", String(64), nullable=False),
    Column("limits_json", Text, nullable=False),
    Column("state", String(32), nullable=False),
    Column("revision", Integer, nullable=False, default=0),
    Column("snapshot_id", String(64), nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Column("deadline", Float, nullable=False),
    Column("attempt_id", String(64)),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("acquisition_count", Integer, nullable=False, default=0),
    Column("reconciliation_count", Integer, nullable=False, default=0),
    Column("owner_token", String(64)),
    Column("lease_until", Float, nullable=False, default=0),
    Column("staged_json", Text),
    Column("publication_intent_json", Text),
    Column("result_json", Text),
    Column("error_code", String(64)),
    Column("next_retry_at", Float, nullable=False, default=0),
    Column("cancel_requested", Boolean, nullable=False, default=False),
)
Index(
    "ix_ingestion_claim",
    ingestion_jobs.c.state,
    ingestion_jobs.c.next_retry_at,
    ingestion_jobs.c.lease_until,
)
Index("ix_ingestion_tenant", ingestion_jobs.c.tenant_id, ingestion_jobs.c.created_at)

ingestion_attempts = Table(
    "ingestion_attempts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("job_id", ForeignKey("ingestion_jobs.id"), nullable=False),
    Column("number", Integer, nullable=False),
    Column("owner_token", String(64), nullable=False),
    Column("state", String(32), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("heartbeat_at", Float, nullable=False),
    Column("staged_json", Text),
    Column("error_code", String(64)),
    UniqueConstraint("job_id", "number"),
)

ingestion_keys = Table(
    "ingestion_idempotency_keys",
    metadata,
    Column("scope_digest", String(64), primary_key=True),
    Column("request_fingerprint", String(64), nullable=False),
    Column("job_id", ForeignKey("ingestion_jobs.id"), nullable=False),
)

raw_snapshots = Table(
    "raw_snapshots",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("job_id", ForeignKey("ingestion_jobs.id"), nullable=False, unique=True),
    Column("manifest_digest", String(64), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("snapshot_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("ix_raw_snapshots_tenant", raw_snapshots.c.tenant_id, raw_snapshots.c.created_at)
