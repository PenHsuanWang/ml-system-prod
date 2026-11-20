"""Disposable checkout and public fixtures; no user configuration or external services."""

import os
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest

from bootstrap.init_config import SetupSpec, example_environment, local_defaults
from bootstrap.init_files import PrivateFiles

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project's workspace"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for name in [
        ".gitignore",
        "pyproject.toml",
        ".env.ml.production.example",
        "compose.ml.yaml",
        "compose.ml.ingestion.yaml",
        "compose.ml.databricks.yaml",
        "compose.ml.databricks-token.yaml",
        "compose.ml.external-mlflow.yaml",
    ]:
        shutil.copyfile(ROOT / name, root / name)
    subprocess.run(["git", "-C", str(root), "add", ".gitignore", "pyproject.toml"], check=True)
    return root


@pytest.fixture
def spec(project):
    return SetupSpec(project, project / ".local/ml-system", "compose", local_defaults({}))


@pytest.fixture
def external(project):
    trust = ssl.create_default_context().get_ca_certs(binary_form=True)[0]
    ca = project / "ca.pem"
    ca.write_text(ssl.DER_cert_to_PEM_cert(trust))
    private = project / ".local/ml-system"
    values = example_environment(project)
    values.update(
        ML_RELEASE_IMAGE="registry.example/ml@sha256:" + "a" * 64,
        ML_IMAGE_DIGEST="sha256:" + "a" * 64,
        ML_CODE_REVISION="b" * 40,
        ML_UID=str(os.geteuid()),
        ML_GID=str(os.getegid()),
        ML_RUNTIME_ROOT=str(project / "var"),
        ML_OPERATOR_DEFINITIONS_DIR=str(private / "operator-definitions"),
        ML_EXTERNAL_MLFLOW_URI="https://tracking.example",
        ML_MLFLOW_CA_FILE=str(ca),
        DATABRICKS_TOKEN="test-only-token",
    )
    result = SetupSpec(project, private, "external", values)
    result.private_values = {
        "ML_DATABASE_URL": "postgresql+psycopg://user:dummy@postgres:5432/ml",
        "ML_RABBITMQ_URL": "amqp://user:dummy@rabbitmq:5672/",
        "ML_REDIS_URL": "redis://:dummy@redis:6379/0",
        "APP_MONGO_URI": "mongodb://user:dummy@mongo:27017/?authSource=admin",
        "APP_JWT_SECRET": "j" * 64,
        "MLFLOW_TRACKING_USERNAME": "writer",
        "MLFLOW_TRACKING_PASSWORD": "writer-test-only",
        "ML_SERVING_MLFLOW_TOKEN": "reader-test-only",
    }
    result.connections = {
        "warehouse-main": {
            "server_hostname": "dbc-approved.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/0123abcd",
            "access_token_env": "DATABRICKS_TOKEN",
        }
    }
    return result


@pytest.fixture
def files(project):
    return PrivateFiles(project)
