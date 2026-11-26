"""Real Compose rendering without accessing a Docker daemon or private inputs."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("authentication", ["oauth", "token"])
def test_compose_discovery_identity_is_management_only(tmp_path, authentication):
    root = Path(__file__).resolve().parents[3]
    fixture = tmp_path / "synthetic.json"
    fixture.write_text("{}")
    env = dict(
        os.environ,
        **{
            "ML_DB_PASSWORD": "synthetic-db",
            "MLFLOW_DB_PASSWORD": "synthetic-tracker",
            "ML_MQ_PASSWORD": "synthetic-mq",
            "ML_MONGO_PASSWORD": "synthetic-mongo",
            "ML_S3_PASSWORD": "synthetic-objects",
            "APP_JWT_SECRET": "s" * 48,
            "ML_INGESTION_INPUT_DIR": str(tmp_path),
            "ML_DATABRICKS_CONNECTIONS_CONFIG": str(fixture),
            "ML_DATABRICKS_SOURCES_CONFIG": str(fixture),
            "ML_DATABRICKS_CLIENT_SECRET_FILE": str(fixture),
            "DATABRICKS_TOKEN": "synthetic-worker-token",
            "ML_CATALOG_CONNECTIONS_CONFIG": str(fixture),
            "ML_CATALOG_POLICY_CONFIG": str(fixture),
            "ML_DISCOVERY_CLIENT_SECRET_FILE": str(fixture),
            "ML_DISCOVERY_DATABRICKS_TOKEN": "synthetic-discovery-token",
        },
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        "compose.ml.yaml",
        "-f",
        "compose.ml.ingestion.yaml",
        "-f",
        "compose.ml.databricks.yaml"
        if authentication == "oauth"
        else "compose.ml.databricks-token.yaml",
        "-f",
        "compose.ml.catalog.yaml",
        "--profile",
        "ingestion",
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=True)
    model = json.loads(result.stdout)
    services = model["services"]
    assert services["management"]["build"]["target"] == "ingestion"
    assert "DATABRICKS_TOKEN" not in services["management"]["environment"]
    for name, service in services.items():
        if name != "management":
            assert "ML_DISCOVERY_DATABRICKS_TOKEN" not in service.get("environment", {})
            assert not any("discovery" in m.get("target", "") for m in service.get("volumes", []))
    assert "ML_CATALOG_ENABLED" not in services["ingestion-worker"]["environment"]
    assert {s["source"] for s in services["management"]["secrets"]} == {"discovery_client_secret"}
