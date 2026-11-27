"""Add the durable DatasetBuilder queue after the dataset lineage schema."""

import sqlalchemy as sa
from alembic import op

revision = "0009_dataset_build_jobs"
down_revision = "0008_dataset_lineage"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_build_jobs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("dataset_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("spec_json", sa.Text, nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("deadline", sa.Float, nullable=False),
        sa.Column("eligible_at", sa.Float, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.Float),
        sa.Column("error_code", sa.String(64)),
        sa.Column("content_digest", sa.String(64)),
        sa.Column("receipt_digest", sa.String(64)),
    )
    op.create_index(
        "ix_dataset_build_jobs_tenant_created", "dataset_build_jobs", ["tenant_id", "created_at"]
    )
    op.create_index("ix_dataset_build_jobs_queue", "dataset_build_jobs", ["state", "eligible_at"])


def downgrade() -> None:
    op.drop_index("ix_dataset_build_jobs_queue", table_name="dataset_build_jobs")
    op.drop_index("ix_dataset_build_jobs_tenant_created", table_name="dataset_build_jobs")
    op.drop_table("dataset_build_jobs")
