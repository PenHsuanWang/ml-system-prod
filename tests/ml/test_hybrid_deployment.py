"""Render the standalone hybrid profile with the real Compose CLI."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from bootstrap import deployment_cli as cli
from tests.ml.test_deployment_qualification import model_and_inspection


@pytest.fixture
def hybrid_model():
    model, inspection = model_and_inspection()
    for name, image in cli.HYBRID_INFRASTRUCTURE.items():
        model["services"][name] = {"image": image, "healthcheck": {"test": ["CMD", "true"]}}
    for role in cli.REQUIRED_ROLES - {"migrate", "gateway"}:
        model["services"][role]["depends_on"] = {
            "migrate": {"condition": "service_completed_successfully"}
        }
    model["services"]["migrate"]["depends_on"] = {"postgres": {"condition": "service_healthy"}}
    model["services"]["gateway"]["network_mode"] = "service:control"
    return model, inspection


def test_hybrid_preflight_is_explicit_and_external_default_remains_strict(hybrid_model):
    model, inspection = hybrid_model
    assert cli.preflight(model, inspection, hybrid=True)["profile"] == "hybrid"
    assert cli.preflight(model, inspection, hybrid=True)["production_accepted"] is False
    with pytest.raises(cli.QualificationError, match="UNAPPROVED_SERVICES"):
        cli.preflight(model, inspection)


@pytest.mark.parametrize(
    "defect,code",
    [
        ("tracking", "INTERNAL_TRACKING"),
        ("publish", "PRIVATE_HYBRID"),
        ("image", "PRIVATE_HYBRID"),
        ("migration", "HYBRID_MIGRATION"),
        ("namespace", "SHARED_CONTROL"),
        ("order", "HYBRID_MIGRATION_ORDER"),
    ],
)
def test_hybrid_preflight_negative_contracts(hybrid_model, defect, code):
    model, inspection = hybrid_model
    if defect == "tracking":
        model["services"]["minio"] = {"image": "minio"}
    elif defect == "publish":
        model["services"]["postgres"]["ports"] = ["5432:5432"]
    elif defect == "image":
        model["services"]["postgres"]["image"] = "arbitrary"
    elif defect == "migration":
        model["services"]["migrate"]["depends_on"] = {}
    elif defect == "namespace":
        model["services"]["gateway"]["network_mode"] = "host"
    else:
        model["services"]["supervisor"]["depends_on"] = {}
    with pytest.raises(cli.QualificationError, match=code):
        cli.preflight(model, inspection, hybrid=True)


def render_hybrid(tmp_path, *, ingestion=False):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    for line in (root / ".env.ml.hybrid.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    infra = tmp_path / "infra"
    infra.mkdir()
    for name in ("postgres", "mongo", "rabbitmq"):
        (infra / (name + ".env")).write_text("SYNTHETIC_FIXTURE=not-a-secret\n")
    (infra / "redis.conf").write_text("appendonly yes\n")
    env.update(
        ML_HYBRID_INFRA_CONFIG_DIR=str(infra),
        ML_RELEASE_IMAGE="registry.example/ml@sha256:" + "a" * 64,
        ML_IMAGE_DIGEST="sha256:" + "a" * 64,
        ML_CODE_REVISION="b" * 40,
        ML_INGESTION_ENABLED="true" if ingestion else "false",
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        "compose.ml.hybrid-mlflow.yaml",
    ]
    if ingestion:
        command += ["--profile", "ingestion"]
    command += ["config", "--format", "json"]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("ingestion", [False, True])
def test_actual_compose_model_has_only_hybrid_dependencies(tmp_path, ingestion):
    model = render_hybrid(tmp_path, ingestion=ingestion)
    services = model["services"]
    assert not cli.INTERNAL_TRACKING.intersection(services)
    assert set(cli.HYBRID_INFRASTRUCTURE) <= set(services)
    assert ("ingestion-worker" in services) == ingestion
    assert services["management"]["environment"]["ML_INGESTION_ENABLED"] == str(ingestion).lower()
    assert services["gateway"]["network_mode"] == "service:control"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name, service in services.items():
        if name in cli.REQUIRED_ROLES:
            env = service["environment"]
            assert env["ML_TRACKING_URI"] == env["MLFLOW_TRACKING_URI"]
            assert env["ML_TRACKING_URI"].startswith("https://")
            assert env["MLFLOW_TRACKING_INSECURE_TLS"] == "false"
            assert not set(cli.SECRET_ENV).intersection(env) - {"DATABRICKS_TOKEN"}
            assert service["entrypoint"] == ["python", "-m", "bootstrap.deployment_entrypoint"]
            assert "runtime_" + name.replace("-", "_") in {s["source"] for s in service["secrets"]}
            if name not in {"gateway", "migrate"}:
                assert (
                    service["depends_on"]["migrate"]["condition"]
                    == "service_completed_successfully"
                )
        else:
            assert not service.get("ports")
