"""Reserve rollout capacity until physical stop has been verified."""

from alembic import op
import sqlalchemy as sa

revision = "0003_runtime_admission"
down_revision = "0002_reconciliation_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("resources_released", sa.Boolean(),
                  nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("deployments", "resources_released")
