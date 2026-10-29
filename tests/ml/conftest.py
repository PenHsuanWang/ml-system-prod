
import os
from uuid import uuid4

import pytest

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from src.api.ml_schemas import TrainingIntent
from src.domain.ml import Actor, canonical_json
from src.infrastructure.persistence.ml_database import metadata


@pytest.fixture
def components(tmp_path):
    url = "sqlite:///"+str(tmp_path/"ml.db")
    admin = None
    schema = None
    if os.environ.get("ML_TEST_DATABASE_URL"):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.schema import CreateSchema
        admin = create_engine(os.environ["ML_TEST_DATABASE_URL"])
        schema = "ml_test_"+uuid4().hex
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = make_url(os.environ["ML_TEST_DATABASE_URL"]).update_query_dict(
            {"options": "-csearch_path="+schema}).render_as_string(hide_password=False)
    settings = MLSettings(database_url=url,
                          dataset_root=tmp_path/"datasets",
                          artifact_root=tmp_path/"artifacts", execution_root=tmp_path/"executions",
                          heartbeat_seconds=1, stale_after=4)
    result = build_components(settings)
    metadata.create_all(result.database.engine)
    from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
    SQLDatasetCatalog(result.database, settings.dataset_root).seed_builtin()
    yield result
    result.database.close()
    if admin is not None:
        from sqlalchemy.schema import DropSchema
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def actor():
    return Actor("tenant-one", "scientist", frozenset({"ml:train", "ml:cancel",
                                                       "ml:model:read", "ml:model:approve", "ml:deploy", "ml:predict"}))


@pytest.fixture
def intent():
    return canonical_json(TrainingIntent(recipe={"id": "nn_classifier", "version": "1.0.0"},
                                         dataset={"id": "dataset-demo", "version": "2026-10-28-v1"}).model_dump())
