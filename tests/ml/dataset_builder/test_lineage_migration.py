"""Additive migration preserves existing datasets and requires no history rewriting."""

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from src.infrastructure.persistence.ml_database import datasets


@pytest.fixture
def migration_url(tmp_path):
    url = os.environ.get("ML_TEST_DATABASE_URL")
    admin = None
    schema = None
    if url:
        admin = create_engine(url)
        schema = "wp3_migration_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = (
            make_url(url)
            .update_query_dict({"options": "-csearch_path=" + schema})
            .render_as_string(hide_password=False)
        )
    else:
        url = "sqlite:///" + str(tmp_path / "migration.db")
    try:
        yield url
    finally:
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()


def test_lineage_upgrade_downgrade_preserves_preexisting_versions(migration_url):
    config = Config("alembic.ini")
    config.attributes["database_url"] = migration_url
    command.upgrade(config, "0007_prediction_contracts")
    engine = create_engine(migration_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(datasets).values(
                    id="legacy-version",
                    tenant_id="tenant-one",
                    dataset_key="legacy",
                    version="v1",
                    content_digest="a" * 64,
                    snapshot_ref="legacy-test-fixture",
                    summary_json="{}",
                    created_at=0.0,
                )
            )
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert "dataset_build_lineage" in inspect(engine).get_table_names()
        assert "dataset_build_jobs" in inspect(engine).get_table_names()
        command.downgrade(config, "0008_dataset_lineage")
        tables = inspect(engine).get_table_names()
        assert "dataset_build_lineage" in tables and "dataset_build_jobs" not in tables
        command.upgrade(config, "head")
        foreign_keys = inspect(engine).get_foreign_keys("dataset_build_lineage")
        assert foreign_keys[0]["referred_table"] == "dataset_versions"
        with engine.connect() as connection:
            assert (
                connection.execute(select(datasets).where(datasets.c.id == "legacy-version"))
                .mappings()
                .one()["content_digest"]
                == "a" * 64
            )
        command.downgrade(config, "0007_prediction_contracts")
        assert "dataset_build_lineage" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            row = connection.execute(select(datasets).where(datasets.c.id == "legacy-version"))
            assert row.mappings().one()["id"] == "legacy-version"
        command.upgrade(config, "head")
    finally:
        engine.dispose()
