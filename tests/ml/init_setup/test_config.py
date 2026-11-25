import json
import os

import pytest

from bootstrap.deployment_entrypoint import load_runtime_environment
from bootstrap.init_config import (
    OAUTH_CONTAINER_PATH,
    ROLES,
    build_plan,
    connection_uri,
    credential,
    https_uri,
    json_bytes,
    load_object,
    role_configurations,
)
from bootstrap.init_files import SetupError, env_values
from src.domain.ml import MLError
from src.infrastructure.ingestion.adapters.databricks_contract import (
    parse_connections,
    parse_templates,
)


def test_local_output_matches_original_compose_contract(spec, files):
    plan = build_plan(spec, files)
    assert plan.compose_files == ("compose.ml.yaml",) and plan.env_files == (".env.ml",)
    data = env_values(plan.outputs[spec.project / ".env.ml"].decode())
    assert data["ML_UID"] == str(os.geteuid())
    assert len(data["APP_JWT_SECRET"]) >= 32
    assert len({data[k] for k in data if k.endswith("PASSWORD")}) == 5
    assert data["ML_DB_PASSWORD"] not in repr(plan)


def test_file_only_profile_has_separate_env_and_input_mount(spec, files):
    spec.environment["ML_INGESTION_INPUT_DIR"] = str(spec.private / "input")
    plan = build_plan(spec, files)
    assert plan.compose_files == ("compose.ml.yaml", "compose.ml.ingestion.yaml")
    assert plan.env_files == (".env.ml", ".env.ml.ingestion")
    env = env_values(plan.outputs[spec.project / ".env.ml.ingestion"].decode())
    assert env == {"ML_INGESTION_INPUT_DIR": str(spec.private / "input")}


@pytest.mark.parametrize("mode", ["oauth", "token"])
def test_databricks_outputs_use_runtime_schema_and_overlay(spec, files, mode):
    connection = {
        "server_hostname": "dbc-approved.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/0123abcd",
    }
    if mode == "oauth":
        connection.update(client_id="principal", client_secret_file=OAUTH_CONTAINER_PATH)
        spec.oauth_secret = "oauth-test-only"
    else:
        connection["access_token_env"] = "DATABRICKS_TOKEN"
        spec.environment["DATABRICKS_TOKEN"] = "token$#'literal"
    spec.connections = {"warehouse-main": connection}
    spec.environment["ML_INGESTION_INPUT_DIR"] = str(spec.private / "input")
    plan = build_plan(spec, files)
    parsed = parse_connections(
        json.loads(plan.outputs[spec.private / "databricks/connections.json"])
    )
    assert "warehouse-main" in parsed
    assert parse_templates(json.loads(plan.outputs[spec.private / "databricks/sources.json"])) == {}
    assert plan.pending_sources
    expected = (
        "compose.ml.databricks-token.yaml" if mode == "token" else "compose.ml.databricks.yaml"
    )
    assert plan.compose_files[-1] == expected
    env = env_values(plan.outputs[spec.project / ".env.ml.ingestion"].decode())
    assert env["ML_DATABRICKS_CONNECTIONS_CONFIG"] == str(
        spec.private / "databricks/connections.json"
    )
    assert "token$" not in plan.outputs[spec.private / "databricks/connections.json"].decode()


def test_external_roles_load_with_unmodified_production_entrypoint(external, files):
    plan = build_plan(external, files)
    assert plan.compose_files == ("compose.ml.external-mlflow.yaml",)
    files.write(plan.outputs, dict.fromkeys(plan.outputs, "overwrite"), plan.directories)
    env = env_values((external.project / ".env.ml.production").read_text())
    assert env["ML_DATABRICKS_CLIENT_SECRET_FILE"] == "/dev/null"
    assert "ML_DATABASE_URL" not in env and "APP_JWT_SECRET" not in env
    for role in ROLES:
        config = external.private / "roles" / (role + ".json")
        runtime = {
            "ML_RUNTIME_ROLE": role,
            "ML_RUNTIME_CONFIG_FILE": str(config),
            "ML_MODEL_ENVIRONMENT": "prod",
            "ML_TRACKER_BACKEND": "mlflow",
            "ML_CODE_REVISION": env["ML_CODE_REVISION"],
            "ML_IMAGE_DIGEST": env["ML_IMAGE_DIGEST"],
            "ML_TRACKING_URI": env["ML_EXTERNAL_MLFLOW_URI"],
            "MLFLOW_TRACKING_URI": env["ML_EXTERNAL_MLFLOW_URI"],
            "MLFLOW_TRACKING_INSECURE_TLS": "false",
            "MLFLOW_TRACKING_SERVER_CERT_PATH": env["ML_MLFLOW_CA_FILE"],
        }
        assert load_runtime_environment(runtime)["ML_DEPLOYMENT_ROLE"]
        assert config.stat().st_mode & 0o777 == 0o600
    management = json.loads((external.private / "roles/management.json").read_text())
    assert set(management) == {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"}
    assert (external.private / "operator-definitions").is_dir()


def test_external_plan_rejects_private_keys_in_merged_environment(external, files):
    path = external.project / ".env.ml.production"
    path.write_text("MLFLOW_TRACKING_TOKEN=existing-private-value\n")
    path.chmod(0o600)
    with pytest.raises(SetupError, match="Production credentials belong only in private role JSON"):
        build_plan(external, files)


def test_per_role_existing_credentials_preserved_without_changes(external):
    original = role_configurations(external)
    original["relay"]["ML_DATABASE_URL"] = "postgresql+psycopg://relay:separate@postgres/ml"
    original["supervisor"]["MLFLOW_TRACKING_USERNAME"] = "separate-worker"
    external.existing_roles = original
    assert role_configurations(external) == original
    external.private_values["ML_DATABASE_URL"] = "postgresql+psycopg://new:credentials@postgres/ml"
    external.changed_private = {"ML_DATABASE_URL"}
    updated = role_configurations(external)
    assert all(
        v["ML_DATABASE_URL"] == external.private_values["ML_DATABASE_URL"] for v in updated.values()
    )
    assert updated["supervisor"]["MLFLOW_TRACKING_USERNAME"] == "separate-worker"


def test_auth_switch_removes_prior_mechanism(external):
    external.existing_roles = role_configurations(external)
    external.private_values.pop("MLFLOW_TRACKING_USERNAME")
    external.private_values.pop("MLFLOW_TRACKING_PASSWORD")
    external.private_values["MLFLOW_TRACKING_TOKEN"] = "new-writer"
    external.changed_private = {
        "MLFLOW_TRACKING_TOKEN",
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
    }
    for role, config in role_configurations(external).items():
        if role in {"supervisor", "control", "operator"}:
            assert config["MLFLOW_TRACKING_TOKEN"] == "new-writer"
            assert "MLFLOW_TRACKING_USERNAME" not in config


def test_identical_mlflow_writer_reader_rejected(external, files):
    external.private_values.update(
        ML_SERVING_MLFLOW_USERNAME="writer", ML_SERVING_MLFLOW_PASSWORD="different-password"
    )
    del external.private_values["ML_SERVING_MLFLOW_TOKEN"]
    with pytest.raises(SetupError, match="distinct"):
        build_plan(external, files)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ML_CODE_REVISION", "development"),
        ("ML_RELEASE_IMAGE", "image:latest"),
        ("ML_IMAGE_DIGEST", "sha256:" + "f" * 64),
        ("ML_EXTERNAL_MLFLOW_URI", "http://tracking"),
        ("ML_ACK_TIMEOUT_MS", "1"),
        ("ML_STALE_AFTER_SECONDS", "1"),
        ("ML_MANAGEMENT_PORT", "0"),
        ("ML_PREDICTION_TIMEOUT_SECONDS", "nan"),
        ("ML_CONTROL_CPUS", "nan"),
        ("ML_CONTROL_MEMORY", "0m"),
        ("ML_API_CONCURRENCY", "0"),
        ("ML_UID", "0"),
    ],
)
def test_external_invalid_release_limits_and_identity_rejected(external, files, field, value):
    external.environment[field] = value
    with pytest.raises((SetupError, ValueError)):
        build_plan(external, files)
    assert not (external.project / ".env.ml.production").exists()


def test_private_runtime_overlap_rejected(spec, files):
    spec.private = spec.project / "var/config"
    with pytest.raises(SetupError, match="separate"):
        build_plan(spec, files)


def test_unknown_template_connection_rejected(external, files):
    external.templates = {
        "source": {
            "connection_ref": "missing",
            "table": ["a", "b", "c"],
            "delta_version": 1,
            "projection": [{"name": "id", "type": "integer"}],
        }
    }
    with pytest.raises(SetupError, match="unknown"):
        build_plan(external, files)


def test_raw_token_inside_connections_rejected(external, files):
    external.connections["warehouse-main"]["token"] = "forbidden"
    with pytest.raises(MLError):
        build_plan(external, files)


@pytest.mark.parametrize(
    "uri",
    [
        "http://tracking",
        "https://u:p@tracking",
        "https://tracking?token=x",
        "https://tracking#fragment",
        "https://tracking:0",
    ],
)
def test_https_endpoint_contract(uri):
    with pytest.raises(SetupError):
        https_uri(uri)


@pytest.mark.parametrize("value", ["", "has space", "bad\nline", "x" * 4097])
def test_credential_bounds(value):
    with pytest.raises(SetupError):
        credential(value)


def test_wrong_database_driver_and_duplicate_json_rejected():
    with pytest.raises(SetupError):
        connection_uri("postgresql://db/ml", {"postgresql+psycopg"})
    with pytest.raises(SetupError):
        load_object(b'{"name":1,"name":2}')
    assert b"NaN" not in json_bytes({"ok": 1})


def test_runtime_policy_is_preserved_in_both_profiles(spec, external, files):
    policy = {"contract": "databricks-access-v1", "grants": []}
    for target in (spec, external):
        target.connections = external.connections
        target.environment["DATABRICKS_TOKEN"] = "synthetic-test-token"
        target.environment["ML_INGESTION_INPUT_DIR"] = str(target.private / "input")
        target.source_policy = policy
        plan = build_plan(target, files)
        assert json.loads(plan.outputs[target.private / "databricks/access-policy.json"]) == policy
        env_path = ".env.ml.ingestion" if target.profile == "compose" else ".env.ml.production"
        environment = env_values(plan.outputs[target.project / env_path].decode())
        assert environment["ML_DATABRICKS_POLICY_FILE"] == "/app/config/databricks-policy.json"
        assert environment["ML_DATABRICKS_POLICY_CONFIG"].endswith("/databricks/access-policy.json")
