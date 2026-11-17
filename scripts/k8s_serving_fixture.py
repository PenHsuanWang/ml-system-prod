"""Disposable-namespace operator used by verify_k8s_serving; never logs credentials."""

import json
import os
import secrets
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import httpx

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from src.domain.ml import Actor, canonical_json


def main():
    if os.environ.get("ML_SERVING_NAMESPACE") != "ml-k8s-serving-runtime":
        raise ValueError("Serving fixture is restricted to its disposable namespace")
    settings = MLSettings.from_env()
    settings = replace(
        settings,
        ebm_quality_policy_json=canonical_json(
            {
                "policy_version": "engineering-test-only-k8s-serving-v1",
                "max_anchor_loss": 100,
                "minimum_lot_correlation": -1,
                "minimum_lots": 2,
            }
        ),
    )
    components = build_components(settings)
    root = settings.execution_root / "serving-qualification"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixture = root / "fixture.json"
    mode = sys.argv[1]
    try:
        if mode == "seed":
            if fixture.exists():
                raise FileExistsError("Retain the existing qualification fixture")
            from bootstrap.ebm_cli import synthetic_snapshot
            from src.api.ml_schemas import EBMTrainingIntent, TrainingIntent
            from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
            from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository

            value = {
                "tenant": "serving-" + uuid4().hex[:12],
                "username": "serving-operator",
                "password": secrets.token_urlsafe(32),
                "models": [],
            }
            permissions = [
                "ml.train",
                "ml.model.read",
                "ml.model.approve",
                "ml.deploy",
                "ml.predict",
            ]
            command = [
                sys.executable,
                "manage.py",
                "create-admin",
                "--tenant-id",
                value["tenant"],
                "--username",
                value["username"],
                "--password-stdin",
            ]
            for permission in permissions:
                command += ["--permission", permission]
            subprocess.run(
                command,
                input=value["password"] + "\n",
                text=True,
                capture_output=True,
                check=True,
                timeout=45,
            )
            actor = Actor(
                value["tenant"],
                "qualification-reviewer",
                frozenset(
                    {
                        "ml:train",
                        "ml:model:read",
                        "ml:model:approve",
                        "ml:deploy",
                        "ml:dataset:manage",
                    }
                ),
            )
            catalog = SQLDatasetCatalog(components.database, settings.dataset_root)
            catalog.seed_builtin()
            reference = synthetic_snapshot(root / "ebm-input")
            source = Path(reference["snapshot_ref"]).parent
            catalog.register_ebm(
                actor, "serving-ebm", "v1", source / "metadata.json", source / "spectrograms.npz"
            )
            intents = [
                TrainingIntent(
                    recipe={"id": "nn_classifier", "version": "1.0.0"},
                    dataset={"id": "dataset-demo", "version": "2026-10-28-v1"},
                    training={"seed": seed},
                )
                for seed in (42, 43)
            ]
            intents.append(
                EBMTrainingIntent(
                    recipe={"id": "ebm_poc_replay_v1", "version": "1.0.0"},
                    dataset={"id": "serving-ebm", "version": "v1"},
                    model={"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
                    training={"epochs": 1, "batch_size": 6, "pair_sample_size": 32},
                )
            )
            outbox = SQLOutboxRepository(components.database)
            for intent in intents:
                job = components.services.training.submit(
                    actor, uuid4().hex, canonical_json(intent.model_dump())
                )
                item = next(item for item in outbox.lease(100) if item.command.job_id == job.job_id)
                outbox.published(item)
                components.supervisor().execute(item.command)
                final = components.training.get(job.job_id)
                if final.state != "SUCCEEDED":
                    raise ValueError("Qualified fixture training failed: " + final.state)
                evaluation = components.registry.list_evaluations(final.model_binding_id)[-1]
                approval = components.services.registry.approve(
                    actor, final.model_binding_id, evaluation.evaluation_id, settings.environment
                )
                value["models"].append(
                    {"binding_id": final.model_binding_id, "approval_id": approval.approval_id}
                )
            value["ebm_reference"] = reference
            with fixture.open("x") as stream:
                os.chmod(fixture, 0o600)
                json.dump(value, stream)
            print(json.dumps({"models_qualified": len(value["models"])}))
            return
        value = json.loads(fixture.read_text())
        actor = Actor(
            value["tenant"], "qualification-reviewer", frozenset({"ml:deploy", "ml:predict"})
        )
        if mode == "create":
            model = value["models"][int(sys.argv[2])]
            deployment = components.services.deployments.create(
                actor, "quality", model["binding_id"], model["approval_id"]
            )
            print(
                json.dumps(
                    {
                        "deployment_id": deployment.deployment_id,
                        "generation": deployment.generation,
                        "approval_id": deployment.approval_id,
                        "spec_digest": deployment.spec_digest,
                    }
                )
            )
        elif mode == "rollback":
            deployment = components.services.deployments.rollback(actor, sys.argv[2])
            print(
                json.dumps(
                    {"deployment_id": deployment.deployment_id, "generation": deployment.generation}
                )
            )
        elif mode == "status":
            deployment = components.deployments.get(actor, sys.argv[2])
            route = components.deployments.route(actor.tenant_id, "quality")
            print(
                json.dumps(
                    {
                        "state": deployment.state,
                        "released": deployment.resources_released,
                        "approval_id": deployment.approval_id,
                        "spec_digest": deployment.spec_digest,
                        "route": asdict(route),
                    }
                )
            )
        elif mode == "predict":
            body = {
                "endpoint_id": "quality",
                "instances": [{"features": {"sensor_a": 1.0, "sensor_b": 1.0}}],
            }
            if sys.argv[2] == "ebm":
                from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
                from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource

                snapshot = EBMFileDatasetSource(root / "ebm-input").load(value["ebm_reference"])
                body = {
                    "schema_version": "ebm-spectrogram-input-v1",
                    "endpoint_id": "quality",
                    "signal_order": list(EBM_SIGNAL_ORDER),
                    "pad_order": list(EBM_PAD_ORDER),
                    "instances": [{"features": snapshot.spectrograms[0].tolist()}],
                }
            with httpx.Client(timeout=20, trust_env=False) as client:
                login = client.post(
                    "http://ml-management:8000/api/v1/identity/login",
                    json={key: value[key] for key in ("username", "password")}
                    | {"tenant_id": value["tenant"]},
                )
                login.raise_for_status()
                response = client.post(
                    "http://ml-control:8001/api/v1/prediction/predict",
                    json=body,
                    headers={"Authorization": "Bearer " + login.json()["access_token"]},
                )
                print(
                    json.dumps(
                        {
                            "status": response.status_code,
                            "provenance": response.json().get("provenance"),
                        }
                    )
                )
        else:
            raise ValueError("Unknown fixture operation")
    finally:
        components.database.close()


if __name__ == "__main__":
    main()
