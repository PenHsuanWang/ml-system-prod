"""Real train/evaluate/approve/contract/serve plus prediction-only HTTP discovery."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from bootstrap import prediction_contract_cli
from bootstrap.dataset_build_wiring import build_worker
from bootstrap.ml_api import create_app as management_app
from bootstrap.ml_gateway import create_app as gateway_app
from bootstrap.ml_serving import create_app as serving_app
from bootstrap.ml_wiring import build_components
from bootstrap.prediction_contracts import extract_public_contract
from src.application.prediction.catalog import ContractPublicationService, EndpointDiscoveryService
from src.domain.ml import MLError, canonical_json
from src.infrastructure.persistence.dataset_build_jobs import SQLDatasetBuildJobs
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from src.infrastructure.persistence.prediction_catalog import SQLPredictionCatalog
from src.infrastructure.persistence.prediction_contract_schema import (
    public_contracts,
    usage_reviews,
)
from src.infrastructure.serving.materializer import BundleMaterializer, verify_bundle
from tests.ml.dataset_builder.conftest import published_builder_inputs as published_builder_inputs
from tests.ml.ebm.test_platform_training import ebm_intent, register
from tests.ml.test_api import Identity
from tests.ml.test_prediction_catalog import USAGE


@pytest.fixture
def builder_lot_count() -> int:
    # The evaluation policy requires at least two test lots after the grouped split.
    return 10


@pytest.mark.parametrize("profile", ["mlp", "ebm"])
@pytest.mark.parametrize("root_path", ["", "/inference"])
def test_prediction_only_user_discovers_and_invokes_verified_bundle(
    components,
    actor,
    intent,
    tmp_path,
    monkeypatch,
    request,
    profile,
    root_path,
):
    roles = {
        "builder": replace(
            actor,
            actor_id="builder",
            permissions=frozenset({"ml:dataset:manage", "ml:ingestion:operate"}),
        ),
        "trainer": replace(actor, actor_id="trainer", permissions=frozenset({"ml:train"})),
        "reviewer": replace(
            actor, actor_id="reviewer", permissions=frozenset({"ml:model:read", "ml:model:approve"})
        ),
        "deployer": replace(actor, actor_id="deployer", permissions=frozenset({"ml:deploy"})),
    }

    async def resolve(token):
        return roles[token]

    def auth(role):
        return {"Authorization": f"Bearer {role}"}

    runtime = components
    ingest = None
    if profile == "ebm":
        ingest = request.getfixturevalue(published_builder_inputs.__name__)
        policy = {
            "policy_version": "synthetic-discovery-v1",
            "max_anchor_loss": 100,
            "minimum_lot_correlation": -1,
            "minimum_lots": 2,
        }
        runtime = build_components(
            replace(components.settings, ebm_quality_policy_json=canonical_json(policy))
        )
    try:
        management = management_app(
            services=runtime.services,
            identity=SimpleNamespace(resolve=resolve),
            ingestion_service=ingest.ingress.service if ingest else None,
        )
        with TestClient(management) as manager:
            if ingest:
                intent = canonical_json(
                    _build_dataset(manager, runtime, tmp_path, roles["builder"])
                )
            trainer = auth("trainer")
            capability = manager.get("/api/v1/platform/capabilities", headers=trainer)
            links = {item["id"]: item["url"] for item in capability.json()["items"]}
            assert set(links) == {"dataset-read", "training"}
            spec = json.loads(intent)
            datasets = manager.get(links["dataset-read"], headers=trainer).json()["items"]
            assert spec["dataset"]["id"] in {item["dataset_id"] for item in datasets}
            recipes = manager.get(links["training"], headers=trainer).json()
            assert spec["recipe"] in [item["recipe"] for item in recipes]
            accepted = manager.post(
                "/api/v1/training/jobs",
                json={"spec": spec},
                headers={**trainer, "Idempotency-Key": "discover-real"},
            )
            assert accepted.status_code == 202, accepted.text
            command = SQLOutboxRepository(runtime.database).lease(1)[0].command
            runtime.supervisor().execute(command)
            final = manager.get(accepted.json()["status_url"], headers=trainer).json()
            assert final["state"] == "SUCCEEDED", final["error_code"]
            binding = runtime.registry.get_model(roles["reviewer"], final["model_binding_id"])
            evaluation_url = f"/api/v1/model_registry/bindings/{binding.binding_id}/evaluations"
            assert manager.get(evaluation_url, headers=trainer).status_code == 403
            evaluation = manager.get(evaluation_url, headers=auth("reviewer")).json()[0]
            assert evaluation["decision"] == "APPROVED_BY_POLICY"
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
        assert publication.publish(roles["reviewer"], binding.binding_id, usage) == 1
        # Retry/publication and a separate reader instance use identical durable contracts.
        assert publication.publish(roles["reviewer"], binding.binding_id, usage) == 1
        with TestClient(management) as manager:
            approval = manager.post(
                f"/api/v1/model_registry/bindings/{binding.binding_id}/approvals",
                json={"evaluation_id": evaluation["evaluation_id"], "environment": "dev"},
                headers=auth("reviewer"),
            )
            assert approval.status_code == 201
            body = {
                "endpoint_id": "synthetic-" + profile,
                "model_binding_id": binding.binding_id,
                "approval_id": approval.json()["approval_id"],
            }
            assert (
                manager.post("/api/v1/deployments", json=body, headers=auth("reviewer")).status_code
                == 403
            )
            deployed = manager.post("/api/v1/deployments", json=body, headers=auth("deployer"))
            assert deployed.status_code == 202
        deployment = runtime.deployments.get(roles["deployer"], deployed.json()["deployment_id"])
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
            with TestClient(gateway, root_path=root_path) as client:
                auth = {"Authorization": "Bearer owner"}
                capabilities = client.get(
                    root_path + "/api/v1/platform/capabilities", headers=auth
                ).json()
                assert {item["id"] for item in capabilities["items"]} == {
                    "prediction-discovery",
                    "prediction",
                }
                list_url = next(
                    item["url"]
                    for item in capabilities["items"]
                    if item["id"] == "prediction-discovery"
                )
                listed = client.get(list_url, headers=auth)
                assert listed.status_code == 200
                endpoint = listed.json()["items"][0]
                assert endpoint["detail_url"] == (
                    root_path + "/api/v1/prediction/endpoints/synthetic-" + profile
                )
                detail = client.get(endpoint["detail_url"], headers=auth)
                assert detail.status_code == 200
                resource = detail.json()
                assert resource["detail_url"] == endpoint["detail_url"]
                assert resource["predict_url"] == root_path + "/api/v1/prediction/predict"
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


def _build_dataset(manager, runtime, tmp_path, builder):
    headers = {"Authorization": "Bearer builder"}
    entries = manager.get("/api/v1/platform/capabilities", headers=headers).json()["items"]
    links = {entry["id"]: entry["url"] for entry in entries}
    snapshots = manager.get(links["snapshot-read"], headers=headers).json()["items"]
    builders = manager.get(links["dataset-build"], headers=headers).json()
    selected = builders[0]
    detail = manager.get(
        f"{links['dataset-build']}/{selected['builder_id']}/versions/{selected['builder_version']}",
        headers=headers,
    ).json()
    payload = detail["submit_example"]
    payload["inputs"] = [
        {
            "role": snapshot["source_id"].removeprefix("builder-"),
            "snapshot_id": snapshot["snapshot_id"],
            "manifest_digest": snapshot["manifest_digest"],
        }
        for snapshot in snapshots
    ]
    accepted = manager.post(
        "/api/v1/datasets/build-jobs",
        json=payload,
        headers={**headers, "Idempotency-Key": "wp7-build"},
    )
    assert accepted.status_code == 202, accepted.text
    worker = build_worker(
        runtime.settings,
        SQLDatasetBuildJobs(runtime.database),
        tmp_path / "raw-snapshots",
        lambda *_: builder,
    )
    job = accepted.json()
    assert worker.run_once() == job["job_id"]
    result = manager.get(job["result_url"], headers=headers)
    assert result.status_code == 200, result.text
    return ebm_intent(result.json()["dataset_id"], result.json()["version"])


@pytest.mark.parametrize("profile", ["mlp", "ebm"])
@pytest.mark.asyncio
async def test_cli_rejects_historical_code_and_publishes_new_matching_bundle(
    components, actor, intent, tmp_path, monkeypatch, profile
):
    historical_revision, current_revision = "a" * 40, "b" * 40
    settings = components.settings
    if profile == "ebm":
        from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

        register(SQLDatasetCatalog(components.database, settings.dataset_root), actor, tmp_path)
        settings = replace(
            settings,
            ebm_quality_policy_json=canonical_json(
                {
                    "policy_version": "synthetic-discovery-v1",
                    "max_anchor_loss": 100,
                    "minimum_lot_correlation": -1,
                    "minimum_lots": 2,
                }
            ),
        )
        intent = canonical_json(ebm_intent())
    current_settings = replace(settings, code_revision=current_revision)
    monkeypatch.setattr(prediction_contract_cli.MLSettings, "from_env", lambda: current_settings)

    # Only Identity transport is controlled; CLI composition, SQL and runtime verification are real.
    class Reviewer:
        async def resolve(self, token):
            assert token == "synthetic-reviewer-token"
            return actor

        def close(self):
            pass

    monkeypatch.setattr(prediction_contract_cli, "MongoActorResolver", lambda *args: Reviewer())
    monkeypatch.setenv("APP_MONGO_URI", "mongodb://unused-synthetic-identity")
    monkeypatch.setenv("APP_JWT_SECRET", "synthetic-test-key" * 3)
    token_file, usage_file = tmp_path / "reviewer.token", tmp_path / "usage.json"
    token_file.write_text("synthetic-reviewer-token")
    token_file.chmod(0o600)
    usage_file.write_text(canonical_json(USAGE))
    for revision in (historical_revision, current_revision):
        runtime = build_components(replace(settings, code_revision=revision))
        try:
            job = runtime.services.training.submit(actor, "revision-" + revision[0], intent)
            command = SQLOutboxRepository(runtime.database).lease(1)[0].command
            runtime.supervisor().execute(command)
            final = runtime.training.get(job.job_id)
            assert final.state == "SUCCEEDED", final.error_code
            binding = runtime.registry.get_model(actor, final.model_binding_id)
            materializer = BundleMaterializer(settings.artifact_root, settings.tracking_uri)
            directory = materializer.materialize(binding.artifact_ref, binding.manifest_digest)
            before = verify_bundle(directory, binding.manifest_digest)
            assert before["model"]["trusted_code_digest"] == revision
            if revision == historical_revision:
                with pytest.raises(MLError):
                    await prediction_contract_cli.publish(
                        binding.binding_id, usage_file, token_file
                    )
                with components.database.transaction() as connection:
                    assert connection.execute(select(public_contracts)).first() is None
                    assert connection.execute(select(usage_reviews)).first() is None
            else:
                assert (
                    await prediction_contract_cli.publish(
                        binding.binding_id, usage_file, token_file
                    )
                    == 1
                )
                assert (
                    await prediction_contract_cli.publish(
                        binding.binding_id, usage_file, token_file
                    )
                    == 1
                )
                with components.database.transaction() as connection:
                    assert (
                        connection.execute(select(public_contracts.c.binding_id)).scalar_one()
                        == binding.binding_id
                    )
            assert verify_bundle(directory, binding.manifest_digest) == before
        finally:
            runtime.database.close()
