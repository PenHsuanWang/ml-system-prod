"""Exercise native Serving on the isolated Minikube profile, retaining all evidence."""

import argparse
import copy
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "ml-k8s-serving-runtime"
KUBECTL = ["kubectl", "--context=minikube", "--namespace=" + NAMESPACE]


def run(*arguments, **kwargs):
    return subprocess.run(
        [*KUBECTL, *arguments], check=True, text=True, capture_output=True, timeout=300, **kwargs
    ).stdout


def wait_for(check, *, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError("Serving qualification deadline exceeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-fixtures", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Retain the existing qualification receipt")
    settings = json.loads(run("get", "configmap/ml-runtime", "-o", "json"))["data"]
    if (
        settings.get("ML_SERVING_NAMESPACE") != NAMESPACE
        or settings.get("ML_SERVING_BACKEND") != "kubernetes"
    ):
        raise ValueError("An isolated native Serving profile is required")

    # Training execution migration is the next PR. Prepare real governed fixtures
    # in one explicit operator Pod while the queue consumers/controller are paused.
    def pause_runtime(name):
        selector = "app.kubernetes.io/name=ml-" + name
        pods = json.loads(run("get", "pods", "-l", selector, "-o", "json"))["items"]
        run("scale", "deployment/ml-" + name, "--replicas=0")
        # Only these disposable fixture roles have no training in flight. Python
        # PID 1 may ignore default SIGTERM; request its registered graceful SIGINT
        # handler instead. Still require actual Pod disappearance before proceeding.
        for item in pods:
            for container in item["spec"]["containers"]:
                subprocess.run(
                    [
                        *KUBECTL,
                        "exec",
                        "pod/" + item["metadata"]["name"],
                        "-c",
                        container["name"],
                        "--",
                        "python",
                        "-c",
                        "import os,signal; os.kill(1, signal.SIGINT)",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=20,
                )
        run("delete", "pods", "-l", selector, "--grace-period=15", "--wait=true", "--timeout=120s")
        wait_for(lambda: not json.loads(run("get", "pods", "-l", selector, "-o", "json"))["items"])

    for name in ("relay", "supervisor", "control"):
        pause_runtime(name)
    native = json.loads(run("get", "deployment/ml-control", "-o", "json"))
    pod = copy.deepcopy(native["spec"]["template"]["spec"])
    pod["serviceAccountName"] = "ml-serving"
    pod["restartPolicy"] = "Never"
    pod["activeDeadlineSeconds"] = 7200
    pod["initContainers"] = [c for c in pod["initContainers"] if c["name"] == "prepare-control"]
    container = next(c for c in pod["containers"] if c["name"] == "control")
    container["name"] = "operator"
    container["args"] = ["python", "-c", "import time; time.sleep(7200)"]
    container["volumeMounts"] = [
        m for m in container["volumeMounts"] if m["name"] != "kubernetes-api"
    ]
    pod["containers"] = [container]
    mounts = {
        m["name"] for c in pod["initContainers"] + pod["containers"] for m in c["volumeMounts"]
    }
    pod["volumes"] = [v for v in pod["volumes"] if v["name"] in mounts]
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "serving-qualification-operator",
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": "ml-system"},
        },
        "spec": pod,
    }
    if not args.resume_fixtures:
        run("create", "-f", "-", input=json.dumps(manifest))
    run("wait", "--for=condition=Ready", "pod/serving-qualification-operator", "--timeout=240s")

    def operator(mode, *values):
        output = run(
            "exec",
            "-i",
            "pod/serving-qualification-operator",
            "-c",
            "operator",
            "--",
            "python",
            "-m",
            "bootstrap.deployment_entrypoint",
            "python",
            "-",
            mode,
            *values,
            input=(ROOT / "scripts/k8s_serving_fixture.py").read_text(),
        )
        return json.loads(output.strip().splitlines()[-1])

    report = {
        "status": "RUNNING",
        "namespace": NAMESPACE,
        "production_qualified": False,
        "training_executor_qualified": False,
        "checks": [],
    }
    try:
        report["fixtures"] = {"retained": True} if args.resume_fixtures else operator("seed")
        run("scale", "deployment/ml-control", "--replicas=1")
        run("rollout", "status", "deployment/ml-control", "--timeout=240s")

        def status(deployment):
            return operator("status", deployment["deployment_id"])

        def active(deployment):
            def check():
                value = status(deployment)
                if value["state"] == "FAILED":
                    raise ValueError("Serving candidate failed")
                return value if value["state"] == "ACTIVE" else None

            return wait_for(check)

        def predict(deployment, kind="mlp"):
            def check():
                value = operator("predict", kind)
                return value if value["status"] == 200 else None

            value = wait_for(check)
            assert value["provenance"]["deployment_id"] == deployment["deployment_id"]
            assert value["provenance"]["generation"] == deployment["generation"]
            return value["provenance"]

        current = operator("current") if args.resume_fixtures else {}
        first = current if current.get("deployment_id") else operator("create", "0")
        before = active(first)
        initial = predict(first)
        report["checks"].append("approved_mlp_via_gateway")
        selector = "ml-system/serving-id=ml-serve-" + first["deployment_id"].replace("-", "")
        pods = json.loads(run("get", "pods", "-l", selector, "-o", "json"))["items"]
        run("delete", "pod/" + pods[0]["metadata"]["name"], "--wait=true")
        recovered = predict(first)
        assert recovered["pod_uid"] != initial["pod_uid"]
        assert status(first) == before
        report["checks"].append("pod_recreated_without_sql_activation")
        # Delete the controller-owned runtime resource; ACTIVE reconciliation must
        # rebuild the same spec and retain route revision, generation and approval.
        name = "ml-serve-" + first["deployment_id"].replace("-", "")
        run("delete", "deployment/" + name, "--cascade=foreground", "--wait=true")
        restored = predict(first)
        assert restored["pod_uid"] != recovered["pod_uid"]
        assert status(first) == before
        report["checks"].append("active_deployment_resource_reconstructed")
        pause_runtime("control")
        run("scale", "deployment/ml-control", "--replicas=1")
        run("rollout", "status", "deployment/ml-control", "--timeout=240s")
        assert predict(first)["pod_uid"] == restored["pod_uid"]
        assert status(first) == before
        report["checks"].append("controller_gateway_restart_preserves_active_route")
        second = operator("create", "1")
        active(second)
        predict(second)
        wait_for(lambda: status(first)["state"] == "RETIRED" and status(first)["released"])
        report["checks"].append("approved_update_retires_previous_pods_before_release")
        rollback = operator("rollback", first["deployment_id"])
        active(rollback)
        assert rollback["generation"] > second["generation"]
        predict(rollback)
        wait_for(lambda: status(second)["state"] == "RETIRED" and status(second)["released"])
        report["checks"].append("rollback_is_new_generation")
        ebm = operator("create", "2")
        active(ebm)
        predict(ebm, "ebm")
        wait_for(lambda: status(rollback)["state"] == "RETIRED" and status(rollback)["released"])
        report["checks"].append("approved_ebm_via_same_native_runtime_contract")
        report["platform_deployments"] = operator("history")
        pods = json.loads(run("get", "pods", "-l", "ml-system/stage=serving", "-o", "json"))[
            "items"
        ]
        report["runtime_pods"] = [
            {
                "uid": item["metadata"]["uid"],
                "node": item["spec"]["nodeName"],
                "images": [c["imageID"] for c in item["status"]["containerStatuses"]],
            }
            for item in pods
        ]
        report.update(
            status="SERVING_RUNTIME_PASSED",
            source_revision=settings["ML_CODE_REVISION"],
            runtime_image_digest=settings["ML_IMAGE_DIGEST"],
            recovered_route=before["route"],
            final_route=status(ebm)["route"],
        )
    finally:
        if report["status"] != "SERVING_RUNTIME_PASSED":
            report["status"] = "INCOMPLETE"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": report["checks"]}))


if __name__ == "__main__":
    main()
