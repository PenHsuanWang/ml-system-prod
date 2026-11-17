"""Private operator commands restricted to the disposable Training namespace."""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from bootstrap.ml_config import MLSettings
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_training import SQLTrainingRepository


def main():
    if os.environ.get("ML_TRAINING_NAMESPACE") != "ml-k8s-training-runtime":
        raise ValueError("Training fixture is restricted to its disposable namespace")
    mode = sys.argv[1]
    if mode == "transport":
        from bootstrap.deployment_entrypoint import load_runtime_environment

        os.environ.update(load_runtime_environment(os.environ))
    settings = MLSettings.from_env()
    root = settings.execution_root / "training-qualification"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    fixture = root / "fixture.json"
    mode = sys.argv[1]
    if mode == "init":
        if fixture.exists():
            raise FileExistsError("Retain the existing fixture")
        from bootstrap.ebm_cli import synthetic_snapshot
        from src.domain.ml import Actor
        from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog

        value = {
            "tenant_id": "training-" + uuid4().hex[:12],
            "username": "training-operator",
            "password": secrets.token_urlsafe(32),
        }
        command = [
            sys.executable,
            "manage.py",
            "create-admin",
            "--tenant-id",
            value["tenant_id"],
            "--username",
            value["username"],
            "--password-stdin",
        ]
        for permission in ("ml.train", "ml.cancel", "ml.model.read", "ml.jobs.read_all"):
            command += ["--permission", permission]
        subprocess.run(
            command,
            input=value["password"] + "\n",
            text=True,
            capture_output=True,
            check=True,
            timeout=45,
        )
        database = Database(settings.database_url)
        try:
            catalog = SQLDatasetCatalog(database, settings.dataset_root)
            catalog.seed_builtin()
            reference = synthetic_snapshot(root / "ebm-input")
            source = Path(reference["snapshot_ref"]).parent
            actor = Actor(
                value["tenant_id"], "qualification-operator", frozenset({"ml:dataset:manage"})
            )
            catalog.register_ebm(
                actor, "training-ebm", "v1", source / "metadata.json", source / "spectrograms.npz"
            )
        finally:
            database.close()
        with fixture.open("x") as stream:
            os.chmod(fixture, 0o600)
            json.dump(value, stream)
        print(json.dumps({"fixture_ready": True}))
        return
    if mode in {"submit", "cancel"}:
        import httpx

        value = json.loads(fixture.read_text())
        with httpx.Client(
            base_url="http://ml-management:8000", timeout=20, trust_env=False
        ) as client:
            login = client.post("/api/v1/identity/login", json=value)
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            if mode == "submit":
                from src.api.ml_schemas import EBMTrainingIntent, TrainingIntent

                intent = (
                    EBMTrainingIntent(
                        recipe={"id": "ebm_poc_replay_v1", "version": "1.0.0"},
                        dataset={"id": "training-ebm", "version": "v1"},
                        model={"architecture": "ebm_cnn_v1", "config": {"channels": [4, 8]}},
                        training={"epochs": 1, "batch_size": 6, "pair_sample_size": 32},
                    )
                    if sys.argv[2] == "ebm"
                    else TrainingIntent(
                        recipe={"id": "nn_classifier", "version": "1.0.0"},
                        dataset={"id": "dataset-demo", "version": "2026-10-28-v1"},
                    )
                )
                response = client.post(
                    "/api/v1/training/jobs",
                    json={"spec": intent.model_dump()},
                    headers={**headers, "Idempotency-Key": uuid4().hex},
                )
                response.raise_for_status()
                print(json.dumps({"job_id": response.json()["job_id"]}))
            else:
                response = client.post(
                    "/api/v1/training/jobs/" + sys.argv[2] + "/cancel", headers=headers
                )
                response.raise_for_status()
                print(json.dumps({"cancel_requested": True}))
        return
    if mode == "status":
        from src.infrastructure.execution.training_records import read

        database = Database(settings.database_url)
        try:
            repository = SQLTrainingRepository(database)
            job = repository.get(sys.argv[2])
            attempt = repository.attempts(job.job_id)[0]
            execution = json.loads(attempt.execution_ref) if attempt.execution_ref else None
            identity = json.loads(execution["identity_json"]) if execution else None
            receipt = None
            if execution and (Path(execution["directory"]) / "stopped.json").exists():
                stopped = read(Path(execution["directory"]) / "stopped.json")
                receipt = {
                    key: stopped.get(key)
                    for key in ("confirmed", "exit_code", "reason", "pod_uid", "container_id")
                }
            print(
                json.dumps(
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "phase": job.phase,
                        "error_code": job.error_code,
                        "binding_id": job.model_binding_id,
                        "attempt_id": job.attempt_id,
                        "attempt_count": len(repository.attempts(job.job_id)),
                        "execution_id": execution["execution_id"] if execution else None,
                        "directory": execution["directory"] if execution else None,
                        "pod_uid": identity["pod_uid"] if identity else None,
                        "receipt": receipt,
                        "revision": job.revision,
                        "owner_changed": bool(
                            identity and attempt.owner_token != identity["owner_token"]
                        ),
                        "code_revision": json.loads(job.spec_json)["code_revision"],
                        "image_digest": json.loads(job.spec_json)["training_image_digest"],
                    }
                )
            )
        finally:
            database.close()
        return
    if mode == "transport":
        from ipaddress import ip_address
        from urllib.parse import urlsplit

        import httpx
        from redis import Redis

        # This mode executes without entrypoint in Supervisor and loads only its
        # existing private role; output is limited to queue counts and snapshots.
        environment = os.environ
        uri = urlsplit(environment["ML_RABBITMQ_URL"])
        with httpx.Client(timeout=5, trust_env=False) as client:
            address = ip_address(sys.argv[2])
            if not address.is_private:
                raise ValueError("Only the test broker Pod address is allowed")
            host = "[" + str(address) + "]" if address.version == 6 else str(address)
            response = client.get(
                "http://" + host + ":15672/api/queues/%2F/" + settings.queue_name,
                auth=(uri.username, uri.password),
            )
            response.raise_for_status()
            queue = response.json()
        redis = Redis.from_url(
            environment["ML_REDIS_URL"], socket_timeout=2, socket_connect_timeout=2
        )
        try:
            snapshots = [json.loads(redis.get(key)) for key in redis.scan_iter("ml:ack:*")]
        except Exception:
            snapshots = None
        finally:
            redis.close()
        print(
            json.dumps(
                {
                    "ready": queue["messages_ready"],
                    "unacked": queue["messages_unacknowledged"],
                    "snapshots": snapshots,
                }
            )
        )
        return
    raise ValueError("Unknown fixture operation")


if __name__ == "__main__":
    main()
