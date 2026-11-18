"""Acceptance isolation and fail-closed evidence; no cluster or ML dependencies."""

import copy
import json

import pytest
import yaml

from scripts.k8s_acceptance_contract import CHECKS, validate
from scripts.k8s_foundation import IMAGES, ROOT, prepare
from scripts.verify_k8s_acceptance import create_operator


def complete_report():
    source, image = "b" * 40, "sha256:" + "a" * 64
    return {
        "status": "K8S_ENGINEERING_ACCEPTANCE_PASSED",
        "namespace": "ml-k8s-acceptance",
        "production_accepted": False,
        "checks": list(CHECKS),
        "source_revision": source,
        "runtime_image_digest": image,
        "training_faults": {
            "status": "POD_TRAINING_RUNTIME_PASSED",
            "checks": list(range(7)),
            "source_revision": source,
            "runtime_image_digest": image,
            "transport": {"ready": 0, "unacked": 0},
        },
        "jobs": [
            {
                "job_id": str(i),
                "state": "SUCCEEDED",
                "attempt_count": 1,
                "stop_confirmed": True,
                "code_revision": source,
                "image_digest": image,
            }
            for i in range(4)
        ],
    }


def test_complete_evidence_remains_engineering_only():
    validate(complete_report())


@pytest.mark.parametrize(
    "fault",
    [
        "partial",
        "missing_case",
        "source",
        "image",
        "transport",
        "refit",
        "no_stop",
        "production",
        "duplicate_job",
    ],
)
def test_partial_mixed_or_unconfirmed_evidence_cannot_pass(fault):
    report = complete_report()
    if fault == "partial":
        report["training_faults"]["status"] = "POD_TRAINING_RECOVERY_PASSED"
    elif fault == "missing_case":
        report["checks"].pop()
    elif fault in {"source", "image"}:
        report["training_faults"][
            "source_revision" if fault == "source" else "runtime_image_digest"
        ] = "wrong"
    elif fault == "transport":
        report["training_faults"]["transport"]["unacked"] = 1
    elif fault == "refit":
        report["jobs"][0]["attempt_count"] = 2
    elif fault == "no_stop":
        report["jobs"][0]["stop_confirmed"] = False
    elif fault == "production":
        report["production_accepted"] = True
    else:
        report["jobs"][1]["job_id"] = report["jobs"][0]["job_id"]
    with pytest.raises(ValueError):
        validate(report)


def test_acceptance_profile_is_explicit_and_does_not_change_the_production_base(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("scripts.k8s_foundation.ROOT", tmp_path)
    images = {name: "example.test/ml@sha256:" + "a" * 64 for name in IMAGES}
    for namespace, enabled in (("ml-k8s-acceptance", False), ("ml-k8s-foundation", True)):
        with pytest.raises(ValueError, match="dedicated namespace"):
            prepare(
                tmp_path / "var/invalid", "b" * 40, images, namespace=namespace, acceptance=enabled
            )
    directory = tmp_path / "var/acceptance"
    prepare(directory, "b" * 40, images, namespace="ml-k8s-acceptance", acceptance=True)
    config = yaml.safe_load((directory / "kustomization.yaml").read_text())
    assert "minikube-acceptance" in config["resources"][0]
    literals = config["configMapGenerator"][1]["literals"]
    assert "ML_TRAINING_NAMESPACE=ml-k8s-acceptance" in literals
    assert "ML_SERVING_NAMESPACE=ml-k8s-acceptance" in literals
    with pytest.raises(FileExistsError):
        prepare(directory, "b" * 40, images, namespace="ml-k8s-acceptance", acceptance=True)
    base = yaml.safe_load((ROOT / "deploy/k8s/base/configmap.yaml").read_text())["data"]
    assert base["ML_MODEL_ENVIRONMENT"] == "prod"
    assert "ML_EBM_QUALITY_POLICY_JSON" not in base


def test_operator_is_bounded_token_free_and_does_not_mutate_runtime_declaration():
    documents = yaml.safe_load_all((ROOT / "deploy/k8s/base/workloads.yaml").read_text())
    native = next(d for d in documents if d["metadata"]["name"] == "ml-control")
    original = copy.deepcopy(native)
    spec = create_operator(native, "example.test/tools@sha256:" + "a" * 64)["spec"]
    assert native == original
    assert spec["automountServiceAccountToken"] is False
    assert spec["restartPolicy"] == "Never" and spec["activeDeadlineSeconds"] == 7200
    assert all("projected" not in volume for volume in spec["volumes"])
    assert "kubernetes-api" not in json.dumps(spec)
    assert spec["containers"][0]["image"] == "example.test/tools@sha256:" + "a" * 64
    with pytest.raises(ValueError):
        create_operator(native, "tools:latest")
