"""Real HTTP, independent polling workers and persistent SQL runtime configuration."""

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt

from tests.ml.ingestion import test_source_management
from tests.ml.ingestion.runtime_source_fixture import TEST_KEY

runtime = test_source_management.runtime


def test_register_after_workers_start_publish_and_restart(runtime, tmp_path):
    ingress, actor, body, settings, _ = runtime
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "ML_DATABASE_URL": settings.database_url,
        "ML_RAW_SNAPSHOT_ROOT": str(settings.raw_root),
        "ML_DATABRICKS_ENABLED": "true",
        "ML_DATABRICKS_CONNECTIONS_FILE": str(settings.databricks_connections_file),
        "ML_DATABRICKS_POLICY_FILE": str(settings.databricks_policy_file),
        "ML_DATABRICKS_SOURCES_FILE": "",
        "FIXTURE_PORT": str(port),
    }
    processes = []
    logs = []

    def start(role):
        log = (tmp_path / f"{role}-{len(processes)}.log").open("w+")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.ml.ingestion.runtime_source_fixture", role],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        return process

    def wait_api(client):
        for _ in range(100):
            try:
                if client.get("/api/v1/ingestion/connections").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        raise AssertionError("Fixture API did not become ready")

    token = jwt.encode(
        {
            "tenant": actor.tenant_id,
            "sub": actor.actor_id,
            "permissions": list(actor.permissions),
        },
        TEST_KEY,
        algorithm="HS256",
    )
    try:
        worker = start("worker")
        start("worker")
        for _ in range(100):
            if all("READY" in Path(log.name).read_text() for log in logs):
                break
            time.sleep(0.05)
        assert all("READY" in Path(log.name).read_text() for log in logs)
        api = start("api")
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token},
            timeout=10,
        ) as client:
            wait_api(client)
            assert worker.poll() is None
            original_files = {
                path: path.read_bytes()
                for path in (settings.databricks_connections_file, settings.databricks_policy_file)
            }

            def ingest(source_id, version, key):
                result = client.post(
                    "/api/v1/ingestion/jobs",
                    headers={"Idempotency-Key": key},
                    json={"source_id": source_id, "source_version": version},
                )
                assert result.status_code == 202, result.text
                for _ in range(200):
                    response = client.get(result.json()["result_url"])
                    if response.status_code == 200:
                        break
                    state = client.get(result.json()["status_url"]).json()
                    assert state["state"] not in ("FAILED", "CANCELLED"), state
                    time.sleep(0.05)
                assert response.status_code == 200, response.text
                snapshot = response.json()
                assert (
                    snapshot["content_digest"]
                    == hashlib.sha256(b'{"temperature":1.0}\n').hexdigest()
                )
                assert snapshot["source_revision"].endswith(":128")
                stored = ingress.service.result(actor, result.json()["job_id"])
                assert ingress.store.verify(stored) == stored
                assert (
                    snapshot["source_configuration_digest"]
                    == ingress.sources.resolve(actor, source_id, version).configuration_digest
                )
                return snapshot

            for source_id in ("trace", "trace-second"):
                registered = client.post(
                    "/api/v1/ingestion/sources", json={**body, "source_id": source_id}
                )
                assert registered.status_code == 201, registered.text
                ingest(source_id, "v1", source_id)
            # New processes independently resolve the same immutable catalog state.
            worker.terminate()
            worker.wait(timeout=10)
            start("worker")
            api.terminate()
            api.wait(timeout=10)
            start("api")
            wait_api(client)
            assert client.get("/api/v1/ingestion/sources/trace/versions/v1").status_code == 200
            ingest("trace", "v1", "after-restart")
            assert all(path.read_bytes() == data for path, data in original_files.items())
            assert not settings.databricks_sources_file
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for log in logs:
            log.close()
