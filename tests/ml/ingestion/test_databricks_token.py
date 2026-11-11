"""Token configuration, connector authentication and Compose isolation contracts."""

import json
import os
import subprocess
from dataclasses import replace
from unittest.mock import Mock

import pytest

from src.domain.ingestion.errors import SourceRejected
from src.domain.ingestion.models import ExtractionLimits
from src.infrastructure.ingestion.adapters.databricks_client import _official_connect
from src.infrastructure.ingestion.adapters.databricks_contract import (
    Column,
    Template,
    parse_connections,
)
from tests.ml.ingestion.test_ingestion_deployment import ROOT, compose_command


def configuration(**authentication):
    return {
        "warehouse": {
            "server_hostname": "approved.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abcdef",
            **authentication,
        }
    }


def token_connection():
    return parse_connections(configuration(access_token_env="DATABRICKS_TOKEN"))["warehouse"]


def test_token_reaches_official_connector_without_oauth_or_persisted_secret(monkeypatch):
    from databricks import sql

    connection = token_connection()  # Management does not need the credential.
    marker = "example-only-token"
    monkeypatch.setenv("DATABRICKS_TOKEN", marker)
    connect = Mock()
    monkeypatch.setattr(sql, "connect", connect)
    _official_connect(connection, ExtractionLimits(timeout_seconds=2))
    options = connect.call_args.kwargs
    assert options["access_token"] == marker
    assert "credentials_provider" not in options and "client_secret" not in options
    assert options["server_hostname"] == connection.server_hostname
    assert options["http_path"] == connection.http_path
    assert options["use_cloud_fetch"] is options["use_inline_params"] is False
    assert options["enable_telemetry"] is options["force_enable_telemetry"] is False
    assert options["_socket_timeout"] == 2
    assert options["_retry_stop_after_attempts_count"] == 1
    assert marker not in repr(connection)
    template = Template("warehouse", ("main", "approved", "events"), 42, (Column("id", "integer"),))
    digest = template.digest(connection)
    monkeypatch.setenv("DATABRICKS_TOKEN", "rotated-example-token")
    assert template.digest(connection) == digest
    assert template.digest(replace(connection, access_token_env="ROTATED_TOKEN")) == digest


@pytest.mark.parametrize(
    "value", [None, "", " ", "token\n", "token\r", "token\x00", "非ASCII", "x" * 4097]
)
def test_invalid_tokens_fail_before_connect_without_exposing_value(monkeypatch, value):
    from databricks import sql

    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    if value is not None and "\x00" not in value:
        monkeypatch.setenv("DATABRICKS_TOKEN", value)
    # os.environ rejects NUL itself; patch lookup to exercise boundary validation.
    if value is not None and "\x00" in value:
        monkeypatch.setattr(os.environ, "get", lambda *args: value)
    connect = Mock()
    monkeypatch.setattr(sql, "connect", connect)
    with pytest.raises(SourceRejected, match="Databricks access token is unavailable or invalid"):
        _official_connect(token_connection(), ExtractionLimits())
    connect.assert_not_called()


@pytest.mark.parametrize(
    "auth",
    [
        {"access_token_env": None},
        {"access_token_env": ""},
        {"access_token_env": "bad-name"},
        {"access_token_env": "lowercase"},
        {"access_token_env": 42},
        {"access_token_env": "DATABRICKS_TOKEN", "client_id": "principal"},
        {"access_token_env": "DATABRICKS_TOKEN", "client_secret_file": "/secret"},
        {"access_token": "inline-token"},
        {},
    ],
)
def test_ambiguous_or_invalid_authentication_is_rejected(auth):
    with pytest.raises(SourceRejected):
        parse_connections(configuration(**auth))


def test_token_compose_reads_env_file_and_exposes_token_only_to_worker(tmp_path):
    marker = "example-only-compose-token"
    env_file = tmp_path / "ingestion.env"
    env_file.write_text(
        f"DATABRICKS_TOKEN={marker}\n"
        "ML_DATABRICKS_CONNECTIONS_CONFIG=/tmp/connections.json\n"
        "ML_DATABRICKS_SOURCES_CONFIG=/tmp/sources.json\n"
        "ML_INGESTION_INPUT_DIR=/tmp/approved-input\n"
        + "".join(
            f"{key}=compose-test-only\n"
            for key in (
                "ML_DB_PASSWORD",
                "MLFLOW_DB_PASSWORD",
                "ML_MQ_PASSWORD",
                "ML_MONGO_PASSWORD",
                "ML_S3_PASSWORD",
                "APP_JWT_SECRET",
            )
        )
    )
    result = subprocess.run(
        [
            *compose_command(),
            "--env-file",
            str(env_file),
            "--profile",
            "ingestion",
            "-f",
            "compose.ml.yaml",
            "-f",
            "compose.ml.ingestion.yaml",
            "-f",
            "compose.ml.databricks-token.yaml",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("ML_", "DATABRICKS_", "APP_JWT_"))
        },
        timeout=30,
    )
    services = json.loads(result.stdout)["services"]
    assert services["ingestion-worker"]["environment"]["DATABRICKS_TOKEN"] == marker
    assert marker not in json.dumps(services["management"])
    assert not services["ingestion-worker"].get("secrets")
