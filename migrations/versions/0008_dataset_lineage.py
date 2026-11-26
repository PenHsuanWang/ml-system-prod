"""Bind verified builder provenance atomically to immutable dataset versions."""

import sqlalchemy as sa
from alembic import op

revision = "0008_dataset_lineage"
down_revision = "0007_prediction_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_build_lineage",
        sa.Column(
            "dataset_version_id",
            sa.String(64),
            sa.ForeignKey("dataset_versions.id"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("builder_id", sa.String(128), nullable=False),
        sa.Column("builder_version", sa.String(64), nullable=False),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dataset_build_lineage")
