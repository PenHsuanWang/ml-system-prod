"""Persist allowlisted trace context with the transactional publication intent."""

import sqlalchemy as sa
from alembic import op

revision = "0005_trace_context"
down_revision = "0004_dataset_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("context_json", sa.Text(),
                                             nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("outbox_events", "context_json")
