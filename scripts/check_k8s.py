"""Check the rendered foundation's security and topology without accessing a cluster."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def check(documents: list[dict]) -> None:
    resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
    assert len(resources) == len(documents), "Duplicate resource identities"
    assert not any(
        kind
        in {
            "Secret",
            "Role",
            "RoleBinding",
            "ClusterRole",
            "ClusterRoleBinding",
            "HorizontalPodAutoscaler",
        }
        for kind, _ in resources
    ), "Unexpected credentials, API permissions or autoscaling"
    roles = {
        "management",
        "relay",
        "supervisor",
        "control",
        "gateway",
        "ingestion-worker",
        "migrate",
    }
    observed = set()
    for (kind, name), document in resources.items():
        if kind not in {"Deployment", "Job"} or not name.startswith("ml-"):
            continue
        pod = document["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsUser"] == 10001
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        if kind == "Deployment":
            assert document["spec"]["replicas"] == 1
            assert document["spec"]["strategy"]["type"] == "Recreate"
        assert pod["terminationGracePeriodSeconds"] >= 330
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        for container in pod["containers"]:
            role = container["name"]
            observed.add(role)
            assert container["command"] == ["python", "-m", "bootstrap.deployment_entrypoint"]
            assert container["securityContext"]["readOnlyRootFilesystem"] is True
            assert container["securityContext"]["allowPrivilegeEscalation"] is False
            assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
            assert {"requests", "limits"} <= container["resources"].keys()
            if kind == "Deployment":
                assert "ephemeral-storage" in container["resources"]["requests"]
                assert "ephemeral-storage" in container["resources"]["limits"]
            assert not any("secretKeyRef" in item.get("valueFrom", {}) for item in container["env"])
            mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
            assert mounts[role + "-private"]["readOnly"] is True
            assert role + "-source" not in mounts and "ca-source" not in mounts
            if kind == "Deployment":
                assert mounts["runtime"]["mountPath"] == "/app/var"
                assert mounts["artifact-cache"]["mountPath"] == "/app/var/artifacts"
                assert volumes["artifact-cache"]["emptyDir"]["sizeLimit"] == "1Gi"
            assert volumes[role + "-private"]["emptyDir"]["medium"] == "Memory"
            assert any(init["name"] == "prepare-" + role for init in pod["initContainers"])
        if name == "ml-control":
            assert {container["name"] for container in pod["containers"]} == {"control", "gateway"}
            assert not pod.get("shareProcessNamespace", False), (
                "Shared PID namespace is not a recovery design"
            )
    assert observed == roles
    config = resources[("ConfigMap", "ml-runtime")]["data"]
    assert config["ML_MODEL_ENVIRONMENT"] == "prod"
    assert config["MLFLOW_TRACKING_INSECURE_TLS"] == "false"
    assert config["ML_TRACKING_URI"] == config["MLFLOW_TRACKING_URI"] == "https://mlflow:8443"
    assert config["ML_DATABRICKS_ENABLED"] == "false"
    assert resources[("Job", "ml-migrate")]["spec"]["backoffLimit"] == 0
    for (kind, _), document in resources.items():
        if kind == "Service":
            assert document["spec"].get("type", "ClusterIP") == "ClusterIP"


def main() -> None:
    result = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / "deploy/k8s/overlays/minikube")],
        check=True,
        capture_output=True,
        text=True,
    )
    documents = list(yaml.safe_load_all(result.stdout))
    check(documents)
    print(f"Foundation manifest contracts passed: {len(documents)} resources; no cluster access")


if __name__ == "__main__":
    main()
