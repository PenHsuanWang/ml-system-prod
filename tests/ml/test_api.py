import json
from dataclasses import replace

import httpx
import pytest

from bootstrap.ml_api import create_app


class Identity:
    def __init__(self, actor):
        self.actor = actor

    async def resolve(self, token):
        return self.actor if token == "owner" else replace(self.actor, tenant_id="other")


@pytest.mark.asyncio
async def test_scoped_api_idempotency_and_safe_attempt_history(components, actor, intent):
    app = create_app(services=components.services, identity=Identity(actor))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        body = {"spec": json.loads(intent)}
        assert (await client.post("/api/v1/training/jobs", json=body)).status_code == 401
        headers = {"Authorization": "Bearer owner", "Idempotency-Key": "api-one"}
        first = await client.post("/api/v1/training/jobs", json=body, headers=headers)
        assert first.status_code == 202, first.text
        repeated = await client.post("/api/v1/training/jobs", json=body, headers=headers)
        assert repeated.json()["job_id"] == first.json()["job_id"]
        url = first.json()["status_url"]
        assert (await client.get(url, headers={"Authorization": "Bearer other"})).status_code == 404
        attempts = await client.get(url+"/attempts", headers=headers)
        assert attempts.status_code == 200
        assert "owner_token" not in attempts.text and "execution_ref" not in attempts.text
        assert (await client.post(url+"/cancel", headers=headers)).json()["state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_invalid_input_does_not_echo_secrets_or_accept_code(components, actor, intent):
    app = create_app(services=components.services, identity=Identity(actor))
    body = {"spec": json.loads(intent), "shell": "DO-NOT-ECHO-THIS"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/v1/training/jobs", json=body,
                                     headers={"Authorization": "Bearer owner", "Idempotency-Key": "api-one"})
        assert response.status_code == 422
        assert "DO-NOT-ECHO-THIS" not in response.text
        oversized = await client.post("/api/v1/training/jobs", content=b"x"*65537,
                                      headers={"Authorization": "Bearer owner", "Idempotency-Key": "large"})
        assert oversized.status_code == 413
