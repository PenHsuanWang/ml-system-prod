"""Real EBM child training, frozen-bundle evaluation and in-process HTTP Serving.

Identity is an explicit test double and the quality policy below is only an
engineering acceptance policy, not evidence of production model quality.
"""

import copy
import json
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest
from sqlalchemy import update

from bootstrap.ml_api import create_app
from bootstrap.ml_serving import create_app as serving_app
from bootstrap.ml_wiring import build_components
from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import Conflict, MLError, canonical_json
from src.domain.training.execution import ExecutionHandle
from src.infrastructure.persistence.ml_database import attempts
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn
from tests.ml.ebm.test_platform_training import ebm_intent, register
from tests.ml.test_api import Identity


pytestmark = pytest.mark.torch


@pytest.fixture
def qualified_components(components):
    policy = {"policy_version": "engineering-test-only-v1", "max_anchor_loss": 1_000_000.0,
              "minimum_lot_correlation": -1.0, "minimum_lots": 2}
    qualified = build_components(replace(
        components.settings, ebm_quality_policy_json=canonical_json(policy)))
    try:
        yield qualified
    finally:
        qualified.database.close()


def execution_files(root):
    """Include bytes and mtimes so a repeated fit in an existing directory fails."""
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def submit_ebm(components, actor, tmp_path, key):
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    registered, _, _ = register(catalog, actor, tmp_path)
    job = components.services.training.submit(actor, key, canonical_json(ebm_intent()))
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    return registered, job, command


def test_real_supervisor_evaluates_frozen_ebm_and_missing_policy_blocks_approval(
        components, actor, tmp_path):
    pytest.importorskip("torch")
    registered, job, command = submit_ebm(components, actor, tmp_path, "ebm-unqualified")
    supervisor = components.supervisor()
    supervisor.execute(command)
    completed = components.training.get(job.job_id)
    assert completed.state == "SUCCEEDED", completed.error_code
    binding = components.registry.get_model(actor, completed.model_binding_id)
    manifest = json.loads((Path(binding.artifact_ref) / "manifest.json").read_text())
    assert manifest["lineage"]["training_spec_digest"] == job.spec_digest
    assert len(manifest["lineage"]["trainer_spec_digest"]) == 64
    assert manifest["lineage"]["dataset_digest"] == registered.content_digest
    assert manifest["producer"] == {"job_id": job.job_id, "attempt_id": job.attempt_id}
    evaluation, = components.registry.list_evaluations(binding.binding_id)
    assert evaluation.manifest_digest == binding.manifest_digest
    assert evaluation.dataset_digest == registered.content_digest
    assert evaluation.evaluator_version == "ebm-neural-v1"
    assert evaluation.policy_version == "ebm-unqualified-v1"
    assert evaluation.decision == "REJECTED_BY_POLICY"
    metrics = json.loads(evaluation.metrics_json)
    assert metrics["quality_policy_configured"] is False
    assert metrics["samples"] == 4 and metrics["lots"] == 2
    assert "accuracy" not in metrics
    # A second evaluator opens the registered bundle independently of the child.
    repeated = components.supervisor().evaluator.evaluate(binding, job.spec_json)
    assert repeated.metrics_json == evaluation.metrics_json
    assert repeated.report_digest == evaluation.report_digest
    with pytest.raises(Conflict):
        components.services.registry.approve(
            actor, binding.binding_id, evaluation.evaluation_id, "dev")
    tags = json.loads((Path(binding.artifact_ref).parent / "tags.json").read_text())
    assert tags["compatibility_id"] == "ebm-torch-cpu-v1"
    assert tags["evaluator_version"] == "ebm-neural-v1"
    before = execution_files(components.settings.execution_root)
    supervisor.execute(command)
    assert execution_files(components.settings.execution_root) == before
    assert len(components.registry.list_models(actor)) == 1
    assert len(components.registry.list_evaluations(binding.binding_id)) == 1


@pytest.mark.asyncio
async def test_ebm_http_train_approve_deploy_serve_and_input_output_boundaries(
        qualified_components, actor, tmp_path, monkeypatch):
    pytest.importorskip("torch")
    components = qualified_components
    catalog = SQLDatasetCatalog(components.database, components.settings.dataset_root)
    register(catalog, actor, tmp_path)
    identity = Identity(actor)
    owner = {"Authorization": "Bearer owner", "Idempotency-Key": "ebm-http"}
    app = create_app(services=components.services, identity=identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://management") as manager:
        accepted = await manager.post("/api/v1/training/jobs", headers=owner,
                                      json={"spec": ebm_intent()})
        assert accepted.status_code == 202, accepted.text
        components.supervisor().execute(SQLOutboxRepository(
            components.database).lease(1)[0].command)
        completed = (await manager.get(accepted.json()["status_url"], headers=owner)).json()
        assert completed["state"] == "SUCCEEDED", completed
        binding_id = completed["model_binding_id"]
        evaluations = await manager.get(
            f"/api/v1/model_registry/bindings/{binding_id}/evaluations", headers=owner)
        evaluation, = evaluations.json()
        assert evaluation["evaluator_version"] == "ebm-neural-v1"
        assert evaluation["policy_version"] == "engineering-test-only-v1"
        assert evaluation["decision"] == "APPROVED_BY_POLICY", evaluation
        approved = await manager.post(
            f"/api/v1/model_registry/bindings/{binding_id}/approvals", headers=owner,
            json={"evaluation_id": evaluation["evaluation_id"], "environment": "dev"})
        assert approved.status_code == 201, approved.text
        created = await manager.post("/api/v1/deployments", headers=owner,
            json={"endpoint_id": "ebm-energy", "model_binding_id": binding_id,
                  "approval_id": approved.json()["approval_id"]})
        assert created.status_code == 202, created.text

    deployment = components.deployments.get(actor, created.json()["deployment_id"])
    spec_path = tmp_path / "serving.json"
    spec_path.write_text(deployment.spec_json)
    monkeypatch.setenv("ML_ARTIFACT_ROOT", str(components.settings.artifact_root))
    monkeypatch.setenv("ML_CODE_REVISION", components.settings.code_revision)
    monkeypatch.setenv("ML_IMAGE_DIGEST", components.settings.image_digest)
    values = np.random.default_rng(23).uniform(0, 3, (1, 7, 4, 9, 31)).astype(np.float32)
    payload = {"schema_version": "ebm-spectrogram-input-v1", "endpoint_id": "ebm-energy",
               "signal_order": list(EBM_SIGNAL_ORDER), "pad_order": list(EBM_PAD_ORDER),
               "instances": [{"features": values[0].tolist()}]}
    # A valid single tensor exceeds the management API's 64 KiB limit.
    encoded = json.dumps(payload).encode()
    assert len(encoded) > 65536
    binding = components.registry.get_model(actor, binding_id)
    runtime = EBMRuntime(Path(binding.artifact_ref), binding.manifest_digest, build_ebm_cnn,
                         expected_code_digest=components.settings.code_revision)
    try:
        expected = runtime.predict(values, signal_order=EBM_SIGNAL_ORDER, pad_order=EBM_PAD_ORDER)
    finally:
        runtime.close()

    serving = serving_app(spec_path=spec_path, expected_digest=deployment.spec_digest,
                          identity=identity)
    async with serving.router.lifespan_context(serving), httpx.AsyncClient(
            transport=httpx.ASGITransport(serving), base_url="http://serving") as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["ready"] is True
        assert ready.json()["manifest_digest"] == binding.manifest_digest
        response = await client.post("/api/v1/prediction/predict", headers={
            **owner, "Content-Type": "application/json"}, content=encoded)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["predictions"] == expected
        assert len(result["predictions"][0]["latent"]) == 112
        assert result["predictions"][0]["score_kind"] == "uncalibrated_latent_mean_plus_std"
        assert result["provenance"]["input_schema_version"] == "ebm-spectrogram-input-v1"
        assert result["provenance"]["model_version"] == binding.version
        assert result["provenance"]["deployment_id"] == deployment.deployment_id
        assert "tenant_id" not in result["provenance"]
        assert result["request_id"] == response.headers["x-request-id"]
        denied = await client.post("/api/v1/prediction/predict", json=payload,
                                    headers={"Authorization": "Bearer other"})
        assert denied.status_code == 404
        incorrect_schema = {"endpoint_id": "ebm-energy",
                            "instances": [{"features": {"sensor_a": 1.0, "sensor_b": 2.0}}]}
        invalid = [incorrect_schema, {**payload, "signal_order": list(reversed(EBM_SIGNAL_ORDER))},
                   {**payload, "pad_order": list(reversed(EBM_PAD_ORDER))},
                   {**payload, "instances": [{"features": values[0, :-1].tolist()}]}]
        for body in invalid:
            rejected = await client.post("/api/v1/prediction/predict", headers=owner, json=body)
            assert rejected.status_code == 422, rejected.text
        nonfinite = copy.deepcopy(payload)
        nonfinite["instances"][0]["features"][0][0][0][0] = float("nan")
        rejected = await client.post("/api/v1/prediction/predict", headers={
            **owner, "Content-Type": "application/json"}, content=json.dumps(nonfinite))
        assert rejected.status_code == 422, rejected.text

    class NonfiniteRuntime:
        mapping = {"latent_dim": 112}

        def predict(self, *args, **kwargs):
            return [{**expected[0], "mu": float("nan")}]

        def close(self):
            pass

    broken = serving_app(spec_path=spec_path, expected_digest=deployment.spec_digest,
        identity=identity, runtime_factory=lambda *args: NonfiniteRuntime())
    async with broken.router.lifespan_context(broken), httpx.AsyncClient(
            transport=httpx.ASGITransport(broken), base_url="http://serving") as client:
        rejected = await client.post("/api/v1/prediction/predict", headers=owner, json=payload)
        assert rejected.status_code == 400, rejected.text
        assert "predictions" not in rejected.json()


def test_ebm_evaluator_error_uses_ebm_version_and_recovers_without_refit(
        components, actor, tmp_path):
    pytest.importorskip("torch")
    registered, job, command = submit_ebm(components, actor, tmp_path, "ebm-evaluation-recovery")

    class BrokenEvaluator:
        def evaluate(self, binding, training_spec_json):
            raise MLError("sensitive-evaluator-detail")

    supervisor = components.supervisor()
    original = supervisor.evaluator
    supervisor.evaluator = BrokenEvaluator()
    with pytest.raises(MLError, match="sensitive-evaluator-detail"):
        supervisor.execute(command)
    assert components.training.get(job.job_id).state != "SUCCEEDED"
    binding, = components.registry.list_models(actor)
    failed, = components.registry.list_evaluations(binding.binding_id)
    assert failed.decision == "ERROR"
    assert failed.evaluator_version == "ebm-neural-v1"
    assert failed.policy_version == "ebm-unqualified-v1"
    assert failed.manifest_digest == binding.manifest_digest
    assert failed.dataset_digest == registered.content_digest
    assert json.loads(failed.metrics_json) == {"error_code": "EVALUATION_EXECUTION_FAILED"}
    with pytest.raises(Conflict):
        components.services.registry.approve(actor, binding.binding_id, failed.evaluation_id, "dev")
    before = execution_files(components.settings.execution_root)
    with components.database.transaction() as connection:
        connection.execute(update(attempts).where(
            attempts.c.id == job.attempt_id).values(heartbeat_at=0))
    claim, reference = components.training.recoverable(components.settings.stale_after)[0]
    supervisor.evaluator = original
    supervisor.monitor(claim, ExecutionHandle(**json.loads(reference)))
    assert components.training.get(job.job_id).state == "SUCCEEDED"
    assert execution_files(components.settings.execution_root) == before
    assert len(components.registry.list_models(actor)) == 1
    evaluations = components.registry.list_evaluations(binding.binding_id)
    assert {item.decision for item in evaluations} == {"ERROR", "REJECTED_BY_POLICY"}
    assert {item.evaluator_version for item in evaluations} == {"ebm-neural-v1"}
