"""Qualify real Pod Training, fenced recovery, drain and ACK on isolated Minikube."""

import argparse
import copy
import json
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "ml-k8s-training-runtime"
KUBECTL = ["kubectl", "--context=minikube", "--namespace=" + NAMESPACE]


def run(*arguments, **kwargs):
    return subprocess.run(
        [*KUBECTL, *arguments], check=True, text=True, capture_output=True, timeout=240, **kwargs
    ).stdout


def wait_for(check, timeout=100):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(0.3)
    raise TimeoutError("Training qualification condition was not observed")


def configure(directory):
    directory = directory.absolute()
    if ROOT / "var" not in directory.parents or any(
        p.is_symlink() for p in (directory, *directory.parents)
    ):
        raise ValueError("Qualification configuration must stay below ignored var/")
    path = directory / "kustomization.yaml"
    value = yaml.safe_load(path.read_text())
    if value["namespace"] != NAMESPACE or "minikube-training" not in str(value["resources"]):
        raise ValueError("Only the isolated Training profile can use fixture limits")
    literals = value["configMapGenerator"][1]["literals"]
    if any(item.startswith("ML_TRAINING_TIMEOUT_SECONDS=") for item in literals):
        raise ValueError("Retain the already-configured qualification directory")
    literals += [
        "ML_TRAINING_TIMEOUT_SECONDS=60",
        "ML_QUEUE_TIMEOUT_SECONDS=10",
        "ML_CANCEL_GRACE_SECONDS=1",
    ]
    path.write_text(yaml.safe_dump(value, sort_keys=False))
    print("Isolated Training fixture limits configured")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reuse-fixtures", action="store_true")
    args = parser.parse_args()
    if args.configure:
        configure(args.configure)
        return
    if not args.output or args.output.exists():
        raise ValueError("A new qualification receipt path is required")
    settings = json.loads(run("get", "configmap/ml-runtime", "-o", "json"))["data"]
    if (
        settings.get("ML_TRAINING_NAMESPACE") != NAMESPACE
        or settings.get("ML_TRAINING_BACKEND") != "pod"
    ):
        raise ValueError("An isolated Pod Training profile is required")
    # Operator only provisions authorized synthetic inputs/accounts. All training
    # commands go through Management HTTP, Outbox Relay and the actual Worker.
    native = json.loads(run("get", "deployment/ml-control", "-o", "json"))
    pod = copy.deepcopy(native["spec"]["template"]["spec"])
    pod.update(serviceAccountName="ml-serving", restartPolicy="Never", activeDeadlineSeconds=7200)
    pod["initContainers"] = [c for c in pod["initContainers"] if c["name"] == "prepare-control"]
    container = next(c for c in pod["containers"] if c["name"] == "control")
    container["name"] = "operator"
    container["args"] = ["python", "-c", "import time; time.sleep(7200)"]
    container["volumeMounts"] = [
        m for m in container["volumeMounts"] if m["name"] != "kubernetes-api"
    ]
    pod["containers"] = [container]
    mounts = {
        m["name"] for c in pod["containers"] + pod["initContainers"] for m in c["volumeMounts"]
    }
    pod["volumes"] = [v for v in pod["volumes"] if v["name"] in mounts]
    existing_operator = run(
        "get", "pod/training-qualification-operator", "--ignore-not-found", "-o", "name"
    ).strip()
    if not existing_operator:
        run(
            "create",
            "-f",
            "-",
            input=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": "training-qualification-operator",
                        "namespace": NAMESPACE,
                        "labels": {"app.kubernetes.io/part-of": "ml-system"},
                    },
                    "spec": pod,
                }
            ),
        )
    run("wait", "--for=condition=Ready", "pod/training-qualification-operator", "--timeout=180s")
    source = (ROOT / "scripts/k8s_training_fixture.py").read_text()
    fault_source = (ROOT / "scripts/k8s_training_fault.py").read_text()

    def operator(mode, *values):
        output = run(
            "exec",
            "-i",
            "pod/training-qualification-operator",
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
            input=source,
        )
        return json.loads(output.strip().splitlines()[-1])

    def worker_pod():
        items = json.loads(
            run("get", "pods", "-l", "app.kubernetes.io/name=ml-supervisor", "-o", "json")
        )["items"]
        return next(
            p
            for p in items
            if not p["metadata"].get("deletionTimestamp")
            and any(c.get("ready") for c in p.get("status", {}).get("containerStatuses", []))
        )

    def transport():
        broker = json.loads(
            run("get", "pods", "-l", "app.kubernetes.io/name=rabbitmq", "-o", "json")
        )["items"][0]
        address = broker["status"]["podIP"]
        output = run(
            "exec",
            "-i",
            "pod/" + worker_pod()["metadata"]["name"],
            "-c",
            "supervisor",
            "--",
            "python",
            "-",
            "transport",
            address,
            input=source,
        )
        return json.loads(output.strip().splitlines()[-1])

    def acked(job):
        value = transport()
        return (
            value
            if value["unacked"] == 0
            and value["snapshots"] is not None
            and any(s["job_id"] == job["job_id"] for s in value["snapshots"])
            else None
        )

    def status(job):
        return operator("status", job["job_id"])

    def terminal(job, expected):
        value = wait_for(
            lambda: (
                (v if v["state"] in {"SUCCEEDED", "FAILED", "CANCELLED"} else None)
                if (v := status(job))
                else None
            )
        )
        assert value["state"] == expected, value
        assert value["attempt_count"] == 1 and value["receipt"]["confirmed"] is True
        return value

    def fault(mode, execution):
        return run(
            "exec",
            "-i",
            "pod/" + worker_pod()["metadata"]["name"],
            "-c",
            "supervisor",
            "--",
            "python",
            "-",
            mode,
            execution,
            input=fault_source,
        )

    def paused_job():
        command = [
            *KUBECTL,
            "exec",
            "-i",
            "pod/" + worker_pod()["metadata"]["name"],
            "-c",
            "supervisor",
            "--",
            "python",
            "-",
            "freeze-next",
        ]
        watcher = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        watcher.stdin.write(fault_source)
        watcher.stdin.close()
        assert watcher.stdout.readline().strip() == "ARMED"
        job = operator("submit", "mlp")
        frozen = json.loads(watcher.stdout.readline())
        watcher.wait(timeout=100)
        assert watcher.returncode == 0 and frozen["frozen"]
        return job

    report = {
        "status": "INCOMPLETE",
        "namespace": NAMESPACE,
        "production_qualified": False,
        "cross_node_qualified": False,
        "checks": [],
        "jobs": [],
    }
    try:
        if not args.reuse_fixtures:
            operator("init")
        mlp = operator("submit", "mlp")
        report["jobs"].append(terminal(mlp, "SUCCEEDED"))
        wait_for(lambda: acked(mlp))
        report["checks"].append("real_mlp_management_outbox_worker_and_manual_ack")
        ebm = operator("submit", "ebm")
        report["jobs"].append(terminal(ebm, "SUCCEEDED"))
        wait_for(lambda: acked(ebm))
        report["checks"].append("real_ebm_same_pod_runtime_contract")
        cancelled = paused_job()
        operator("cancel", cancelled["job_id"])
        report["jobs"].append(terminal(cancelled, "CANCELLED"))
        wait_for(lambda: acked(cancelled))
        report["checks"].append("cancel_confirms_compute_group_stop_before_ack")
        deadline = paused_job()
        value = terminal(deadline, "FAILED")
        assert value["error_code"] == "TRAINING_DEADLINE"
        report["jobs"].append(value)
        wait_for(lambda: acked(deadline))
        report["checks"].append("guardian_wall_deadline_terminates_paused_compute")
        recovered = paused_job()
        before = status(recovered)
        fault("kill-worker", before["execution_id"])
        wait_for(lambda: status(recovered)["state"] == "NEEDS_RECONCILIATION", timeout=35)
        fault("resume", before["execution_id"])
        after = terminal(recovered, "SUCCEEDED")
        assert (
            after["execution_id"] == before["execution_id"]
            and after["pod_uid"] == before["pod_uid"]
        )
        report["jobs"].append(after)
        wait_for(lambda: acked(recovered))
        report["checks"].append("worker_crash_control_recovers_same_fit_and_redelivery_ack")
        run("scale", "deployment/redis", "--replicas=0")
        wait_for(
            lambda: (
                not json.loads(
                    run("get", "pods", "-l", "app.kubernetes.io/name=redis", "-o", "json")
                )["items"]
            )
        )
        technical = operator("submit", "mlp")
        report["jobs"].append(terminal(technical, "SUCCEEDED"))
        wait_for(lambda: transport()["unacked"] == 1)
        run("scale", "deployment/redis", "--replicas=1")
        run("rollout", "status", "deployment/redis", "--timeout=120s")
        wait_for(lambda: acked(technical))
        report["checks"].append("redis_outage_preserves_terminal_sql_and_unacked_delivery")
        drained = paused_job()
        old = worker_pod()
        run(
            "delete",
            "pod/" + old["metadata"]["name"],
            "--grace-period=100",
            "--wait=true",
            "--timeout=150s",
        )
        run("rollout", "status", "deployment/ml-supervisor", "--timeout=180s")
        value = terminal(drained, "FAILED")
        assert value["error_code"] == "SUPERVISOR_DRAIN"
        assert worker_pod()["metadata"]["uid"] != old["metadata"]["uid"]
        report["jobs"].append(value)
        wait_for(lambda: acked(drained))
        report["checks"].append("normal_supervisor_pod_drain_confirms_stop_and_new_pod_identity")
        final = transport()
        assert final["ready"] == 0 and final["unacked"] == 0
        report.update(
            status="POD_TRAINING_RUNTIME_PASSED",
            source_revision=settings["ML_CODE_REVISION"],
            runtime_image_digest=settings["ML_IMAGE_DIGEST"],
            transport=final,
        )
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": report["checks"]}))


if __name__ == "__main__":
    main()
