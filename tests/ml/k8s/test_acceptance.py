"""Acceptance isolation and fail-closed evidence; no cluster or ML dependencies."""

import copy
import json

import pytest
import yaml

from scripts.k8s_acceptance_contract import CHECKS, TRAINING_CHECKS, validate, validate_checkpoint
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
            "checks": list(TRAINING_CHECKS),
            "source_revision": source,
            "runtime_image_digest": image,
            "transport": {
                "ready": 0,
                "unacked": 0,
                "snapshots": [{"job_id": str(i)} for i in range(7)],
            },
            "jobs": [
                {
                    "job_id": str(i),
                    "code_revision": source,
                    "image_digest": image,
                    "attempt_count": 1,
                    "receipt": {"confirmed": True, "pod_uid": "pod"},
                    "execution_id": str(i),
                    "pod_uid": "pod",
                    "owner_changed": i == 4,
                    "state": {2: "CANCELLED", 3: "FAILED", 6: "FAILED"}.get(i, "SUCCEEDED"),
                    "error_code": {3: "TRAINING_DEADLINE", 6: "SUPERVISOR_DRAIN"}.get(i),
                }
                for i in range(7)
            ],
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
    worker_secret = json.loads((directory / "dataset-build-worker.json").read_text())
    assert set(worker_secret) == {"ML_DATABASE_URL", "APP_MONGO_URI"}
    assert worker_secret["APP_MONGO_URI"].startswith("mongodb://dataset-build:")
    assert worker_secret["APP_MONGO_URI"].endswith("?authSource=backend")
    mongo_init = (directory / "dataset-build-identity.js").read_text()
    assert 'collection:"identity_users"},actions:["find"]' in mongo_init
    assert 'roles:[{role:"datasetBuildLookup",db:"backend"}]' in mongo_init
    with pytest.raises(FileExistsError):
        prepare(directory, "b" * 40, images, namespace="ml-k8s-acceptance", acceptance=True)
    base = yaml.safe_load((ROOT / "deploy/k8s/base/configmap.yaml").read_text())["data"]
    assert base["ML_MODEL_ENVIRONMENT"] == "prod"
    assert "ML_EBM_QUALITY_POLICY_JSON" not in base


def test_worker_declaration_exposes_only_approved_shared_subpaths():
    resources = list(
        yaml.safe_load_all((ROOT / "deploy/k8s/dataset-build/worker.yaml").read_text())
    )
    assert [resource["kind"] for resource in resources] == ["ServiceAccount", "Deployment"]
    deployment = resources[1]["spec"]
    assert deployment["replicas"] == 1 and deployment["strategy"]["type"] == "Recreate"
    pod = deployment["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    (worker,) = pod["containers"]
    assert worker["args"] == ["python", "-m", "bootstrap.dataset_build_worker"]
    assert worker["securityContext"]["readOnlyRootFilesystem"] is True
    assert "ports" not in worker
    assert {key for key in worker["resources"]["limits"]} == {"cpu", "memory", "ephemeral-storage"}
    mounts = {mount["subPath"]: mount for mount in worker["volumeMounts"] if "subPath" in mount}
    assert set(mounts) == {"datasets", "executions", "raw-snapshots"}
    assert mounts["raw-snapshots"]["readOnly"] is True
    assert all(not mounts[path].get("readOnly") for path in ("datasets", "executions"))


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


@pytest.mark.parametrize("fault", ["late", "source", "image", "tools", "namespace", "extra_fit"])
def test_resume_rejects_later_or_different_runtime_checkpoints(fault):
    current = complete_report()
    previous = copy.deepcopy(current)
    previous.update(
        status="INCOMPLETE",
        checks=list(CHECKS[:5]),
        jobs=current["jobs"][:1],
        deployments=[{"deployment_id": "first"}],
    )
    validate_checkpoint(previous, current)
    if fault == "late":
        previous["checks"].append(CHECKS[5])
    elif fault == "extra_fit":
        previous["jobs"] = current["jobs"][:2]
    else:
        key = {
            "source": "source_revision",
            "image": "runtime_image_digest",
            "tools": "tools_image",
            "namespace": "namespace",
        }[fault]
        previous[key] = "different"
    with pytest.raises(ValueError):
        validate_checkpoint(previous, current)


@pytest.mark.parametrize(
    "fault", ["duplicate_case", "mixed_source", "missing_stop", "owner", "missing_ack"]
)
def test_nested_training_fault_evidence_is_required_for_every_case(fault):
    report = complete_report()
    training = report["training_faults"]
    if fault == "duplicate_case":
        training["checks"][-1] = training["checks"][0]
    elif fault == "mixed_source":
        training["jobs"][0]["code_revision"] = "wrong"
    elif fault == "missing_stop":
        training["jobs"][0]["receipt"]["confirmed"] = False
    elif fault == "owner":
        training["jobs"][4]["owner_changed"] = False
    else:
        training["transport"]["snapshots"].pop()
    with pytest.raises(ValueError):
        validate(report)
