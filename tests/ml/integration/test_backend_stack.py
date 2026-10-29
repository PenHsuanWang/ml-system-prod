"""Real HTTP/Identity/queue/tracking/rollout acceptance against an isolated stack."""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest


async def wait_for_state(client, path, headers, expected, *, timeout=180):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        value = response.json()
        if value["state"] == expected:
            return value
        assert value["state"] not in {"FAILED", "CANCELLED"}, value
        await asyncio.sleep(0.25)
    pytest.fail("Backend did not reach the required durable state within its acceptance deadline")


async def wait_for_ready(client, *, timeout=90):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            response = await client.get("/health/ready")
            if response.status_code == 200 and response.json().get("ready") is True:
                return
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.5)
    pytest.fail("Required Backend service did not become ready")


async def provision(tenant, username, password):
    permissions = ("ml.train", "ml.cancel", "ml.model.read", "ml.model.approve",
                   "ml.deploy", "ml.predict", "ml.jobs.read_all")
    command = [sys.executable, "manage.py", "create-admin", "--tenant-id", tenant,
               "--username", username, "--password-stdin"]
    for permission in permissions:
        command.extend(["--permission", permission])
    result = await asyncio.to_thread(subprocess.run, command, input=password+"\n", text=True,
                                     capture_output=True, timeout=40, check=False)
    assert result.returncode == 0, "Explicit test-account provisioning failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_backend_train_approve_predict_rollout_rollback_and_revoke():
    management = os.environ.get("ML_TEST_MANAGEMENT_URL")
    prediction = os.environ.get("ML_TEST_PREDICTION_URL")
    if not management or not prediction:
        if os.environ.get("ML_REQUIRE_STACK_ACCEPTANCE") == "1":
            pytest.fail("Stack acceptance URLs are required")
        pytest.skip("Set isolated ML_TEST_MANAGEMENT_URL and ML_TEST_PREDICTION_URL")
    # Accounts and all backend data belong to a disposable acceptance stack.
    tenant, foreign = "accept-"+uuid4().hex[:16], "accept-"+uuid4().hex[:16]
    username, password = "acceptance_operator", secrets.token_urlsafe(24)
    async with httpx.AsyncClient(base_url=management, timeout=15) as manager, httpx.AsyncClient(
            base_url=prediction, timeout=15) as gateway:
        await wait_for_ready(manager)
        await wait_for_ready(gateway)
        await provision(tenant, username, password)
        await provision(foreign, username, password)

        async def login(scope):
            response = await manager.post("/api/v1/identity/login", json={
                "tenant_id": scope, "username": username, "password": password})
            assert response.status_code == 200
            return {"Authorization": "Bearer "+response.json()["access_token"]}

        owner, intruder = await login(tenant), await login(foreign)
        assert (await manager.get("/api/v1/identity/me", headers=owner)).json()["tenant_id"] == tenant
        assert (await manager.post("/api/v1/training/jobs", json={})).status_code == 401
        endpoint = "quality-"+uuid4().hex[:8]

        async def train_and_deploy(seed):
            body = {"spec": {"recipe": {"id": "nn_classifier", "version": "1.0.0"},
                             "dataset": {"id": "dataset-demo", "version": "2026-10-28-v1"}, "training": {"seed": seed}}}
            headers = {**owner, "Idempotency-Key": uuid4().hex}
            accepted = await manager.post("/api/v1/training/jobs", headers=headers, json=body)
            assert accepted.status_code == 202
            replay = await manager.post("/api/v1/training/jobs", headers=headers, json=body)
            assert replay.status_code == 202
            assert replay.json()["job_id"] == accepted.json()["job_id"]
            assert (await manager.get(accepted.json()["status_url"], headers=intruder)).status_code == 404
            completed = await wait_for_state(manager, accepted.json()["status_url"], owner, "SUCCEEDED")
            binding = completed["model_binding_id"]
            model = await manager.get(f"/api/v1/model_registry/bindings/{binding}", headers=owner)
            assert model.status_code == 200
            evaluation = (await manager.get(f"/api/v1/model_registry/bindings/{binding}/evaluations", headers=owner)).json()[0]
            assert evaluation["decision"] == "APPROVED_BY_POLICY"
            assert json.loads(evaluation["metrics_json"])["accuracy"] >= 0.75
            approved = await manager.post(f"/api/v1/model_registry/bindings/{binding}/approvals", headers=owner,
                                          json={"evaluation_id": evaluation["evaluation_id"], "environment": "dev"})
            assert approved.status_code == 201
            deployment = await manager.post("/api/v1/deployments", headers=owner, json={"endpoint_id": endpoint,
                                                                                        "model_binding_id": binding, "approval_id": approved.json()["approval_id"]})
            assert deployment.status_code == 202
            path = "/api/v1/deployments/"+deployment.json()["deployment_id"]
            return await wait_for_state(manager, path, owner, "ACTIVE"), model.json()

        async def predict(deployment, model):
            payload = {"endpoint_id": endpoint, "instances": [
                {"features": {"sensor_a": 1.0, "sensor_b": 1.0}}]}
            result = await gateway.post("/api/v1/prediction/predict", headers=owner, json=payload)
            assert result.status_code == 200
            assert result.json()["provenance"]["deployment_id"] == deployment["deployment_id"]
            assert result.json()["provenance"]["model_version"] == model["model_version"]
            assert result.json()["predictions"][0]["class_id"] == 1
            assert (await gateway.post("/api/v1/prediction/predict", headers=intruder, json=payload)).status_code == 404
            return payload

        first, first_model = await train_and_deploy(42)
        await predict(first, first_model)
        second, second_model = await train_and_deploy(43)
        assert second_model["model_version"] != first_model["model_version"]
        await predict(second, second_model)
        await wait_for_state(manager, "/api/v1/deployments/"+first["deployment_id"], owner, "RETIRED")
        rollback = await manager.post(f"/api/v1/deployments/{first['deployment_id']}/rollback", headers=owner)
        assert rollback.status_code == 202
        restored = await wait_for_state(manager, "/api/v1/deployments/"+rollback.json()["deployment_id"], owner, "ACTIVE")
        assert restored["generation"] > second["generation"]
        payload = await predict(restored, first_model)
        assert (await manager.post("/api/v1/identity/logout", headers=owner)).status_code == 204
        assert (await gateway.post("/api/v1/prediction/predict", headers=owner, json=payload)).status_code == 401
        assert (await manager.get("/api/v1/identity/me", headers=owner)).status_code == 401
