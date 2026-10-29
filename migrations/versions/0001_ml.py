"""Initial explicit ML schema. Only the migration command may create these tables."""

from alembic import op

from migrations.schema_v1 import metadata

revision = "0001_ml"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
