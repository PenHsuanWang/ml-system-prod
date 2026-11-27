"""Additive inference metadata within the existing model registry database."""

from sqlalchemy import Column, Float, ForeignKey, Integer, String, Table, Text

from src.infrastructure.persistence.ml_database import metadata

public_contracts = Table(
    "model_public_contracts",
    metadata,
    Column("binding_id", ForeignKey("model_version_bindings.id"), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("contract_digest", String(64), nullable=False),
    Column("document_json", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
usage_reviews = Table(
    "model_usage_reviews",
    metadata,
    Column("binding_id", ForeignKey("model_version_bindings.id"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("review_json", Text, nullable=False),
    Column("review_digest", String(64), nullable=False),
    Column("reviewer_id", String(128), nullable=False),
    Column("created_at", Float, nullable=False),
)
