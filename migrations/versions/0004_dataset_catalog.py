"""Immutable tenant dataset catalog and fixed public engineering fixture."""
from alembic import op
import sqlalchemy as sa

revision = "0004_dataset_catalog"
down_revision = "0003_runtime_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table("dataset_versions",
                            sa.Column("id", sa.String(64), primary_key=True),
                            sa.Column("tenant_id", sa.String(128), nullable=False),
                            sa.Column("dataset_key", sa.String(128), nullable=False),
                            sa.Column("version", sa.String(64), nullable=False),
                            sa.Column("content_digest", sa.String(64), nullable=False),
                            sa.Column("snapshot_ref", sa.Text(), nullable=False),
                            sa.Column("summary_json", sa.Text(), nullable=False),
                            sa.Column("created_at", sa.Float(), nullable=False),
                            sa.UniqueConstraint("tenant_id", "dataset_key", "version"))
    op.bulk_insert(table, [{"id": "builtin-dataset-demo-v1", "tenant_id": "__public__",
                            "dataset_key": "dataset-demo", "version": "2026-10-28-v1",
                            "content_digest": "0d3b1bc213b97137c2046846ae72f600739146fdb61f8d2b0215558804ae1885",
                            "snapshot_ref": "builtin:dataset-demo-v1",
                            "summary_json": '{"feature_schema_version":"1","features":["sensor_a","sensor_b"],"samples":{"test":64,"training":384,"validation":64}}',
                            "created_at": 0.0}])


def downgrade() -> None:
    op.drop_table("dataset_versions")
