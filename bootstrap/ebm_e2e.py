"""Engineering verification against existing isolated runtime roles.

This harness never starts a supervisor or changes deployment state. Management,
Outbox relay, Taskiq worker, MLflow, Controller and Gateway must already run.
The only automatic approval is explicitly restricted to a disposable dev test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from bootstrap.dataset_builder_cli import _service
from bootstrap.ml_wiring import Components
from src.domain.datasets.ebm_builder import EBMDatasetBuilderConfig, EBMRawSnapshotReference
from src.domain.ml import Actor
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.serving.materializer import BundleMaterializer, verify_bundle


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


async def wait_state(client: httpx.AsyncClient, path: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        response = await client.get(path)
        require(response.status_code == 200, "STATE_READ_FAILED")
        value: dict[str, Any] = response.json()
        if value["state"] == state:
            return value
        require(value["state"] not in {"FAILED", "CANCELLED"}, "LIFECYCLE_FAILED")
        await asyncio.sleep(0.25)
    raise TimeoutError("LIFECYCLE_DEADLINE")


async def verify_engineering_lifecycle(
    components: Components,
    actor: Actor,
    references: tuple[EBMRawSnapshotReference, ...],
    raw_root: Path,
    manager: httpx.AsyncClient,
    gateway: httpx.AsyncClient,
    report: dict[str, Any],
    *,
    dataset_id: str,
    endpoint: str,
) -> None:
    """Run only on disposable dev resources with an authorized test reviewer.

    Clients carry an existing authenticated identity. Reports contain only an
    allowlist of provenance, never request headers, source records or URLs.
    """
    require(components.settings.environment == "dev", "ENGINEERING_REQUIRES_DEV")
    require(components.settings.tracker_backend == "mlflow", "REAL_MLFLOW_REQUIRED")
    policy = json.loads(components.settings.ebm_quality_policy_json or "{}")
    require(
        policy.get("policy_version", "").startswith("engineering-test-only"),
        "EXPLICIT_TEST_POLICY_REQUIRED",
    )
    actor.require("ml:model:approve")
    service, database = _service(components.settings, raw_root)
    try:
        result = service.build(
            actor,
            dataset_id,
            "v1",
            references,
            EBMDatasetBuilderConfig("recipe-a", 1, split_seed=17),
        )
    finally:
        database.close()
    report["inputs"] = [reference.to_dict() for reference in references]
    receipt_bytes = Path(result.receipt_ref).read_bytes()
    require(
        hashlib.sha256(receipt_bytes).hexdigest() == result.receipt_digest,
        "BUILDER_RECEIPT_MISMATCH",
    )
    report["dataset"] = {
        "id": dataset_id,
        "version": "v1",
        "content_digest": result.dataset.content_digest,
        "builder_receipt_digest": result.receipt_digest,
        "builder_receipt": json.loads(receipt_bytes),
    }
    snapshot = EBMFileDatasetSource(components.settings.dataset_root).load(
        {
            "snapshot_ref": result.dataset.snapshot_ref,
            "content_digest": result.dataset.content_digest,
        }
    )
    intent = {
        "recipe": {"id": "ebm_poc_replay_v1", "version": "1.0.0"},
        "dataset": {"id": dataset_id, "version": "v1"},
        "model": {"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
        "training": {"epochs": 1, "batch_size": 6, "pair_sample_size": 16},
    }
    accepted = await manager.post(
        "/api/v1/training/jobs", json={"spec": intent}, headers={"Idempotency-Key": dataset_id}
    )
    require(accepted.status_code == 202, "TRAINING_SUBMISSION_FAILED")
    completed = await wait_state(manager, accepted.json()["status_url"], "SUCCEEDED")
    job = components.services.training.get(actor, completed["job_id"])
    spec = json.loads(job.spec_json)
    require(
        spec["dataset"]["content_digest"] == result.dataset.content_digest, "JOB_DATASET_MISMATCH"
    )
    require(
        spec["evaluation_policy"].startswith("engineering-test-only"),
        "EXPLICIT_TEST_POLICY_REQUIRED",
    )
    report["recipe"] = spec["recipe"]
    report["code_revision"] = spec["code_revision"]
    report["training"] = {
        "job_id": job.job_id,
        "state": job.state,
        "attempt_id": job.attempt_id,
        "spec_digest": job.spec_digest,
    }
    if job.model_binding_id is None:
        raise ValueError("MISSING_MODEL_BINDING")
    binding = components.registry.get_model(actor, job.model_binding_id)
    require(binding.attempt_id == job.attempt_id, "MODEL_JOB_MISMATCH")
    from mlflow import MlflowClient

    client = MlflowClient(
        tracking_uri=components.settings.tracking_uri, registry_uri=components.settings.tracking_uri
    )
    run = client.get_run(binding.run_id)
    versions = client.search_model_versions(f"name = '{binding.registered_name}'")
    (version,) = [item for item in versions if str(item.version) == binding.version]
    require(run.info.status == "FINISHED", "MLFLOW_RUN_NOT_FINISHED")
    require(run.data.tags["training_job_id"] == job.job_id, "MLFLOW_JOB_MISMATCH")
    require(
        run.data.tags["dataset_digest"] == result.dataset.content_digest, "MLFLOW_DATASET_MISMATCH"
    )
    require(version.run_id == binding.run_id, "MLFLOW_VERSION_RUN_MISMATCH")
    require(version.tags["manifest_digest"] == binding.manifest_digest, "MLFLOW_MANIFEST_MISMATCH")
    bundle = BundleMaterializer(
        components.settings.artifact_root, components.settings.tracking_uri
    ).materialize(binding.artifact_ref, binding.manifest_digest)
    manifest = verify_bundle(bundle, binding.manifest_digest)
    require(
        manifest["lineage"]["dataset_digest"] == result.dataset.content_digest,
        "BUNDLE_DATASET_MISMATCH",
    )
    require(manifest["producer"]["job_id"] == job.job_id, "BUNDLE_JOB_MISMATCH")
    report["model"] = {
        "binding_id": binding.binding_id,
        "run_id": binding.run_id,
        "registered_name": binding.registered_name,
        "version": binding.version,
        "manifest_digest": binding.manifest_digest,
        "dataset_digest": manifest["lineage"]["dataset_digest"],
    }
    (evaluation,) = components.registry.list_evaluations(binding.binding_id)
    require(evaluation.manifest_digest == binding.manifest_digest, "EVALUATION_MODEL_MISMATCH")
    require(
        evaluation.dataset_digest == result.dataset.content_digest, "EVALUATION_DATASET_MISMATCH"
    )
    report["evaluation"] = asdict(evaluation)
    require(evaluation.decision == "APPROVED_BY_POLICY", "ENGINEERING_POLICY_REJECTED")
    denied = await manager.post(
        "/api/v1/deployments",
        json={
            "endpoint_id": endpoint,
            "model_binding_id": binding.binding_id,
            "approval_id": "missing-approval",
        },
    )
    require(denied.status_code in {404, 409}, "DEPLOYMENT_WITHOUT_APPROVAL_ALLOWED")
    approved = await manager.post(
        f"/api/v1/model_registry/bindings/{binding.binding_id}/approvals",
        json={"evaluation_id": evaluation.evaluation_id, "environment": "dev"},
    )
    require(approved.status_code == 201, "TEST_REVIEWER_APPROVAL_FAILED")
    report["approval"] = {
        "approval_id": approved.json()["approval_id"],
        "kind": "authorized-engineering-test-reviewer",
    }
    created = await manager.post(
        "/api/v1/deployments",
        json={
            "endpoint_id": endpoint,
            "model_binding_id": binding.binding_id,
            "approval_id": approved.json()["approval_id"],
        },
    )
    require(created.status_code == 202, "DEPLOYMENT_SUBMISSION_FAILED")
    active = await wait_state(
        manager, "/api/v1/deployments/" + created.json()["deployment_id"], "ACTIVE"
    )
    route = components.deployments.route(actor.tenant_id, endpoint)
    require(route.deployment_id == active["deployment_id"], "ACTIVE_ROUTE_MISMATCH")
    require(route.generation == active["generation"], "ROUTE_GENERATION_MISMATCH")
    report["deployment"] = {
        **active,
        "route_revision": route.revision,
        "route_deployment_id": route.deployment_id,
    }
    payload = {
        "schema_version": "ebm-spectrogram-input-v1",
        "endpoint_id": endpoint,
        "signal_order": list(snapshot.metadata.signal_order),
        "pad_order": list(snapshot.metadata.pad_order),
        "instances": [{"features": snapshot.spectrograms[0].tolist()}],
    }
    predicted = await gateway.post("/api/v1/prediction/predict", json=payload)
    require(predicted.status_code == 200, "GATEWAY_PREDICTION_FAILED")
    value = predicted.json()
    provenance = value["provenance"]
    require(
        provenance["deployment_id"] == active["deployment_id"], "PREDICTION_DEPLOYMENT_MISMATCH"
    )
    require(provenance["model_version"] == binding.version, "PREDICTION_MODEL_MISMATCH")
    require(provenance["input_schema_version"] == "ebm-spectrogram-input-v1", "PREDICTION_SCHEMA")
    (output,) = value["predictions"]
    require(len(output["latent"]) == 112, "PREDICTION_LATENT_SHAPE")
    require(
        all(
            math.isfinite(number)
            for number in [output["mu"], output["sigma"], output["raw_nn_score"], *output["latent"]]
        ),
        "NONFINITE_PREDICTION",
    )
    require(output["score_kind"] == "uncalibrated_latent_mean_plus_std", "PREDICTION_SEMANTICS")
    report["prediction"] = {
        "schema": "ebm-neural-output-v1",
        "provenance": provenance,
        "output": output,
    }
    require(len(components.registry.list_models(actor)) == 1, "PREDICTION_CREATED_MODEL")
    require(components.training.get(job.job_id).state == "SUCCEEDED", "JOB_NOT_DURABLE")
