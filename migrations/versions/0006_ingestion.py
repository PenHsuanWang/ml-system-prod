"""Approved sources, durable ingestion queue and immutable raw snapshot catalog."""

import sqlalchemy as sa
from alembic import op

revision = "0006_ingestion"
down_revision = "0005_trace_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_source_definitions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_id", "version"),
    )
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("source_version", sa.String(128), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("limits_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_id", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("deadline", sa.Float(), nullable=False),
        sa.Column("attempt_id", sa.String(64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acquisition_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconciliation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_token", sa.String(64)),
        sa.Column("lease_until", sa.Float(), nullable=False, server_default="0"),
        sa.Column("staged_json", sa.Text()),
        sa.Column("publication_intent_json", sa.Text()),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("next_retry_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_ingestion_claim", "ingestion_jobs", ["state", "next_retry_at", "lease_until"]
    )
    op.create_index("ix_ingestion_tenant", "ingestion_jobs", ["tenant_id", "created_at"])
    op.create_table(
        "ingestion_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("ingestion_jobs.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("heartbeat_at", sa.Float(), nullable=False),
        sa.Column("staged_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("job_id", "number"),
    )
    op.create_table(
        "ingestion_idempotency_keys",
        sa.Column("scope_digest", sa.String(64), primary_key=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("ingestion_jobs.id"), nullable=False),
    )
    op.create_table(
        "raw_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "job_id", sa.String(64), sa.ForeignKey("ingestion_jobs.id"), nullable=False, unique=True
        ),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_raw_snapshots_tenant", "raw_snapshots", ["tenant_id", "created_at"])


def downgrade() -> None:
    for name in (
        "raw_snapshots",
        "ingestion_idempotency_keys",
        "ingestion_attempts",
        "ingestion_jobs",
        "ingestion_source_definitions",
    ):
        op.drop_table(name)
