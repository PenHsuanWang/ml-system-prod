"""SQL storage for immutable build intents and bounded worker leases."""

from sqlalchemy import Column, Float, Index, Integer, String, Table, Text

from src.infrastructure.persistence.ml_database import metadata

dataset_build_jobs = Table(
    "dataset_build_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("dataset_id", String(64), nullable=False),
    Column("version", String(64), nullable=False),
    Column("key_digest", String(64), nullable=False, unique=True),
    Column("state", String(16), nullable=False),
    Column("spec_json", Text, nullable=False),
    Column("spec_digest", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("deadline", Float, nullable=False),
    Column("eligible_at", Float, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("lease_token", String(64)),
    Column("lease_until", Float),
    Column("error_code", String(64)),
    Column("content_digest", String(64)),
    Column("receipt_digest", String(64)),
)
Index(
    "ix_dataset_build_jobs_tenant_created",
    dataset_build_jobs.c.tenant_id,
    dataset_build_jobs.c.created_at,
)
Index(
    "ix_dataset_build_jobs_queue",
    dataset_build_jobs.c.state,
    dataset_build_jobs.c.eligible_at,
)
