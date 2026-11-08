"""Parse the real Compose model; these checks do not claim containers have started."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def compose_command():
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    if shutil.which("docker"):
        check = subprocess.run(["docker", "compose", "version"], capture_output=True)
        if check.returncode == 0:
            return ["docker", "compose"]
    pytest.skip("Docker Compose CLI is required for actual configuration parsing")


@pytest.mark.parametrize("databricks", [False, True])
def test_worker_and_management_share_raw_storage_with_least_privilege_mounts(tmp_path, databricks):
    secret_value = "not-a-real-oauth-secret-for-compose-test"
    secret = tmp_path / "oauth-secret"
    secret.write_text(secret_value)
    approved = tmp_path / "approved"
    approved.mkdir()
    connections = tmp_path / "connections.json"
    sources = tmp_path / "sources.json"
    connections.write_text("{}")
    sources.write_text("{}")
    environment = {
        **os.environ,
        **{
            key: "compose-test-only"
            for key in (
                "ML_DB_PASSWORD",
                "MLFLOW_DB_PASSWORD",
                "ML_MQ_PASSWORD",
                "ML_MONGO_PASSWORD",
                "ML_S3_PASSWORD",
                "APP_JWT_SECRET",
            )
        },
        "ML_INGESTION_INPUT_DIR": str(approved),
        "ML_DATABRICKS_CONNECTIONS_CONFIG": str(connections),
        "ML_DATABRICKS_SOURCES_CONFIG": str(sources),
        "ML_DATABRICKS_CLIENT_SECRET_FILE": str(secret),
    }
    command = [
        *compose_command(),
        "--env-file",
        "/dev/null",
        "--profile",
        "ingestion",
        "-f",
        "compose.ml.yaml",
        "-f",
        "compose.ml.ingestion.yaml",
    ]
    if databricks:
        command.extend(["-f", "compose.ml.databricks.yaml"])
    result = subprocess.run(
        [*command, "config", "--format=json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert secret_value not in result.stdout
    model = json.loads(result.stdout)
    worker, management = model["services"]["ingestion-worker"], model["services"]["management"]
    assert worker["build"]["target"] == "ingestion"
    assert worker["command"] == ["python", "-m", "bootstrap.ingestion_cli", "work"]
    assert (
        worker["environment"]["ML_RAW_SNAPSHOT_ROOT"]
        == management["environment"]["ML_RAW_SNAPSHOT_ROOT"]
    )
    assert worker["environment"]["ML_DATABASE_URL"] == management["environment"]["ML_DATABASE_URL"]
    for service in (worker, management):
        assert any(volume["target"] == "/app/var" for volume in service["volumes"])
    file_input = next(
        volume for volume in worker["volumes"] if volume["target"] == "/app/ingestion-input"
    )
    assert file_input["read_only"] is True
    assert not any(volume["target"] == "/app/ingestion-input" for volume in management["volumes"])
    assert worker["cap_drop"] == ["ALL"]
    assert worker["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "bootstrap.ingestion_cli",
        "health",
    ]
    assert worker["restart"] == "unless-stopped"
    if databricks:
        assert worker["environment"]["ML_DATABRICKS_ENABLED"] == "true"
        assert management["environment"]["ML_DATABRICKS_ENABLED"] == "true"
        assert not management.get("secrets")
        assert worker["secrets"] == [
            {"source": "databricks_client_secret", "target": "databricks_client_secret"}
        ]
        assert "DATABRICKS_CLIENT_SECRET" not in worker["environment"]
        for service in (worker, management):
            configs = [
                volume
                for volume in service["volumes"]
                if volume["target"].startswith("/app/config/")
            ]
            assert len(configs) == 2 and all(volume["read_only"] for volume in configs)
    else:
        assert worker["environment"]["ML_DATABRICKS_ENABLED"] == "false"
        assert not worker.get("secrets")
