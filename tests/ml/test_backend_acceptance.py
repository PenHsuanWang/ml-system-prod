"""In-process HTTP acceptance with real SQL/PyTorch and explicit identity/launcher doubles.

This proves application behavior; live Identity, broker, sockets and containers
are covered separately by the opt-in stack acceptance test.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from bootstrap.ml_api import create_app
from bootstrap.ml_wiring import build_components
from src.application.deployments.service import DeploymentController
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from tests.ml.test_api import Identity


@pytest.mark.torch
@pytest.mark.asyncio
async def test_http_submit_train_approve_route_predict_restart_and_rollback(components, actor, intent, monkeypatch):
    pytest.importorskip("torch")

    class ScopedIdentity(Identity):
        async def resolve(self, token):
            if token == "readonly":
                return replace(actor, permissions=frozenset({"ml:model:read"}))
            return await super().resolve(token)

    identity = ScopedIdentity(actor)
    app = create_app(services=components.services, identity=identity)
    owner = {"Authorization": "Bearer owner", "Idempotency-Key": "backend-http"}
    other = {"Authorization": "Bearer other"}
    readonly = {"Authorization": "Bearer readonly"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://management") as manager:
        body = {"spec": json.loads(intent)}
        assert (await manager.post("/api/v1/training/jobs", json=body)).status_code == 401
        assert (await manager.post("/api/v1/training/jobs", json=body,
                                   headers={**readonly, "Idempotency-Key": "denied"})).status_code == 403
        submissions = await asyncio.gather(*[
            manager.post("/api/v1/training/jobs", headers=owner, json=body) for _ in range(12)])
        assert all(response.status_code == 202 for response in submissions)
        assert len({response.json()["job_id"] for response in submissions}) == 1
        job = submissions[0].json()
        assert (await manager.get(job["status_url"], headers=owner)).json()["state"] == "ACCEPTED"
        different = json.loads(intent)
        different["training"]["epochs"] = 1
        assert (await manager.post("/api/v1/training/jobs", headers=owner,
                                   json={"spec": different})).status_code == 409
        assert (await manager.get(job["status_url"], headers=other)).status_code == 404
        command = SQLOutboxRepository(components.database).lease(1)[0].command
        components.supervisor().execute(command)
        completed = (await manager.get(job["status_url"], headers=owner)).json()
        assert completed["state"] == "SUCCEEDED"
        binding_id = completed["model_binding_id"]
        model = await manager.get("/api/v1/model_registry/bindings/"+binding_id, headers=owner)
        assert model.status_code == 200 and "artifact_ref" not in model.json()
        assert (await manager.get("/api/v1/model_registry/bindings/"+binding_id, headers=other)).status_code == 404
        evaluations = (await manager.get(
            f"/api/v1/model_registry/bindings/{binding_id}/evaluations", headers=owner)).json()
        evaluation = evaluations[0]
        assert evaluation["decision"] == "APPROVED_BY_POLICY"
        approval_path = f"/api/v1/model_registry/bindings/{binding_id}/approvals"
        approval_body = {"evaluation_id": evaluation["evaluation_id"], "environment": "dev"}
        assert (await manager.post(approval_path, headers=readonly, json=approval_body)).status_code == 403
        approval = await manager.post(approval_path, headers=owner, json=approval_body)
        assert approval.status_code == 201
        created = await manager.post("/api/v1/deployments", headers=owner, json={"endpoint_id": "quality",
                                                                                 "model_binding_id": binding_id, "approval_id": approval.json()["approval_id"]})
        assert created.status_code == 202
        deployment_id = created.json()["deployment_id"]
        deployment = components.deployments.get(actor, deployment_id)
        spec_path = components.settings.execution_root/"accepted-serving.json"
        spec_path.write_text(deployment.spec_json)
        monkeypatch.setenv("ML_ARTIFACT_ROOT", str(components.settings.artifact_root))
        from bootstrap.ml_serving import create_app as serving_app
        serving = serving_app(spec_path=spec_path,
                              expected_digest=deployment.spec_digest, identity=identity)
        async with serving.router.lifespan_context(serving), httpx.AsyncClient(
                transport=httpx.ASGITransport(serving), base_url="http://serving") as predictor:
            observed = await predictor.get("/health/ready")
            assert observed.status_code == 200 and observed.json()["ready"] is True

            class ObservedServing:
                def prepare(self, target):
                    assert target.deployment_id == observed.json()["deployment_id"]

                def ready(self, target):
                    return observed.json()["ready"]

                def retire(self, target):
                    raise AssertionError("Active serving must not retire")

            DeploymentController(components.deployments, ObservedServing()).reconcile(deployment)
            assert (await manager.get("/api/v1/deployments/"+deployment_id, headers=owner)).json()["state"] == "ACTIVE"
            payload = {"endpoint_id": "quality", "instances": [
                {"features": {"sensor_a": 1.0, "sensor_b": 1.0}}]}
            response = await predictor.post("/api/v1/prediction/predict", headers=owner, json=payload)
            assert response.status_code == 200
            assert response.json()["predictions"][0]["class_id"] == 1
            assert response.json()["provenance"]["model_version"] == model.json()["model_version"]
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert (await predictor.post("/api/v1/prediction/predict", headers=other, json=payload)).status_code == 404
            assert (await predictor.post("/api/v1/prediction/predict", headers=readonly, json=payload)).status_code == 403
            assert (await predictor.post("/api/v1/prediction/predict", headers=owner,
                                         json={**payload, "instances": payload["instances"]*129})).status_code == 422
            # A fixed loaded model remains usable when its artifact source disappears.
            from pathlib import Path
            artifact = Path(components.registry.get_model(actor, binding_id).artifact_ref)
            unavailable = artifact.with_name("bundle-unavailable")
            artifact.rename(unavailable)
            try:
                assert (await predictor.post("/api/v1/prediction/predict", headers=owner, json=payload)).status_code == 200
            finally:
                unavailable.rename(artifact)
        restarted = build_components(components.settings)
        try:
            rebuilt = create_app(services=restarted.services, identity=identity)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(rebuilt), base_url="http://restarted") as client:
                assert (await client.get(job["status_url"], headers=owner)).json()["state"] == "SUCCEEDED"
                rollback = await client.post(f"/api/v1/deployments/{deployment_id}/rollback", headers=owner)
                assert rollback.status_code == 202
                assert rollback.json()["generation"] > deployment.generation
                assert rollback.json()["deployment_id"] != deployment_id
        finally:
            restarted.database.close()


@pytest.mark.torch
@pytest.mark.asyncio
async def test_real_quality_rejection_remains_visible_and_prevents_approval(components, actor, intent, tmp_path):
    pytest.importorskip("torch")
    from pathlib import Path
    from src.domain.ml import canonical_json
    from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
    value = json.loads(Path("src/infrastructure/datasets/dataset-demo.json").read_text())
    for index in value["splits"]["test"]:
        value["labels"][index] = 1-value["labels"][index]
    source = tmp_path/"rejected.json"
    source.write_text(canonical_json(value))
    operator = replace(actor, permissions=actor.permissions | {"ml:dataset:manage"})
    SQLDatasetCatalog(components.database, components.settings.dataset_root).register(
        operator, "quality-rejection", "v1", source)
    value = json.loads(intent)
    value["dataset"] = {"id": "quality-rejection", "version": "v1"}
    app = create_app(services=components.services, identity=Identity(actor))
    owner = {"Authorization": "Bearer owner", "Idempotency-Key": "quality-rejection"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://management") as client:
        accepted = await client.post("/api/v1/training/jobs", headers=owner, json={"spec": value})
        assert accepted.status_code == 202
        components.supervisor().execute(SQLOutboxRepository(
            components.database).lease(1)[0].command)
        result = (await client.get(accepted.json()["status_url"], headers=owner)).json()
        assert result["state"] == "SUCCEEDED"
        binding = result["model_binding_id"]
        evaluation = (await client.get(f"/api/v1/model_registry/bindings/{binding}/evaluations", headers=owner)).json()[0]
        assert evaluation["decision"] == "REJECTED_BY_POLICY"
        denied = await client.post(f"/api/v1/model_registry/bindings/{binding}/approvals", headers=owner,
                                   json={"evaluation_id": evaluation["evaluation_id"], "environment": "dev"})
        assert denied.status_code == 409


@pytest.mark.torch
def test_evaluation_error_is_durable_and_recovery_does_not_refit(components, actor, intent):
    pytest.importorskip("torch")
    from sqlalchemy import update
    from src.domain.ml import Conflict, MLError
    from src.domain.training.execution import ExecutionHandle
    from src.infrastructure.persistence.ml_database import attempts

    class BrokenEvaluator:
        def evaluate(self, binding, training_spec_json):
            raise MLError("test-only-sensitive-value")

    job = components.services.training.submit(actor, "evaluation-recovery", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    supervisor = components.supervisor()
    original = supervisor.evaluator
    supervisor.evaluator = BrokenEvaluator()
    with pytest.raises(MLError):
        supervisor.execute(command)
    assert components.training.get(job.job_id).state != "SUCCEEDED"
    binding = components.registry.list_models(actor)[0]
    failure = components.registry.list_evaluations(binding.binding_id)[0]
    assert failure.decision == "ERROR"
    assert "test-only-sensitive-value" not in failure.metrics_json
    with pytest.raises(Conflict):
        components.services.registry.approve(
            actor, binding.binding_id, failure.evaluation_id, "dev")
    before = set(components.settings.execution_root.iterdir())
    with components.database.transaction() as connection:
        connection.execute(update(attempts).where(
            attempts.c.id == job.attempt_id).values(heartbeat_at=0))
    claim, reference = components.training.recoverable(components.settings.stale_after)[0]
    supervisor.evaluator = original
    supervisor.monitor(claim, ExecutionHandle(**json.loads(reference)))
    assert components.training.get(job.job_id).state == "SUCCEEDED"
    assert set(components.settings.execution_root.iterdir()) == before
    assert {item.decision for item in components.registry.list_evaluations(binding.binding_id)} == {
        "ERROR", "APPROVED_BY_POLICY"}
