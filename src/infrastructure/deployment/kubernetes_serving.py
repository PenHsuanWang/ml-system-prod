"""Namespaced Kubernetes adapter for one immutable, approved Serving deployment.

The SQL route remains authoritative. Kubernetes generation and replica counters
never allocate a platform generation or authorize an activation.
"""

import os
import re
import ssl
from pathlib import Path

import httpx

from src.domain.deployments.identity import deployment_spec, matches_identity
from src.domain.deployments.models import Deployment
from src.domain.ml import Conflict, Unavailable
from src.infrastructure.deployment.endpoints import KubernetesEndpointResolver, resource_name

MANAGED = "ml-system/serving-id"
DIGEST = "ml-system/spec-digest"


class KubernetesAPI:
    def __init__(self, client: httpx.Client, token_path: Path) -> None:
        self.client = client
        self.token_path = token_path

    @classmethod
    def in_cluster(cls):
        root = Path("/run/kubernetes")
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443"))
        if ":" in host:
            host = "[" + host + "]"
        client = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=ssl.create_default_context(cafile=str(root / "ca.crt")),
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        )
        return cls(client, root / "token")

    def request(self, method: str, path: str, *, body=None, params=None):
        try:
            # Re-read the projected token on every call to support kubelet rotation.
            token = self.token_path.read_text().strip()
            response = self.client.request(
                method, path, json=body, params=params, headers={"Authorization": "Bearer " + token}
            )
            if response.status_code == 404:
                return None
            if response.status_code == 409:
                raise Conflict("Kubernetes resource ownership conflict")
            if not response.is_success:
                raise Unavailable("Kubernetes API is unavailable")
            value = response.json()
            if not isinstance(value, dict):
                raise Unavailable("Invalid Kubernetes API response")
            return value
        except (httpx.HTTPError, OSError, ValueError):
            raise Unavailable("Kubernetes API is unavailable") from None


def _contains(actual, expected) -> bool:
    """Compare declared fields while allowing Kubernetes' defaulted object fields."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _contains(actual[k], v) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_contains(a, e) for a, e in zip(actual, expected, strict=True))
        )
    return type(actual) is type(expected) and actual == expected


class KubernetesServingExecutor:
    def __init__(
        self, api: KubernetesAPI, namespace: str, image_repository: str, *, health_client=None
    ) -> None:
        self.api = api
        self.endpoints = KubernetesEndpointResolver(namespace)
        self.namespace = namespace
        if re.fullmatch(r"[a-zA-Z0-9._:/-]+", image_repository) is None:
            raise ValueError("Invalid Serving image repository")
        self.image_repository = image_repository
        self.health = health_client or httpx.Client(
            timeout=2, trust_env=False, follow_redirects=False
        )

    def path(self, plural: str, name: str = "") -> str:
        group = "apis/apps/v1" if plural in {"deployments", "replicasets"} else "api/v1"
        return f"/{group}/namespaces/{self.namespace}/{plural}" + ("/" + name if name else "")

    def labels(self, deployment: Deployment) -> dict:
        return {
            MANAGED: resource_name(deployment),
            DIGEST: deployment.spec_digest[:32],
            "app.kubernetes.io/part-of": "ml-system",
            "ml-system/stage": "serving",
        }

    def metadata(self, deployment: Deployment) -> dict:
        return {
            "name": resource_name(deployment),
            "namespace": self.namespace,
            "labels": self.labels(deployment),
            "annotations": {DIGEST: deployment.spec_digest},
        }

    def resources(self, deployment: Deployment) -> dict:
        spec = deployment_spec(deployment)
        if (
            re.fullmatch(r"sha256:[a-f0-9]{64}", spec["runtime_image_digest"]) is None
            or re.fullmatch(r"[a-f0-9]{40}", spec.get("runtime_code_revision", "")) is None
        ):
            raise Conflict("Kubernetes Serving requires a qualified immutable image and source")
        image = self.image_repository + "@" + spec["runtime_image_digest"]
        metadata = self.metadata(deployment)
        security = {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        env = [
            {"name": "ML_RUNTIME_ROLE", "value": "serving"},
            {"name": "ML_IMAGE_DIGEST", "value": spec["runtime_image_digest"]},
            {"name": "ML_CODE_REVISION", "value": spec["runtime_code_revision"]},
            {"name": "ML_SERVING_BACKEND", "value": "local"},
            {"name": "ML_WAIT_FOR_SCHEMA", "value": "false"},
            {"name": "ML_DEPLOYMENT_SPEC_PATH", "value": "/run/model/spec.json"},
            {"name": "ML_DEPLOYMENT_SPEC_DIGEST", "value": deployment.spec_digest},
            {
                "name": "ML_SERVING_POD_UID",
                "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
            },
        ]
        common = {
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "env": env,
            "envFrom": [{"configMapRef": {"name": "ml-runtime"}}],
            "securityContext": security,
            "resources": {
                "requests": {"cpu": "50m", "memory": "256Mi", "ephemeral-storage": "128Mi"},
                "limits": {"cpu": "1", "memory": "1536Mi", "ephemeral-storage": "2Gi"},
            },
        }
        runtime = {"name": "runtime", "mountPath": "/app/var"}
        tmp = {"name": "tmp", "mountPath": "/tmp"}
        private = {"name": "private", "mountPath": "/run/private"}
        init = {
            **common,
            "name": "prepare-serving",
            "command": ["python", "/foundation/prepare_runtime.py"],
            "volumeMounts": [
                runtime,
                tmp,
                private,
                {"name": "foundation", "mountPath": "/foundation", "readOnly": True},
                {"name": "role-source", "mountPath": "/projected/role", "readOnly": True},
                {"name": "ca-source", "mountPath": "/projected/ca", "readOnly": True},
            ],
        }
        server = {
            **common,
            "name": "serving",
            "command": ["python", "-m", "bootstrap.deployment_entrypoint"],
            "args": ["python", "-m", "bootstrap.ml_serving", "--host", "0.0.0.0", "--port", "8001"],
            "ports": [{"name": "http", "containerPort": 8001}],
            "volumeMounts": [
                runtime,
                tmp,
                {**private, "readOnly": True},
                {"name": "spec", "mountPath": "/run/model", "readOnly": True},
            ],
            "startupProbe": {
                "httpGet": {"path": "/health/ready", "port": 8001},
                "periodSeconds": 2,
                "failureThreshold": 90,
            },
            "readinessProbe": {
                "httpGet": {"path": "/health/ready", "port": 8001},
                "periodSeconds": 2,
            },
            "livenessProbe": {
                "httpGet": {"path": "/health/live", "port": 8001},
                "periodSeconds": 10,
            },
        }
        pod = {
            "serviceAccountName": "ml-serving",
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 40,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "initContainers": [init],
            "containers": [server],
            "volumes": [
                {"name": "runtime", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                {"name": "private", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
                {
                    "name": "foundation",
                    "configMap": {"name": "ml-foundation-files", "defaultMode": 292},
                },
                {
                    "name": "role-source",
                    "secret": {"secretName": "ml-runtime-serving", "defaultMode": 288},
                },
                {"name": "ca-source", "configMap": {"name": "mlflow-ca", "defaultMode": 292}},
                {
                    "name": "spec",
                    "configMap": {"name": resource_name(deployment), "defaultMode": 292},
                },
            ],
        }
        return {
            "deployments": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": metadata,
                "spec": {
                    "replicas": 1,
                    "progressDeadlineSeconds": 180,
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": self.labels(deployment)},
                    "template": {"metadata": {"labels": self.labels(deployment)}, "spec": pod},
                },
            },
            "configmaps": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": metadata,
                "immutable": True,
                "data": {"spec.json": deployment.spec_json},
            },
            "services": {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": metadata,
                "spec": {
                    "type": "ClusterIP",
                    "selector": self.labels(deployment),
                    "ports": [{"name": "http", "port": 8001, "targetPort": 8001}],
                },
            },
        }

    def ensure(self, plural: str, desired: dict):
        name = desired["metadata"]["name"]
        actual = self.api.request("GET", self.path(plural, name))
        if actual is None:
            try:
                actual = self.api.request("POST", self.path(plural), body=desired)
            except Conflict:
                actual = self.api.request("GET", self.path(plural, name))
        if (
            actual is None
            or actual.get("metadata", {}).get("deletionTimestamp")
            or not _contains(actual, desired)
        ):
            raise Conflict(
                "Existing Kubernetes Serving resource differs from its approved specification"
            )
        return actual

    def prepare(self, deployment: Deployment) -> None:
        resources = self.resources(deployment)
        # A removed API object is not proof that its old physical instance stopped.
        # Wait for lingering Pods instead of creating a second incarnation.
        if self.api.request(
            "GET", self.path("deployments", resource_name(deployment))
        ) is None and self.pods(deployment):
            raise Unavailable("Previous Serving incarnation still requires reconciliation")
        native = self.ensure("deployments", resources["deployments"])
        owner = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": resource_name(deployment),
            "uid": native["metadata"]["uid"],
            "controller": True,
            "blockOwnerDeletion": False,
        }
        for plural in ("configmaps", "services"):
            resource = resources[plural]
            resource["metadata"] = {**resource["metadata"], "ownerReferences": [owner]}
            self.ensure(plural, resource)

    def pods(self, deployment: Deployment) -> list:
        value = self.api.request(
            "GET",
            self.path("pods"),
            params={"labelSelector": MANAGED + "=" + resource_name(deployment)},
        )
        if value is None or not isinstance(value.get("items"), list):
            raise Unavailable("Kubernetes Pod observation is unavailable")
        return value["items"]

    def ready(self, deployment: Deployment) -> bool:
        native = self.api.request("GET", self.path("deployments", resource_name(deployment)))
        if (
            native is None
            or native["metadata"].get("deletionTimestamp")
            or not _contains(native, self.resources(deployment)["deployments"])
        ):
            return False
        if any(
            condition.get("type") == "Progressing"
            and condition.get("status") == "False"
            and condition.get("reason") == "ProgressDeadlineExceeded"
            for condition in native.get("status", {}).get("conditions", [])
        ):
            raise Conflict("Serving startup exceeded its bounded progress deadline")
        try:
            response = self.health.get(self.endpoints.resolve(deployment) + "/health/ready")
            actual = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        if (
            response.status_code != 200
            or not isinstance(actual, dict)
            or actual.get("ready") is not True
            or actual.get("tenant_id") != deployment.tenant_id
            or not matches_identity(deployment, actual)
        ):
            return False
        for pod in self.pods(deployment):
            if (
                pod["metadata"]["uid"] != actual.get("pod_uid")
                or pod["metadata"].get("deletionTimestamp")
                or not _contains(pod["metadata"].get("labels", {}), self.labels(deployment))
                or not any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
            ):
                continue
            owners = pod["metadata"].get("ownerReferences", [])
            if (
                len(owners) != 1
                or owners[0].get("kind") != "ReplicaSet"
                or not owners[0].get("controller")
            ):
                return False
            rs = self.api.request("GET", self.path("replicasets", owners[0]["name"]))
            return bool(
                rs
                and rs["metadata"]["uid"] == owners[0]["uid"]
                and any(
                    o.get("kind") == "Deployment"
                    and o.get("uid") == native["metadata"]["uid"]
                    and o.get("controller")
                    for o in rs["metadata"].get("ownerReferences", [])
                )
            )
        return False

    def retire(self, deployment: Deployment) -> bool:
        # Do not release a SQL slot on API failure or while any managed Pod exists.
        try:
            name = resource_name(deployment)
            for plural in ("deployments", "services", "configmaps"):
                actual = self.api.request("GET", self.path(plural, name))
                if actual is None:
                    continue
                if not _contains(actual["metadata"], self.metadata(deployment)):
                    return False
                if not actual["metadata"].get("deletionTimestamp"):
                    self.api.request(
                        "DELETE",
                        self.path(plural, name),
                        body={
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "propagationPolicy": "Foreground",
                            "preconditions": {"uid": actual["metadata"]["uid"]},
                        },
                    )
            return (
                self.api.request("GET", self.path("deployments", name)) is None
                and not self.pods(deployment)
                and all(
                    self.api.request("GET", self.path(p, name)) is None
                    for p in ("services", "configmaps")
                )
            )
        except (Unavailable, Conflict):
            return False
