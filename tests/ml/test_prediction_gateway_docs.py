"""Exercise first-use instructions against the public gateway and its HTTP contracts."""

import httpx
import pytest

from bootstrap.ml_gateway import create_app
from src.api.ebm_schemas import EBMPredictionRequest
from src.api.prediction_docs import GatewayPredictionResult
from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER


@pytest.mark.asyncio
async def test_user_can_find_docs_and_submit_documented_example_under_proxy():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/inference"),
        base_url="https://example.test/inference/",
    ) as client:
        response = await client.get("")
        assert response.headers["location"] == "/inference/docs"
        docs = await client.get(response.next_request.url)
        assert docs.status_code == 200 and "/inference/openapi.json" in docs.text
        assert (await client.get("redoc")).status_code == 200
        spec = (await client.get("openapi.json")).json()
        ids = [op["operationId"] for methods in spec["paths"].values() for op in methods.values()]
        assert len(ids) == len(set(ids))
        assert "predict_api_v1_prediction_predict_post" in ids
        path = "/api/v1/prediction/predict"
        operation = spec["paths"][path]["post"]
        schemes = spec["components"]["securitySchemes"]
        assert operation["security"] == [{"HTTPBearer": []}]
        assert schemes["HTTPBearer"]["scheme"] == "bearer"

        example = operation["requestBody"]["content"]["application/json"]["examples"]["mlp"][
            "value"
        ]
        denied = await client.post(path.lstrip("/"), json=example)
        assert denied.status_code == 401 and denied.json()["code"] == "UNAUTHENTICATED"
        invalid = await client.post(path.lstrip("/"), json={**example, "endpoint_id": "#"})
        assert invalid.status_code == 422 and invalid.json()["code"] == "SCHEMA_MISMATCH"
        for response in (denied, invalid):
            schema = operation["responses"][str(response.status_code)]["content"][
                "application/json"
            ]
            name = schema["schema"]["$ref"].split("/")[-1]
            assert set(spec["components"]["schemas"][name]["required"]) <= response.json().keys()

        success = operation["responses"]["200"]["content"]["application/json"]["schema"]
        schema = spec["components"]["schemas"][success["$ref"].split("/")[-1]]
        result = GatewayPredictionResult.model_validate(schema["examples"][0])
        assert result.provenance.endpoint_id == example["endpoint_id"]


def test_ebm_instructions_identify_accepted_signal_and_pad_order():
    spec = create_app().openapi()
    description = spec["paths"]["/api/v1/prediction/predict"]["post"]["description"]
    assert repr(list(EBM_SIGNAL_ORDER)) in description
    assert repr(list(EBM_PAD_ORDER)) in description
    operation = spec["paths"]["/api/v1/prediction/predict"]["post"]
    example = operation["requestBody"]["content"]["application/json"]["examples"]["ebm"]["value"]
    request = EBMPredictionRequest.model_validate(example)
    assert len(request.to_domain().spectrograms) == 1


@pytest.fixture
def gateway_runtime():
    """Real HTTP error handlers and bounded execution, with isolated identity/route fixtures."""
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.api.ml_http import install_errors
    from src.domain.deployments.identity import serving_identity
    from src.domain.deployments.models import Deployment, Route
    from src.domain.ml import Actor, NotFound, canonical_json, digest
    from src.infrastructure.ml_identity import AuthenticationRequired
    from src.infrastructure.ml_tracing import MLTraceMiddleware
    from src.infrastructure.serving.bounded_prediction import BoundedPredictionExecutor

    spec = {
        "tenant_id": "synthetic-tenant",
        "endpoint_id": "synthetic-quality",
        "deployment_id": "synthetic-deployment",
        "generation": 3,
        "registered_name": "synthetic-mlp",
        "model_version": "7",
        "manifest_digest": "a" * 64,
    }
    deployment = Deployment(
        spec["deployment_id"],
        spec["tenant_id"],
        spec["endpoint_id"],
        3,
        "binding",
        "approval",
        "ACTIVE",
        0,
        canonical_json(spec),
        digest(spec),
    )
    identity_fields = serving_identity(deployment)
    state = {"mode": "mlp", "calls": 0}
    release = threading.Event()

    class SlowRuntime:
        def predict(self, values):
            release.wait(timeout=5)
            return [{"class_id": 1, "label": "positive", "score": 0.7}]

        def close(self):
            pass

    executor = BoundedPredictionExecutor(SlowRuntime(), capacity=1, timeout=0.01)
    upstream = FastAPI()
    install_errors(upstream)
    upstream.add_middleware(MLTraceMiddleware)

    @upstream.get("/health/ready")
    def health():
        return {**identity_fields, "ready": True, "tenant_id": spec["tenant_id"], "pod_uid": ""}

    @upstream.post("/api/v1/prediction/predict")
    def predict(request: Request):
        state["calls"] += 1
        if state["mode"] == "slow":
            executor.predict(((0.2, -0.1),))
        predictions = [
            {"class_id": 1, "label": "positive", "score": 0.7, "score_kind": "uncalibrated_softmax"}
        ]
        version = "1"
        if state["mode"] == "ebm":
            predictions = [
                {
                    "mu": 0.2,
                    "sigma": 0.1,
                    "latent": [0.3],
                    "raw_nn_score": 0.3645,
                    "score_kind": "uncalibrated_latent_mean_plus_std",
                }
            ]
            version = "ebm-spectrogram-input-v1"
        if state["mode"] == "wrongtypes":
            predictions[0]["score"] = "0.7"
        if state["mode"] == "malformed":
            predictions = [{"invalid": "synthetic-private-upstream-error"}]
        return {
            "predictions": predictions,
            "provenance": {**identity_fields, "input_schema_version": version, "pod_uid": ""},
            "request_id": request.state.request_id,
        }

    async def resolve(token):
        if token in {"invalid", "expired", "revoked"}:
            raise AuthenticationRequired("Invalid credentials")
        return Actor(
            "other" if token == "other" else spec["tenant_id"],
            "caller",
            frozenset() if token == "forbidden" else frozenset({"ml:predict"}),
        )

    def dependency_ready(*args):
        if state["mode"] == "unavailable":
            from src.domain.ml import Unavailable

            raise Unavailable("synthetic-private-dependency")

    def route(tenant, endpoint):
        if tenant != spec["tenant_id"] or endpoint != spec["endpoint_id"]:
            raise NotFound("Endpoint not found")
        return Route(tenant, endpoint, 1, deployment.deployment_id, 3)

    @contextmanager
    def client(transport_timeout=False):
        with TestClient(upstream) as serving:

            def handle(request):
                if transport_timeout:
                    raise httpx.ReadTimeout("synthetic-private-host", request=request)
                response = serving.request(
                    request.method,
                    request.url.path,
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    response.status_code, content=response.content, headers=response.headers
                )

            app = create_app(
                components=SimpleNamespace(
                    database=SimpleNamespace(ready=dependency_ready),
                    deployments=SimpleNamespace(route=route, get=lambda *args: deployment),
                ),
                identity=SimpleNamespace(
                    resolve=resolve,
                    client=SimpleNamespace(admin=SimpleNamespace(command=dependency_ready)),
                ),
                resolver=SimpleNamespace(resolve=lambda _: "http://synthetic-runtime"),
                transport=httpx.MockTransport(handle),
            )
            with TestClient(app) as gateway:
                yield gateway

    try:
        yield client, state
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize("kind", ["mlp", "ebm"])
def test_actual_http_success_matches_published_contract(gateway_runtime, kind):
    from src.api.prediction_docs import PREDICT_EXAMPLES

    client, state = gateway_runtime
    state["mode"] = kind
    body = PREDICT_EXAMPLES[kind]["value"]
    # Both synthetic requests target the fixture's single assigned endpoint.
    body = {**body, "endpoint_id": "synthetic-quality"}
    with client() as gateway:
        response = gateway.post(
            "/api/v1/prediction/predict",
            json=body,
            headers={"Authorization": "Bearer valid", "X-Request-ID": "synthetic-correlation"},
        )
    assert response.status_code == 200
    parsed = GatewayPredictionResult.model_validate(response.json())
    assert parsed.provenance.input_schema_version == body["schema_version"]
    assert parsed.provenance.model_version == "7"
    assert response.headers["x-request-id"] == parsed.request_id == "synthetic-correlation"


@pytest.mark.parametrize(
    "token,status",
    [
        ("invalid", 401),
        ("expired", 401),
        ("revoked", 401),
        ("forbidden", 403),
        ("other", 404),
    ],
)
def test_http_auth_and_tenant_errors_match_contract(gateway_runtime, token, status):
    from src.api.prediction_docs import PREDICT_EXAMPLES, PredictionError

    client, state = gateway_runtime
    with client() as gateway:
        response = gateway.post(
            "/api/v1/prediction/predict",
            json=PREDICT_EXAMPLES["mlp"]["value"],
            headers={"Authorization": "Bearer " + token, "X-Request-ID": "synthetic-correlation"},
        )
    assert response.status_code == status
    assert PredictionError.model_validate(response.json()).request_id == "synthetic-correlation"
    assert state["calls"] == 0


def test_payload_limit_validation_and_unknown_endpoint(gateway_runtime):
    from src.api.prediction_docs import PREDICT_EXAMPLES, PayloadTooLarge, PredictionError

    client, _ = gateway_runtime
    with client() as gateway:
        headers = {"Authorization": "Bearer valid", "X-Request-ID": "synthetic-correlation"}
        too_large = gateway.post(
            "/api/v1/prediction/predict", content=b"x" * (8 * 1024 * 1024 + 1), headers=headers
        )
        assert too_large.status_code == 413
        PayloadTooLarge.model_validate(too_large.json())
        assert "request_id" not in too_large.json()
        assert too_large.headers["x-request-id"] == "synthetic-correlation"
        invalid = gateway.post(
            "/api/v1/prediction/predict", json={"secret": "do-not-echo"}, headers=headers
        )
        assert invalid.status_code == 422
        error = PredictionError.model_validate(invalid.json())
        assert error.request_id != invalid.headers["x-request-id"]
        assert "do-not-echo" not in invalid.text
        unknown = gateway.post(
            "/api/v1/prediction/predict",
            json={**PREDICT_EXAMPLES["mlp"]["value"], "endpoint_id": "missing"},
            headers=headers,
        )
        assert unknown.status_code == 404


def test_serving_deadline_and_busy_slot_reach_gateway(gateway_runtime):
    from src.api.prediction_docs import PREDICT_EXAMPLES, PredictionError

    client, state = gateway_runtime
    state["mode"] = "slow"
    with client() as gateway:
        for status, code in [(504, "PREDICTION_DEADLINE"), (429, "CAPACITY_EXCEEDED")]:
            response = gateway.post(
                "/api/v1/prediction/predict",
                json=PREDICT_EXAMPLES["mlp"]["value"],
                headers={"Authorization": "Bearer valid"},
            )
            assert response.status_code == status
            error = PredictionError.model_validate(response.json())
            assert error.code == code and error.retryable


@pytest.mark.parametrize("failure", ["malformed", "wrongtypes", "transport"])
def test_gateway_returns_safe_503_on_upstream_contract_failure(gateway_runtime, failure):
    from src.api.prediction_docs import PREDICT_EXAMPLES, PredictionError

    client, state = gateway_runtime
    state["mode"] = failure
    with client(transport_timeout=failure == "transport") as gateway:
        response = gateway.post(
            "/api/v1/prediction/predict",
            json=PREDICT_EXAMPLES["mlp"]["value"],
            headers={"Authorization": "Bearer valid"},
        )
    assert response.status_code == 503
    assert PredictionError.model_validate(response.json()).code == "DEPENDENCY_UNAVAILABLE"
    assert "synthetic-private" not in response.text


def test_gateway_health_contracts(gateway_runtime):
    client, state = gateway_runtime
    with client() as gateway:
        assert gateway.get("/health/live").json() == {"live": True}
        response = gateway.get("/health/ready")
        assert response.status_code == 200 and response.json() == {"ready": True}
        state["mode"] = "unavailable"
        response = gateway.get("/health/ready")
        assert response.status_code == 503 and response.json() == {"ready": False}
        assert "synthetic-private" not in response.text
