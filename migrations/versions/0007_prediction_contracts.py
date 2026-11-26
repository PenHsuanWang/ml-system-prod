"""Add public inference contracts and separately reviewed usage revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0007_prediction_contracts"
down_revision = "0006_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_public_contracts",
        sa.Column(
            "binding_id",
            sa.String(64),
            sa.ForeignKey("model_version_bindings.id"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "model_usage_reviews",
        sa.Column(
            "binding_id",
            sa.String(64),
            sa.ForeignKey("model_version_bindings.id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("review_json", sa.Text(), nullable=False),
        sa.Column("review_digest", sa.String(64), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("model_usage_reviews")
    op.drop_table("model_public_contracts")
