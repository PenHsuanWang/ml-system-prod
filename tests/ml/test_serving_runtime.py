"""Runtime recovery, immutable resource ownership and Gateway route fencing."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from bootstrap.ml_gateway import create_app
from src.application.deployments.service import DeploymentController
from src.domain.deployments.identity import serving_identity
from src.domain.deployments.models import Deployment, Route
from src.domain.ml import Actor, Conflict, Unavailable, canonical_json, digest
from src.infrastructure.deployment.endpoints import (
    KubernetesEndpointResolver,
    LocalEndpointResolver,
    resource_name,
)
from src.infrastructure.deployment.kubernetes_serving import (
    DIGEST,
    KubernetesAPI,
    KubernetesServingExecutor,
)


@pytest.fixture
def deployment():
    spec = {
        "schema_version": "1",
        "deployment_id": str(uuid4()),
        "tenant_id": "tenant-one",
        "endpoint_id": "quality",
        "generation": 3,
        "manifest_digest": "a" * 64,
        "registered_name": "tenant-one-model",
        "model_version": "17",
        "runtime_image_digest": "sha256:" + "b" * 64,
        "runtime_code_revision": "c" * 40,
    }
    text = canonical_json(spec)
    return Deployment(
        spec["deployment_id"],
        spec["tenant_id"],
        spec["endpoint_id"],
        3,
        "binding",
        "approval",
        "ACTIVE",
        8,
        text,
        digest(spec),
    )


class FakeAPI:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.pod_items = []
        self.unavailable = False
        self.retain_deleting = False

    def request(self, method, path, *, body=None, params=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.unavailable:
            raise Unavailable("API unavailable")
        if method == "GET" and path.endswith("/pods"):
            return {"items": copy.deepcopy(self.pod_items)}
        if method == "GET":
            return copy.deepcopy(self.objects.get(path))
        if method == "POST":
            key = path + "/" + body["metadata"]["name"]
            if key in self.objects:
                raise Conflict("Already exists")
            value = copy.deepcopy(body)
            value["metadata"]["uid"] = str(uuid4())
            self.objects[key] = value
            return copy.deepcopy(value)
        if method == "DELETE":
            assert body["preconditions"]["uid"] == self.objects[path]["metadata"]["uid"]
            assert body["propagationPolicy"] == "Foreground"
            if self.retain_deleting:
                self.objects[path]["metadata"]["deletionTimestamp"] = "now"
            else:
                del self.objects[path]
            return {"status": "Success"}
        raise AssertionError(method)


@pytest.fixture
def executor():
    return KubernetesServingExecutor(FakeAPI(), "serving-test", "registry.example/runtime")


def test_native_preparation_is_idempotent_and_immutable(executor, deployment):
    executor.prepare(deployment)
    original = copy.deepcopy(executor.api.objects)
    executor.prepare(deployment)
    assert original == executor.api.objects
    assert len([c for c in executor.api.calls if c[0] == "POST"]) == 3
    spec = executor.resources(deployment)
    assert spec["configmaps"]["immutable"] is True
    pod = spec["deployments"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert all("persistentVolumeClaim" not in v for v in pod["volumes"])
    assert "ml-runtime-serving" in str(pod)
    assert "ml-runtime-control" not in str(pod)
    assert "kubernetes-api" not in str(pod)
    assert pod["containers"][0]["image"].endswith("@sha256:" + "b" * 64)


@pytest.mark.parametrize("plural", ["deployments", "configmaps", "services"])
def test_native_resources_are_never_adopted_or_overwritten(executor, deployment, plural):
    executor.prepare(deployment)
    path = executor.path(plural, resource_name(deployment))
    executor.api.objects[path]["metadata"]["annotations"][DIGEST] = "foreign"
    with pytest.raises(Conflict, match="differs"):
        executor.prepare(deployment)
    assert executor.retire(deployment) is False
    assert path in executor.api.objects


@pytest.mark.parametrize("mutation", ["image", "selector", "spec"])
def test_native_resource_payload_tampering_blocks_reconciliation(executor, deployment, mutation):
    executor.prepare(deployment)
    if mutation == "image":
        executor.api.objects[executor.path("deployments", resource_name(deployment))]["spec"][
            "template"
        ]["spec"]["containers"][0]["image"] = "untrusted:latest"
    elif mutation == "selector":
        executor.api.objects[executor.path("services", resource_name(deployment))]["spec"][
            "selector"
        ] = {}
    else:
        executor.api.objects[executor.path("configmaps", resource_name(deployment))]["data"][
            "spec.json"
        ] = "{}"
    with pytest.raises(Conflict):
        executor.prepare(deployment)


def test_native_recreates_missing_runtime_with_same_platform_identity(executor, deployment):
    executor.prepare(deployment)
    spec = executor.resources(deployment)
    executor.api.objects.clear()  # GC of an accidentally deleted native Deployment.
    executor.prepare(deployment)
    assert executor.resources(deployment) == spec


@pytest.mark.parametrize("obstacle", ["api", "terminating", "pod"])
def test_retirement_retains_capacity_until_observed_stop(executor, deployment, obstacle):
    executor.prepare(deployment)
    if obstacle == "api":
        executor.api.unavailable = True
    elif obstacle == "terminating":
        executor.api.retain_deleting = True
    else:
        executor.api.pod_items = [{"metadata": {"uid": "old-pod"}}]
    assert executor.retire(deployment) is False
    executor.api.unavailable = executor.api.retain_deleting = False
    executor.api.pod_items = []
    executor.api.objects.clear()
    assert executor.retire(deployment) is True


def test_native_ready_proves_pod_and_replicaset_owner(executor, deployment):
    executor.prepare(deployment)
    native = executor.api.objects[executor.path("deployments", resource_name(deployment))]
    executor.api.pod_items = [
        {
            "metadata": {
                "uid": "pod-uid",
                "labels": executor.labels(deployment),
                "ownerReferences": [
                    {"kind": "ReplicaSet", "name": "rs", "uid": "rs-uid", "controller": True}
                ],
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
    ]
    rs = {
        "metadata": {
            "uid": "rs-uid",
            "ownerReferences": [
                {"kind": "Deployment", "controller": True, "uid": native["metadata"]["uid"]}
            ],
        }
    }
    executor.api.objects[executor.path("replicasets", "rs")] = rs
    health = {
        **serving_identity(deployment),
        "ready": True,
        "tenant_id": deployment.tenant_id,
        "pod_uid": "pod-uid",
    }
    executor.health = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=health))
    )
    assert executor.ready(deployment)
    rs["metadata"]["ownerReferences"][0]["uid"] = "other-deployment"
    assert executor.ready(deployment) is False
    health["pod_uid"] = "stale-pod"
    assert executor.ready(deployment) is False


def test_api_rotates_token_and_never_follows_redirects(tmp_path):
    token = tmp_path / "token"
    observed = []

    def handle(request):
        observed.append(request.headers["authorization"])
        return httpx.Response(302, headers={"location": "https://outside.example"})

    api = KubernetesAPI(
        httpx.Client(base_url="https://kubernetes", transport=httpx.MockTransport(handle)), token
    )
    for value in ("first", "rotated"):
        token.write_text(value)
        with pytest.raises(Unavailable):
            api.request("GET", "/api/v1/namespaces/test/pods")
    assert observed == ["Bearer first", "Bearer rotated"]


def test_resolvers_use_trusted_identity_and_local_loopback(tmp_path, deployment):
    assert (
        KubernetesEndpointResolver("tenant-test").resolve(deployment)
        == f"http://{resource_name(deployment)}.tenant-test.svc:8001"
    )
    directory = tmp_path / deployment.deployment_id
    directory.mkdir()
    instance = directory / "instance.json"
    for value in (
        "http://outside.example:8001",
        "http://127.0.0.1:8001@outside.example",
        "http://127.0.0.1:8001/redirect",
    ):
        instance.write_text(json.dumps({"address": value}))
        with pytest.raises(Conflict):
            LocalEndpointResolver(tmp_path).resolve(deployment)
    instance.write_text(json.dumps({"address": "http://127.0.0.1:8001"}))
    assert LocalEndpointResolver(tmp_path).resolve(deployment) == "http://127.0.0.1:8001"


@pytest.mark.parametrize("stale", [False, True])
def test_active_recovery_never_activates_or_releases(deployment, stale):
    calls = []
    repo = SimpleNamespace(
        route=lambda *a: Route(
            deployment.tenant_id, "quality", 9, "newer" if stale else deployment.deployment_id, 3
        )
    )
    executor = SimpleNamespace(
        prepare=lambda d: calls.append("prepare"), ready=lambda d: calls.append("ready")
    )
    DeploymentController(repo, executor).reconcile(deployment)
    assert calls == ([] if stale else ["prepare", "ready"])


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "health-generation",
        "health-tenant",
        "health-manifest",
        "health-spec",
        "response-generation",
        "response-model",
        "response-spec",
        "response-pod",
        "route-change",
        "redirect",
        "tenant-denied",
    ],
)
def test_gateway_verifies_health_response_and_current_route(deployment, failure):
    route = Route(deployment.tenant_id, "quality", 9, deployment.deployment_id, 3)
    reads = []

    def current_route(*args):
        reads.append(args)
        return (
            replace(route, revision=10) if failure == "route-change" and len(reads) > 1 else route
        )

    repo = SimpleNamespace(route=current_route, get=lambda *a: deployment)
    identity = SimpleNamespace()

    async def resolve(token):
        return Actor(
            deployment.tenant_id,
            "predictor",
            frozenset() if failure == "tenant-denied" else frozenset({"ml:predict"}),
        )

    identity.resolve = resolve
    health = {
        **serving_identity(deployment),
        "ready": True,
        "tenant_id": deployment.tenant_id,
        "pod_uid": "pod",
    }
    provenance = {**serving_identity(deployment), "pod_uid": "pod"}
    changes = {
        "health-generation": (health, "generation", 4),
        "health-tenant": (health, "tenant_id", "other"),
        "health-manifest": (health, "manifest_digest", "other"),
        "health-spec": (health, "spec_digest", "other"),
        "response-generation": (provenance, "generation", 4),
        "response-model": (provenance, "model_version", "other"),
        "response-spec": (provenance, "spec_digest", "other"),
        "response-pod": (provenance, "pod_uid", "other"),
    }
    if failure in changes:
        value, key, change = changes[failure]
        value[key] = change

    def handle(request):
        assert request.url.host.endswith(".serving-test.svc")
        if request.method == "GET":
            return httpx.Response(200, json=health)
        assert request.headers["authorization"] == "Bearer scoped-token"
        return httpx.Response(
            302 if failure == "redirect" else 200,
            json={"predictions": [], "provenance": provenance},
        )

    app = create_app(
        components=SimpleNamespace(deployments=repo),
        identity=identity,
        resolver=KubernetesEndpointResolver("serving-test"),
        transport=httpx.MockTransport(handle),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/prediction/predict",
            headers={"Authorization": "Bearer scoped-token"},
            json={
                "schema_version": "1",
                "endpoint_id": "quality",
                "instances": [{"features": {"sensor_a": 1, "sensor_b": 2}}],
            },
        )
    assert response.status_code == (
        200 if failure == "none" else 403 if failure == "tenant-denied" else 503
    )


def test_failed_candidate_has_bounded_native_progress_deadline(executor, deployment):
    executor.prepare(deployment)
    native = executor.api.objects[executor.path("deployments", resource_name(deployment))]
    native["status"] = {
        "conditions": [
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
        ]
    }
    with pytest.raises(Conflict, match="deadline"):
        executor.ready(replace(deployment, state="STARTING"))


def test_missing_native_deployment_does_not_duplicate_lingering_pod(executor, deployment):
    executor.api.pod_items = [{"metadata": {"uid": "lingering-pod"}}]
    with pytest.raises(Unavailable, match="incarnation"):
        executor.prepare(deployment)
    assert not executor.api.objects
    assert not [call for call in executor.api.calls if call[0] == "POST"]
