"""Immutable builder receipts share the existing authoritative Dataset Catalog."""

from sqlalchemy import Column, Float, ForeignKey, String, Table, Text

from src.infrastructure.persistence.ml_database import metadata

dataset_lineage = Table(
    "dataset_build_lineage",
    metadata,
    Column("dataset_version_id", ForeignKey("dataset_versions.id"), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("builder_id", String(128), nullable=False),
    Column("builder_version", String(64), nullable=False),
    Column("receipt_digest", String(64), nullable=False),
    Column("receipt_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
