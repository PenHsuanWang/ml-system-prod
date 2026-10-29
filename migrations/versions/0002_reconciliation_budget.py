"""Persist bounded automatic recovery without treating uncertainty as death."""

import sqlalchemy as sa
from alembic import op

revision = "0002_reconciliation_budget"
down_revision = "0001_ml"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("training_attempts", sa.Column("reconciliation_count", sa.Integer(),
                  nullable=False, server_default="0"))
    op.create_index("ix_jobs_queue_deadline", "training_jobs", ["state", "queue_deadline"])


def downgrade() -> None:
    op.drop_index("ix_jobs_queue_deadline", table_name="training_jobs")
    op.drop_column("training_attempts", "reconciliation_count")
