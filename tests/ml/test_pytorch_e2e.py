import json
import threading
import time

import httpx
import pytest

from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


@pytest.mark.torch
@pytest.mark.asyncio
async def test_real_subprocess_train_evaluate_approve_deploy_predict(components, actor, intent, monkeypatch):
    pytest.importorskip("torch")
    job = components.services.training.submit(actor, "real-train", intent)
    item = SQLOutboxRepository(components.database).lease(1)[0]
    supervisor = components.supervisor()
    supervisor.execute(item.command)
    final = components.training.get(job.job_id)
    assert final.state == "SUCCEEDED", final.error_code
    binding = components.registry.get_model(actor, final.model_binding_id)
    evaluation = components.registry.list_evaluations(binding.binding_id)[0]
    assert evaluation.decision == "APPROVED_BY_POLICY"
    assert json.loads(evaluation.metrics_json)["accuracy"] >= 0.75
    approval = components.services.registry.approve(
        actor, binding.binding_id, evaluation.evaluation_id, "dev")
    deployment = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    spec = components.settings.execution_root/"serving-spec.json"
    spec.write_text(deployment.spec_json)
    from bootstrap.ml_serving import create_app
    from tests.ml.test_api import Identity
    monkeypatch.setenv("ML_ARTIFACT_ROOT", str(components.settings.artifact_root))
    app = create_app(spec_path=spec, expected_digest=deployment.spec_digest,
                     identity=Identity(actor))
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/health/ready")).json()["deployment_id"] == deployment.deployment_id
        response = await client.post("/api/v1/prediction/predict", headers={"Authorization": "Bearer owner"},
                                     json={"endpoint_id": "quality", "instances": [{"features": {"sensor_a": 1.0, "sensor_b": 1.0}}]})
        assert response.status_code == 200, response.text
        assert response.json()["predictions"][0]["class_id"] == 1
        assert response.json()["provenance"]["model_version"] == binding.version
        assert (await client.post("/api/v1/prediction/predict", headers={"Authorization": "Bearer other"},
                                  json={"endpoint_id": "quality", "instances": [{"features": {"sensor_a": 1.0, "sensor_b": 1.0}}]})).status_code == 404
    # Lost ACK redelivery reads the terminal job and never starts a second fit.
    before = set(components.settings.execution_root.iterdir())
    supervisor.execute(item.command)
    assert set(components.settings.execution_root.iterdir()) == before


@pytest.mark.torch
def test_cancel_terminates_physical_training_process(components, actor, intent):
    pytest.importorskip("torch")
    value = json.loads(intent)
    value["training"]["epochs"] = 100
    job = components.services.training.submit(actor, "cancel-real", json.dumps(value))
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    supervisor = components.supervisor()
    errors = []

    def run():
        try:
            supervisor.execute(command)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        if components.training.attempts(job.job_id)[0].execution_ref:
            break
        time.sleep(0.02)
    components.services.training.cancel(actor, job.job_id)
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert not errors
    assert components.training.get(job.job_id).state == "CANCELLED"
    attempt = components.training.attempts(job.job_id)[0]
    from src.domain.training.execution import ExecutionHandle
    handle = ExecutionHandle(**json.loads(attempt.execution_ref))
    assert supervisor.executor.inspect(handle).state == "STOPPED"
