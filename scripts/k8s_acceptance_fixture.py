"""Synthetic inputs and authenticated HTTP operations for the isolated K8s gate."""

import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from src.domain.ml import Actor, canonical_json

NAMESPACE = "ml-k8s-acceptance"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def provision(tenant, name, permissions):
    account = {"tenant_id": tenant, "username": name, "password": secrets.token_urlsafe(32)}
    command = [
        sys.executable,
        "manage.py",
        "create-admin",
        "--tenant-id",
        tenant,
        "--username",
        name,
        "--password-stdin",
    ]
    for permission in permissions:
        command += ["--permission", permission]
    subprocess.run(
        command,
        input=account["password"] + "\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return account


def main():
    require(os.environ.get("ML_TRAINING_NAMESPACE") == NAMESPACE, "ISOLATED_NAMESPACE_REQUIRED")
    settings = MLSettings.from_env()
    policy = json.loads(settings.ebm_quality_policy_json or "{}")
    require(
        policy.get("policy_version") == "engineering-test-only-k8s-acceptance-v1",
        "EXPLICIT_ENGINEERING_POLICY_REQUIRED",
    )
    root = settings.execution_root / "governed-acceptance"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture = root / "fixture.json"
    components = build_components(settings)
    mode, *arguments = sys.argv[1:]
    try:
        if mode == "init":
            require(not fixture.exists(), "RETAIN_EXISTING_FIXTURE")
            from bootstrap.dataset_builder_cli import _service
            from src.domain.datasets.ebm_builder import EBMDatasetBuilderConfig
            from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
            from tests.ml.ebm.test_realdata_e2e import publish_inputs

            tenant = "acceptance-" + uuid4().hex[:12]
            permissions = [
                "ml.train",
                "ml.cancel",
                "ml.model.read",
                "ml.model.approve",
                "ml.deploy",
                "ml.predict",
                "ml.jobs.read_all",
            ]
            value = {
                "reviewer": provision(tenant, "reviewer", permissions),
                "viewer": provision(tenant, "viewer", ["ml.model.read", "ml.predict"]),
                "foreign": provision("foreign-" + uuid4().hex[:12], "foreign", permissions),
            }
            actor = Actor(
                tenant,
                "synthetic-input-operator",
                frozenset(
                    {"ml:dataset:manage", "ml:source:manage", "ml:ingest", "ml:ingestion:operate"}
                ),
            )
            catalog = SQLDatasetCatalog(components.database, settings.dataset_root)
            catalog.seed_builtin()
            rejected = json.loads(Path("src/infrastructure/datasets/dataset-demo.json").read_text())
            for index in rejected["splits"]["test"]:
                rejected["labels"][index] = 1 - rejected["labels"][index]
            path = root / "rejected.json"
            path.write_text(canonical_json(rejected))
            catalog.register(actor, "acceptance-rejected", "v1", path)
            references = publish_inputs(components, actor, root / "raw-inputs")
            service, database = _service(settings, root / "raw-inputs/raw")
            try:
                result = service.build(
                    actor,
                    "acceptance-ebm",
                    "v1",
                    references,
                    EBMDatasetBuilderConfig("recipe-a", 1, split_seed=17),
                )
            finally:
                database.close()
            receipt = Path(result.receipt_ref).read_bytes()
            require(
                hashlib.sha256(receipt).hexdigest() == result.receipt_digest,
                "BUILDER_RECEIPT_DIGEST",
            )
            value["ebm_reference"] = {
                "snapshot_ref": result.dataset.snapshot_ref,
                "content_digest": result.dataset.content_digest,
            }
            value["dataset_evidence"] = {
                "content_digest": result.dataset.content_digest,
                "builder_receipt_digest": result.receipt_digest,
                "raw_inputs": [reference.to_dict() for reference in references],
            }
            with fixture.open("x") as stream:
                os.chmod(fixture, 0o600)
                json.dump(value, stream)
            print(json.dumps({"fixture_ready": True, "dataset": value["dataset_evidence"]}))
            return

        value = json.loads(fixture.read_text())
        account = value["reviewer"]
        actor = Actor(
            account["tenant_id"], "evidence-reader", frozenset({"ml:deploy", "ml:model:read"})
        )
        if mode == "status":
            from src.infrastructure.execution.training_records import read

            job = components.training.get(arguments[0])
            attempts = components.training.attempts(job.job_id)
            attempt = attempts[0] if attempts else None
            handle = (
                json.loads(attempt.execution_ref) if attempt and attempt.execution_ref else None
            )
            stopped = Path(handle["directory"]) / "stopped.json" if handle else None
            receipt = read(stopped) if stopped and stopped.exists() else {}
            report = {
                "job_id": job.job_id,
                "state": job.state,
                "binding_id": job.model_binding_id,
                "attempt_id": job.attempt_id,
                "attempt_count": len(attempts),
                "code_revision": json.loads(job.spec_json)["code_revision"],
                "image_digest": json.loads(job.spec_json)["training_image_digest"],
                "execution_id": handle["execution_id"] if handle else None,
                "stop_confirmed": receipt.get("confirmed", False),
            }
            # Execution details are not part of the public Management job DTO.
            print(json.dumps(report))
            return
        if mode == "route":
            deployment = components.deployments.get(actor, arguments[0])
            route = components.deployments.route(actor.tenant_id, deployment.endpoint_id)
            print(
                json.dumps(
                    {
                        "state": deployment.state,
                        "released": deployment.resources_released,
                        "spec_digest": deployment.spec_digest,
                        "approval_id": deployment.approval_id,
                        "route": asdict(route),
                    }
                )
            )
            return
        if mode == "lineage":
            from mlflow import MlflowClient

            from src.infrastructure.serving.materializer import BundleMaterializer, verify_bundle

            binding = components.registry.get_model(actor, arguments[0])
            client = MlflowClient(
                tracking_uri=settings.tracking_uri, registry_uri=settings.tracking_uri
            )
            run = client.get_run(binding.run_id)
            version = client.get_model_version(binding.registered_name, binding.version)
            bundle = BundleMaterializer(settings.artifact_root, settings.tracking_uri).materialize(
                binding.artifact_ref, binding.manifest_digest
            )
            manifest = verify_bundle(bundle, binding.manifest_digest)
            require(
                run.info.status == "FINISHED" and version.run_id == binding.run_id,
                "TRACKER_LINEAGE_MISMATCH",
            )
            require(
                version.tags["manifest_digest"] == binding.manifest_digest,
                "TRACKER_MANIFEST_MISMATCH",
            )
            require(
                manifest["lineage"]["dataset_digest"] == value["ebm_reference"]["content_digest"],
                "BUNDLE_DATASET_MISMATCH",
            )
            require(
                run.data.tags["dataset_digest"] == value["ebm_reference"]["content_digest"],
                "TRACKER_DATASET_MISMATCH",
            )
            print(
                json.dumps(
                    {
                        "binding_id": binding.binding_id,
                        "run_id": binding.run_id,
                        "version": binding.version,
                        "manifest_digest": binding.manifest_digest,
                        "producer_job_id": manifest["producer"]["job_id"],
                        "dataset": value["dataset_evidence"],
                    }
                )
            )
            return

        account_name = arguments.pop(0) if mode == "request-as" else "reviewer"
        if mode == "request-as":
            mode = "request"
        with httpx.Client(
            base_url="http://ml-management:8000", timeout=30, trust_env=False
        ) as manager:
            login = manager.post("/api/v1/identity/login", json=value[account_name])
            require(login.status_code == 200, "LOGIN_FAILED")
            manager.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            if mode == "submit":
                from src.api.ml_schemas import EBMTrainingIntent, TrainingIntent

                kind, key = arguments[:2]
                if kind == "ebm":
                    intent = EBMTrainingIntent(
                        recipe={"id": "ebm_poc_replay_v1", "version": "1.0.0"},
                        dataset={"id": "acceptance-ebm", "version": "v1"},
                        model={"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
                        training={"epochs": 1, "batch_size": 6, "pair_sample_size": 16},
                    )
                else:
                    intent = TrainingIntent(
                        recipe={"id": "nn_classifier", "version": "1.0.0"},
                        dataset=(
                            {"id": "acceptance-rejected", "version": "v1"}
                            if kind == "rejected"
                            else {"id": "dataset-demo", "version": "2026-10-28-v1"}
                        ),
                        training={"seed": int(arguments[2]) if len(arguments) > 2 else 42},
                    )
                response = manager.post(
                    "/api/v1/training/jobs",
                    json={"spec": intent.model_dump()},
                    headers={"Idempotency-Key": key},
                )
            elif mode == "request":
                method, path, body = arguments
                require(path.startswith("/api/v1/"), "SCOPED_API_REQUIRED")
                response = manager.request(
                    method,
                    path,
                    json=json.loads(body),
                    headers={"Idempotency-Key": "negative-" + uuid4().hex},
                )
            elif mode == "predict":
                endpoint, kind = arguments
                body = {
                    "endpoint_id": endpoint,
                    "instances": [{"features": {"sensor_a": 1.0, "sensor_b": 1.0}}],
                }
                if kind == "ebm":
                    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource

                    snapshot = EBMFileDatasetSource(settings.dataset_root).load(
                        value["ebm_reference"]
                    )
                    body = {
                        "schema_version": "ebm-spectrogram-input-v1",
                        "endpoint_id": endpoint,
                        "signal_order": list(snapshot.metadata.signal_order),
                        "pad_order": list(snapshot.metadata.pad_order),
                        "instances": [{"features": snapshot.spectrograms[0].tolist()}],
                    }
                response = manager.post(
                    "http://ml-gateway:8001/api/v1/prediction/predict", json=body
                )
                if response.status_code == 200:
                    prediction = response.json()
                    if kind == "ebm":
                        output = prediction["predictions"][0]
                        require(
                            len(output["latent"]) == 112
                            and all(
                                math.isfinite(n)
                                for n in [
                                    output["mu"],
                                    output["sigma"],
                                    output["raw_nn_score"],
                                    *output["latent"],
                                ]
                            ),
                            "EBM_OUTPUT_CONTRACT",
                        )
                    print(
                        json.dumps(
                            {"status": 200, "body": {"provenance": prediction["provenance"]}}
                        )
                    )
                    return
            else:
                raise ValueError("UNKNOWN_OPERATOR_OPERATION")
            # HTTP DTOs contain no credential or owner-token fields.
            body = response.json()
            if response.status_code >= 400:
                body = {"code": body.get("code", "HTTP_ERROR")}
            print(json.dumps({"status": response.status_code, "body": body}))
    finally:
        components.database.close()


if __name__ == "__main__":
    main()
