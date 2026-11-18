"""Run the governed lifecycle and real faults only in disposable Minikube acceptance."""

import argparse
import base64
import copy
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from scripts.k8s_acceptance_contract import validate

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "ml-k8s-acceptance"
KUBECTL = ["kubectl", "--context=minikube", "--namespace=" + NAMESPACE]


def require(condition, code):
    if not condition:
        raise ValueError(code)


def run(*arguments, **kwargs):
    return subprocess.run(
        [*KUBECTL, *arguments], check=True, text=True, capture_output=True, timeout=300, **kwargs
    ).stdout


def wait_for(check, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(1)
    raise TimeoutError("ACCEPTANCE_CONDITION_DEADLINE")


def create_operator(native, image):
    require(
        re.fullmatch(r"[A-Za-z0-9._/:-]+@sha256:[a-f0-9]{64}", image)
        and not image.endswith("0" * 64),
        "PINNED_TOOLS_IMAGE_REQUIRED",
    )
    pod = copy.deepcopy(native["spec"]["template"]["spec"])
    pod.update(
        serviceAccountName="ml-serving",
        automountServiceAccountToken=False,
        restartPolicy="Never",
        activeDeadlineSeconds=7200,
    )
    pod["initContainers"] = [c for c in pod["initContainers"] if c["name"] == "prepare-control"]
    container = next(c for c in pod["containers"] if c["name"] == "control")
    container.update(
        name="operator", image=image, args=["python", "-c", "import time; time.sleep(7200)"]
    )
    container["volumeMounts"] = [
        m for m in container["volumeMounts"] if m["name"] != "kubernetes-api"
    ]
    pod["containers"] = [container]
    mounts = {
        m["name"] for c in pod["initContainers"] + pod["containers"] for m in c["volumeMounts"]
    }
    pod["volumes"] = [v for v in pod["volumes"] if v["name"] in mounts]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "governed-acceptance-operator",
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": "ml-system"},
        },
        "spec": pod,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    require(
        ROOT / "var" in output.parents
        and not any(p.is_symlink() for p in (output, *output.parents)),
        "NEW_PRIVATE_OUTPUT_REQUIRED",
    )
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = json.loads(run("get", "configmap/ml-runtime", "-o", "json"))["data"]
    require(
        settings.get("ML_TRAINING_NAMESPACE") == NAMESPACE
        and settings.get("ML_SERVING_NAMESPACE") == NAMESPACE
        and settings.get("ML_TRAINING_BACKEND") == "pod"
        and settings.get("ML_SERVING_BACKEND") == "kubernetes",
        "NATIVE_PROFILES_REQUIRED",
    )
    report = {
        "status": "INCOMPLETE",
        "namespace": NAMESPACE,
        "source_revision": settings["ML_CODE_REVISION"],
        "runtime_image_digest": settings["ML_IMAGE_DIGEST"],
        "tools_image": args.tools_image,
        "qualification_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "production_accepted": False,
        "cross_node_qualified": False,
        "checks": [],
        "jobs": [],
        "deployments": [],
    }

    def record(name):
        report["checks"].append(name)
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print("PASS " + name, flush=True)

    def operator(*arguments):
        result = run(
            "exec",
            "pod/governed-acceptance-operator",
            "-c",
            "operator",
            "--",
            "python",
            "-m",
            "bootstrap.deployment_entrypoint",
            "python",
            "scripts/k8s_acceptance_fixture.py",
            *arguments,
        )
        return json.loads(result.strip().splitlines()[-1])

    def request(method, path, body=None, *, expected=200, account="reviewer"):
        value = operator("request-as", account, method, path, json.dumps(body))
        require(
            value["status"] == expected,
            "HTTP_STATUS_" + str(value["status"]) + "_EXPECTED_" + str(expected),
        )
        return value["body"]

    def job(kind, seed=42):
        key = uuid4().hex
        value = operator("submit", kind, key, str(seed))
        require(value["status"] == 202, "TRAINING_ADMISSION_FAILED")
        again = operator("submit", kind, key, str(seed))
        require(
            again["status"] == 202 and again["body"]["job_id"] == value["body"]["job_id"],
            "IDEMPOTENCY_CREATED_DUPLICATE",
        )
        final = wait_for(
            lambda: (
                (v if v["state"] == "SUCCEEDED" else None)
                if (v := operator("status", value["body"]["job_id"]))
                else None
            )
        )
        require(
            final["attempt_count"] == 1 and final["stop_confirmed"], "ONE_FIT_AND_STOP_REQUIRED"
        )
        require(
            final["code_revision"] == report["source_revision"]
            and final["image_digest"] == report["runtime_image_digest"],
            "JOB_SOURCE_IMAGE_MISMATCH",
        )
        request("GET", "/api/v1/training/jobs/" + final["job_id"])
        report["jobs"].append(final)
        return final

    def evaluation(job):
        values = request(
            "GET", "/api/v1/model_registry/bindings/" + job["binding_id"] + "/evaluations"
        )
        require(len(values) == 1, "EXACT_EVALUATION_REQUIRED")
        return values[0]

    def approval(job):
        value = evaluation(job)
        require(value["decision"] == "APPROVED_BY_POLICY", "ENGINEERING_EVALUATION_REJECTED")
        approved = request(
            "POST",
            "/api/v1/model_registry/bindings/" + job["binding_id"] + "/approvals",
            {
                "evaluation_id": value["evaluation_id"],
                "environment": settings["ML_MODEL_ENVIRONMENT"],
            },
            expected=201,
        )
        return {
            "binding_id": job["binding_id"],
            "approval_id": approved["approval_id"],
            "evaluation": value,
        }

    def create(model, endpoint="quality"):
        value = request(
            "POST",
            "/api/v1/deployments",
            {
                "endpoint_id": endpoint,
                "model_binding_id": model["binding_id"],
                "approval_id": model["approval_id"],
            },
            expected=202,
        )
        report["deployments"].append(value)
        return value

    def status(deployment):
        return operator("route", deployment["deployment_id"])

    def active(deployment):
        return wait_for(lambda: v if (v := status(deployment))["state"] == "ACTIVE" else None)

    def predict(deployment, kind="mlp", endpoint="quality"):
        value = wait_for(
            lambda: v if (v := operator("predict", endpoint, kind))["status"] == 200 else None
        )
        provenance = value["body"]["provenance"]
        require(
            provenance["deployment_id"] == deployment["deployment_id"]
            and provenance["generation"] == deployment["generation"],
            "PREDICTION_IDENTITY_MISMATCH",
        )
        return provenance

    def retired(deployment):
        return wait_for(
            lambda: v if (v := status(deployment))["state"] == "RETIRED" and v["released"] else None
        )

    def native_name(deployment):
        return "ml-serve-" + deployment["deployment_id"].replace("-", "")

    def restart(role):
        pods = json.loads(
            run("get", "pods", "-l", "app.kubernetes.io/name=ml-" + role, "-o", "json")
        )["items"]
        run("scale", "deployment/ml-" + role, "--replicas=0")
        for pod in pods:
            subprocess.run(
                [
                    *KUBECTL,
                    "exec",
                    "pod/" + pod["metadata"]["name"],
                    "-c",
                    role,
                    "--",
                    "python",
                    "-c",
                    "import os,signal; os.kill(1,signal.SIGINT)",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        run(
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/name=ml-" + role,
            "--timeout=180s",
        )
        run("scale", "deployment/ml-" + role, "--replicas=1")
        run("rollout", "status", "deployment/ml-" + role, "--timeout=180s")

    try:
        native = json.loads(run("get", "deployment/ml-control", "-o", "json"))
        run("create", "-f", "-", input=json.dumps(create_operator(native, args.tools_image)))
        run("wait", "--for=condition=Ready", "pod/governed-acceptance-operator", "--timeout=240s")
        report["fixture"] = operator("init")
        record("synthetic_raw_snapshots_and_verified_dataset_builder")
        first_job = job("mlp")
        record("http_idempotency_outbox_real_pod_fit_evaluation")
        binding_path = "/api/v1/model_registry/bindings/" + first_job["binding_id"]
        request(
            "GET", "/api/v1/training/jobs/" + first_job["job_id"], expected=404, account="foreign"
        )
        request("GET", binding_path, expected=404, account="foreign")
        request(
            "POST",
            binding_path + "/approvals",
            {
                "evaluation_id": evaluation(first_job)["evaluation_id"],
                "environment": settings["ML_MODEL_ENVIRONMENT"],
            },
            expected=403,
            account="viewer",
        )
        request(
            "POST",
            "/api/v1/deployments",
            {
                "endpoint_id": "quality",
                "model_binding_id": first_job["binding_id"],
                "approval_id": "missing",
            },
            expected=404,
        )
        record("tenant_permission_and_explicit_approval_fences")
        model = approval(first_job)
        first = create(model)
        before = active(first)
        initial = predict(first)
        record("explicit_http_approval_native_activation_gateway_prediction")

        pods = json.loads(
            run("get", "pods", "-l", "ml-system/serving-id=" + native_name(first), "-o", "json")
        )["items"]
        run("delete", "pod/" + pods[0]["metadata"]["name"], "--wait=true")
        require(
            predict(first)["pod_uid"] != initial["pod_uid"] and status(first) == before,
            "POD_RECOVERY_CHANGED_BUSINESS_IDENTITY",
        )
        run("delete", "deployment/" + native_name(first), "--cascade=foreground", "--wait=true")
        reconstructed = predict(first)
        require(status(first) == before, "ACTIVE_RECONSTRUCTION_CHANGED_ROUTE")
        report["active_recovery"] = {"before": initial, "after": reconstructed, "route": before}
        record("serving_pod_and_active_resource_recovery_without_reactivation")
        for role in ("control", "gateway"):
            restart(role)
        predict(first)
        require(status(first) == before, "ROLE_RESTART_CHANGED_ROUTE")
        record("control_gateway_restart_preserves_durable_route")

        rejected = job("rejected")
        rejected_evaluation = evaluation(rejected)
        require(
            rejected_evaluation["decision"] == "REJECTED_BY_POLICY",
            "REAL_QUALITY_REJECTION_REQUIRED",
        )
        request(
            "POST",
            "/api/v1/model_registry/bindings/" + rejected["binding_id"] + "/approvals",
            {
                "evaluation_id": rejected_evaluation["evaluation_id"],
                "environment": settings["ML_MODEL_ENVIRONMENT"],
            },
            expected=409,
        )
        report["rejected_evaluation"] = rejected_evaluation
        record("real_quality_rejection_cannot_be_approved")

        second_job = job("mlp", 43)
        second_model = approval(second_job)
        # New Serving Pods copy their reader configuration at init. Corrupt only
        # the new candidate's private copy, then restore the shared source Secret.
        secret = json.loads(run("get", "secret/ml-runtime-serving", "-o", "json"))
        saved = secret["data"]["runtime.json"]
        private = json.loads(base64.b64decode(saved))
        private["ML_SERVING_MLFLOW_PASSWORD"] = "intentional-disposable-auth-fault"
        corrupt = base64.b64encode(json.dumps(private).encode()).decode()
        try:
            run(
                "patch",
                "secret/ml-runtime-serving",
                "--type=merge",
                "--patch-file=/dev/stdin",
                input=json.dumps({"data": {"runtime.json": corrupt}}),
            )
            failed = create(second_model)
            selector = "ml-system/serving-id=" + native_name(failed)

            def private_copied():
                items = json.loads(run("get", "pods", "-l", selector, "-o", "json"))["items"]
                return any(
                    any(
                        c["name"] == "prepare-serving"
                        and c.get("state", {}).get("terminated", {}).get("exitCode") == 0
                        for c in p["status"].get("initContainerStatuses", [])
                    )
                    for p in items
                )

            wait_for(private_copied)
        finally:
            run(
                "patch",
                "secret/ml-runtime-serving",
                "--type=merge",
                "--patch-file=/dev/stdin",
                input=json.dumps({"data": {"runtime.json": saved}}),
            )
        predict(first)
        wait_for(
            lambda: v if (v := status(failed))["state"] == "FAILED" and v["released"] else None,
            timeout=300,
        )
        after = status(first)
        require(
            after["route"]["deployment_id"] == first["deployment_id"]
            and after["route"]["generation"] == first["generation"]
            and after["approval_id"] == before["approval_id"]
            and after["spec_digest"] == before["spec_digest"],
            "FAILED_CANDIDATE_REPLACED_ACTIVE",
        )
        predict(first)
        report["failed_candidate"] = {
            "deployment": failed,
            "terminal": status(failed),
            "active": after,
        }
        record("candidate_auth_failure_fenced_retired_active_route_preserved")

        second = create(second_model)
        active(second)
        predict(second)
        retired(first)
        rollback = request(
            "POST", "/api/v1/deployments/" + first["deployment_id"] + "/rollback", expected=202
        )
        require(
            rollback["generation"] > second["generation"]
            and rollback["deployment_id"] != first["deployment_id"],
            "ROLLBACK_NOT_NEW_GENERATION",
        )
        report["deployments"].append(rollback)
        active(rollback)
        predict(rollback)
        retired(second)
        record("approved_update_and_rollback_with_confirmed_retirement")

        ebm_job = job("ebm")
        report["ebm_lineage"] = operator("lineage", ebm_job["binding_id"])
        require(
            report["ebm_lineage"]["producer_job_id"] == ebm_job["job_id"],
            "EBM_JOB_LINEAGE_MISMATCH",
        )
        ebm = create(approval(ebm_job), "ebm-quality")
        active(ebm)
        report["ebm_prediction"] = predict(ebm, "ebm", "ebm-quality")
        record("ebm_raw_builder_worker_mlflow_evaluation_approval_native_inference")
        request(
            "POST",
            "/api/v1/deployments",
            {
                "endpoint_id": "capacity-blocked",
                "model_binding_id": model["binding_id"],
                "approval_id": model["approval_id"],
            },
            expected=429,
        )
        predict(rollback)
        predict(ebm, "ebm", "ebm-quality")
        record("two_global_serving_reservations_remain_enforced")

        # Same immutable runtime, all roles active, no direct fit or SQL mutation.
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/verify_k8s_training.py"),
                "--namespace",
                NAMESPACE,
                "--output",
                str(output / "training.json"),
            ],
            check=True,
            timeout=1200,
        )
        training = json.loads((output / "training.json").read_text())
        require(
            training["status"] == "POD_TRAINING_RUNTIME_PASSED"
            and len(training["checks"]) == 7
            and training["source_revision"] == report["source_revision"]
            and training["runtime_image_digest"] == report["runtime_image_digest"],
            "TRAINING_FAULT_GATE_FAILED",
        )
        report["training_faults"] = training
        predict(rollback)
        predict(ebm, "ebm", "ebm-quality")
        record("same_stack_training_faults_do_not_change_active_serving")
        report["namespace_uid"] = json.loads(run("get", "namespace", NAMESPACE, "-o", "json"))[
            "metadata"
        ]["uid"]
        roles = json.loads(run("get", "pods", "-o", "json"))["items"]
        report["runtime_pods"] = [
            {
                "name": p["metadata"]["name"],
                "uid": p["metadata"]["uid"],
                "images": [c["imageID"] for c in p["status"].get("containerStatuses", [])],
            }
            for p in roles
            if p["status"].get("phase") == "Running"
        ]
        report["status"] = "K8S_ENGINEERING_ACCEPTANCE_PASSED"
        validate(report)
    except Exception:
        report["status"] = "INCOMPLETE"
        raise
    finally:
        path = output / "result.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(path, 0o600)
    print(json.dumps({"status": report["status"], "checks": len(report["checks"])}))


if __name__ == "__main__":
    main()
