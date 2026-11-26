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
        spec = (await client.get("openapi.json")).json()
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
