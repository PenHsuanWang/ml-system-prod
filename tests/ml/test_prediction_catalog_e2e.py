"""Real train/evaluate/approve/contract/serve plus prediction-only HTTP discovery."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from bootstrap.ml_gateway import create_app as gateway_app
from bootstrap.ml_serving import create_app as serving_app
from bootstrap.ml_wiring import build_components
from bootstrap.prediction_contracts import extract_public_contract
from src.application.prediction.catalog import ContractPublicationService, EndpointDiscoveryService
from src.domain.ml import MLError, canonical_json
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from src.infrastructure.persistence.prediction_catalog import SQLPredictionCatalog
from src.infrastructure.serving.materializer import BundleMaterializer
from tests.ml.ebm.test_platform_training import ebm_intent, register
from tests.ml.test_api import Identity
from tests.ml.test_prediction_catalog import USAGE


@pytest.mark.parametrize("profile", ["mlp", "ebm"])
def test_prediction_only_user_discovers_and_invokes_verified_bundle(
    components,
    actor,
    intent,
    tmp_path,
    monkeypatch,
    profile,
):
    runtime = components
    if profile == "ebm":
        from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

        register(
            SQLDatasetCatalog(components.database, components.settings.dataset_root),
            actor,
            tmp_path,
        )
        policy = {
            "policy_version": "synthetic-discovery-v1",
            "max_anchor_loss": 100,
            "minimum_lot_correlation": -1,
            "minimum_lots": 2,
        }
        runtime = build_components(
            replace(components.settings, ebm_quality_policy_json=canonical_json(policy))
        )
        intent = canonical_json(ebm_intent())
    try:
        job = runtime.services.training.submit(actor, "discover-real", intent)
        command = SQLOutboxRepository(runtime.database).lease(1)[0].command
        runtime.supervisor().execute(command)
        final = runtime.training.get(job.job_id)
        assert final.state == "SUCCEEDED", final.error_code
        binding = runtime.registry.get_model(actor, final.model_binding_id)
        evaluation = runtime.registry.list_evaluations(binding.binding_id)[0]
        assert evaluation.decision == "APPROVED_BY_POLICY"
        reader = replace(actor, actor_id="prediction-client", permissions=frozenset({"ml:predict"}))
        materializer = BundleMaterializer(
            runtime.settings.artifact_root, runtime.settings.tracking_uri
        )
        publication = ContractPublicationService(
            runtime.registry,
            runtime.registry,
            lambda b: extract_public_contract(
                b, materializer, expected_code_digest=runtime.settings.code_revision
            ),
        )
        usage = {**USAGE, "purpose": f"Exercise the synthetic {profile} inference contract."}
        assert publication.publish(actor, binding.binding_id, usage) == 1
        # Retry/publication and a separate reader instance use identical durable contracts.
        assert publication.publish(actor, binding.binding_id, usage) == 1
        approval = runtime.services.registry.approve(
            actor, binding.binding_id, evaluation.evaluation_id, "dev"
        )
        deployment = runtime.services.deployments.create(
            actor, "synthetic-" + profile, binding.binding_id, approval.approval_id
        )
        runtime.deployments.transition(deployment.deployment_id, "REQUESTED", "READY")
        runtime.deployments.activate(deployment)
        spec = tmp_path / "serving.json"
        spec.write_text(deployment.spec_json)
        monkeypatch.setenv("ML_ARTIFACT_ROOT", str(runtime.settings.artifact_root))
        monkeypatch.setenv("ML_CODE_REVISION", runtime.settings.code_revision)
        fixed = serving_app(
            spec_path=spec, expected_digest=deployment.spec_digest, identity=Identity(reader)
        )
        with TestClient(fixed) as serving:

            def no_artifact_download(*args, **kwargs):
                pytest.fail("Discovery and loaded prediction must not access artifact storage")

            # Simulates registry/artifact unavailability after the physical model loaded.
            monkeypatch.setattr(BundleMaterializer, "materialize", no_artifact_download)

            def handle(request):
                response = serving.request(
                    request.method,
                    request.url.path,
                    content=request.content,
                    headers=dict(request.headers),
                )
                return httpx.Response(
                    response.status_code, content=response.content, headers=response.headers
                )

            gateway = gateway_app(
                components=runtime,
                identity=Identity(reader),
                resolver=SimpleNamespace(resolve=lambda _: "http://synthetic-fixed-serving"),
                transport=httpx.MockTransport(handle),
                endpoint_discovery=EndpointDiscoveryService(
                    SQLPredictionCatalog(runtime.database), "s" * 48
                ),
            )
            with TestClient(gateway) as client:
                auth = {"Authorization": "Bearer owner"}
                listed = client.get("/api/v1/prediction/endpoints", headers=auth)
                assert listed.status_code == 200
                endpoint = listed.json()["items"][0]
                detail = client.get(endpoint["detail_url"], headers=auth)
                assert detail.status_code == 200
                resource = detail.json()
                response = client.post(
                    resource["predict_url"],
                    headers=auth,
                    json=resource["request_example"],
                )
                assert response.status_code == 200, response.text
                assert response.json()["provenance"]["model_version"] == binding.version
                assert response.json()["provenance"]["manifest_digest"] == binding.manifest_digest
                assert len(response.json()["predictions"]) == 1
                serialized = json.dumps(resource)
                assert binding.artifact_ref not in serialized
                assert "normalization.npz" not in serialized
        # Corruption cannot be re-qualified as an immutable verified contract.
        monkeypatch.undo()
        directory = materializer.materialize(binding.artifact_ref, binding.manifest_digest)
        (directory / "feature_schema.json").write_text("{}")
        with pytest.raises(MLError):
            publication.publish(actor, binding.binding_id, usage)
    finally:
        if runtime is not components:
            runtime.database.close()
