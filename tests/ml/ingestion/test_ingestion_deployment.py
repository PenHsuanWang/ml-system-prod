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
    # MLflow 3.6 artifact repositories consult the SDK environment even when
    # the application's client was constructed with an explicit tracking URI.
    for role in ("migrate", "management", "relay", "supervisor", "control", "gateway"):
        tracking = model["services"][role]["environment"]
        assert tracking["MLFLOW_TRACKING_URI"] == tracking["ML_TRACKING_URI"], role
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


@pytest.fixture
def external_production_environment(tmp_path):
    values = {}
    for line in (ROOT / ".env.ml.production.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    paths = {
        "ML_RUNTIME_ROOT": tmp_path / "runtime",
        "ML_DEPLOYMENT_CONFIG_DIR": tmp_path / "private-roles",
        "ML_OPERATOR_DEFINITIONS_DIR": tmp_path / "approved-definitions",
    }
    for key, directory in paths.items():
        directory.mkdir()
        values[key] = str(directory)
    marker = "not-a-real-secret-must-not-appear-in-compose"
    for role in (
        "migrate",
        "management",
        "relay",
        "supervisor",
        "control",
        "gateway",
        "ingestion-worker",
        "operator",
    ):
        source = paths["ML_DEPLOYMENT_CONFIG_DIR"] / (role + ".json")
        source.write_text(json.dumps({"APP_JWT_SECRET": marker}))
        source.chmod(0o600)
    for key, filename, body in (
        ("ML_MLFLOW_CA_FILE", "ca.pem", "test-only-ca"),
        ("ML_DATABRICKS_CONNECTIONS_CONFIG", "connections.json", "{}"),
        ("ML_DATABRICKS_SOURCES_CONFIG", "sources.json", "{}"),
        ("ML_DATABRICKS_CLIENT_SECRET_FILE", "oauth-secret", marker),
    ):
        path = tmp_path / filename
        path.write_text(body)
        values[key] = str(path)
    values.update(
        ML_RELEASE_IMAGE="registry.example.internal/ml-system@sha256:" + "a" * 64,
        ML_IMAGE_DIGEST="sha256:" + "a" * 64,
        ML_CODE_REVISION="b" * 40,
    )
    return {**os.environ, **values}, marker


def external_production_model(environment, *, operator=False):
    command = [*compose_command(), "--env-file", "/dev/null"]
    if operator:
        command.extend(["--profile", "ops"])
    return subprocess.run(
        [*command, "-f", "compose.ml.external-mlflow.yaml", "config", "--format=json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("operator", [False, True])
def test_external_production_is_standalone_pinned_and_starts_all_runtime_roles(
    external_production_environment, operator
):
    environment, marker = external_production_environment
    parsed = external_production_model(environment, operator=operator)
    assert parsed.returncode == 0, parsed.stderr
    assert marker not in parsed.stdout
    model = json.loads(parsed.stdout)
    required = {
        "migrate",
        "management",
        "relay",
        "supervisor",
        "control",
        "gateway",
        "ingestion-worker",
    }
    assert set(model["services"]) == required | ({"operator"} if operator else set())
    assert (
        not {"mlflow", "mlflow-db", "minio", "minio-init", "postgres", "mongo", "rabbitmq", "redis"}
        & model["services"].keys()
    )
    for role, service in model["services"].items():
        assert "build" not in service
        assert service["image"] == environment["ML_RELEASE_IMAGE"]
        assert service["entrypoint"] == ["python", "-m", "bootstrap.deployment_entrypoint"]
        public = service["environment"]
        assert public["ML_RUNTIME_ROLE"] == role
        assert public["ML_RUNTIME_CONFIG_FILE"] == "/run/secrets/runtime.json"
        assert public["ML_MODEL_ENVIRONMENT"] == "prod"
        assert public["ML_TRACKER_BACKEND"] == "mlflow"
        assert public["ML_IMAGE_DIGEST"] == environment["ML_IMAGE_DIGEST"]
        assert public["ML_CODE_REVISION"] == environment["ML_CODE_REVISION"]
        assert public["ML_TRACKING_URI"] == public["MLFLOW_TRACKING_URI"]
        assert public["ML_TRACKING_URI"] == environment["ML_EXTERNAL_MLFLOW_URI"]
        assert public["MLFLOW_TRACKING_SERVER_CERT_PATH"] == "/run/config/mlflow-ca.pem"
        assert public["MLFLOW_TRACKING_INSECURE_TLS"] == "false"
        assert (
            not {
                "ML_DATABASE_URL",
                "APP_MONGO_URI",
                "APP_JWT_SECRET",
                "MLFLOW_TRACKING_PASSWORD",
                "MLFLOW_TRACKING_TOKEN",
            }
            & public.keys()
        )
        assert service["user"] == "10001:10001"
        assert float(service["cpus"]) > 0 and int(service["mem_limit"]) > 0
        assert service["pids_limit"] > 0
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert set(service.get("depends_on", {})) <= required
    assert model["services"]["gateway"]["network_mode"] == "service:control"
    assert not model["services"]["ingestion-worker"].get("profiles")
    assert model["services"]["migrate"]["restart"] == "no"
    if operator:
        assert model["services"]["operator"]["profiles"] == ["ops"]


def test_external_production_mounts_role_secrets_and_operator_inputs_without_oauth_leak(
    external_production_environment,
):
    environment, _ = external_production_environment
    parsed = external_production_model(environment, operator=True)
    assert parsed.returncode == 0, parsed.stderr
    model = json.loads(parsed.stdout)
    for role, service in model["services"].items():
        role_secret = next(item for item in service["secrets"] if item["target"] == "runtime.json")
        secret_file = model["secrets"][role_secret["source"]]["file"]
        assert Path(secret_file) == Path(environment["ML_DEPLOYMENT_CONFIG_DIR"]) / (role + ".json")
        assert service["configs"] == [
            {"source": "mlflow_ca", "target": "/run/config/mlflow-ca.pem"}
        ]
        raw = next(volume for volume in service["volumes"] if volume["target"] == "/app/var")
        assert raw["source"] == environment["ML_RUNTIME_ROOT"]
        assert raw["bind"].get("create_host_path", False) is False
        assert not raw.get("read_only", False)
        has_oauth = any(item["source"] == "databricks_client_secret" for item in service["secrets"])
        assert has_oauth is (role == "ingestion-worker")
    for role in ("management", "ingestion-worker", "operator"):
        service = model["services"][role]
        approved = [v for v in service["volumes"] if v["target"].startswith("/app/config/")]
        assert len(approved) == 2 and all(v["read_only"] for v in approved)
        assert service["environment"]["ML_DATABRICKS_ENABLED"] == "true"
    operator = model["services"]["operator"]
    definitions = next(
        v for v in operator["volumes"] if v["target"] == "/run/ml/operator-definitions"
    )
    assert definitions["read_only"] is True
    assert definitions["source"] == environment["ML_OPERATOR_DEFINITIONS_DIR"]
    assert not any(
        v["target"] == "/run/ml/operator-definitions"
        for role, service in model["services"].items()
        if role != "operator"
        for v in service["volumes"]
    )
    assert model["services"]["ingestion-worker"]["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "bootstrap.deployment_entrypoint",
        "python",
        "-m",
        "bootstrap.ingestion_cli",
        "health",
    ]


def test_external_compose_capture_matches_offline_preflight_contract(
    external_production_environment,
):
    from bootstrap.deployment_cli import preflight

    environment, _ = external_production_environment
    parsed = external_production_model(environment, operator=True)
    assert parsed.returncode == 0, parsed.stderr
    # This metadata is a unit fixture, not an image provenance assertion.
    inspection = {
        "Os": "linux",
        "Architecture": "amd64",
        "RepoDigests": [environment["ML_RELEASE_IMAGE"]],
        "Config": {
            "Labels": {"org.opencontainers.image.revision": environment["ML_CODE_REVISION"]}
        },
    }
    report = preflight(json.loads(parsed.stdout), inspection)
    assert report["status"] == "CONFIGURATION_PREFLIGHT_PASSED"
    assert report["connectivity_qualified"] is False
    assert report["production_accepted"] is False


@pytest.mark.parametrize(
    "missing", ["ML_RELEASE_IMAGE", "ML_CODE_REVISION", "ML_EXTERNAL_MLFLOW_URI"]
)
def test_external_production_rejects_missing_public_release_inputs(
    external_production_environment, missing
):
    environment, _ = external_production_environment
    parsed = external_production_model({**environment, missing: ""})
    assert parsed.returncode != 0
    assert missing in parsed.stderr


def test_external_production_token_is_available_only_to_ingestion_worker(
    external_production_environment,
):
    environment, _ = external_production_environment
    marker = "example-only-production-token"
    environment = {
        **environment,
        "DATABRICKS_TOKEN": marker,
        "ML_DATABRICKS_CLIENT_SECRET_FILE": "/dev/null",
    }
    parsed = external_production_model(environment, operator=True)
    assert parsed.returncode == 0, parsed.stderr
    services = json.loads(parsed.stdout)["services"]
    assert services["ingestion-worker"]["environment"]["DATABRICKS_TOKEN"] == marker
    for role, service in services.items():
        if role != "ingestion-worker":
            assert marker not in json.dumps(service)
