"""Render the standalone hybrid profile with the real Compose CLI."""

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
