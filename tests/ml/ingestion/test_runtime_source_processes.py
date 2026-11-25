"""Distinct tables, observable worker execution and persistent SQL configuration."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jwt

from tests.ml.ingestion import test_source_management
from tests.ml.ingestion.runtime_source_fixture import SYNTHETIC_TABLES, TEST_KEY

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
    processes, logs = [], []
    permits = {name: tmp_path / (name + ".permit") for name in ("worker-a", "worker-b")}
    traces = {name: tmp_path / (name + ".jsonl") for name in permits}

    def start(role, worker_id=None):
        log = (tmp_path / f"{role}-{len(processes)}.log").open("w+")
        logs.append(log)
        process_environment = dict(environment)
        if worker_id:
            process_environment.update(
                FIXTURE_WORKER_ID=worker_id,
                FIXTURE_TRACE_FILE=str(traces[worker_id]),
                FIXTURE_PERMIT_FILE=str(permits[worker_id]),
            )
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.ml.ingestion.runtime_source_fixture", role],
            env=process_environment,
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
        workers = {name: start("worker", name) for name in permits}
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
            original_files = {
                path: path.read_bytes()
                for path in (settings.databricks_connections_file, settings.databricks_policy_file)
            }
            initial_worker_pids = {name: process.pid for name, process in workers.items()}
            sources, executions = {}, []

            def ingest(source_id, worker_id, key):
                expected_table = sources[source_id]["configuration"]["table"]
                assert workers[worker_id].poll() is None
                assert not any(path.exists() for path in permits.values())
                permits[worker_id].touch()
                result = client.post(
                    "/api/v1/ingestion/jobs",
                    headers={"Idempotency-Key": key},
                    json={"source_id": source_id, "source_version": "v1"},
                )
                assert result.status_code == 202, result.text
                for _ in range(200):
                    response = client.get(result.json()["result_url"])
                    if response.status_code == 200 and not permits[worker_id].exists():
                        break
                    state = client.get(result.json()["status_url"]).json()
                    assert state["state"] not in ("FAILED", "CANCELLED"), state
                    time.sleep(0.05)
                assert response.status_code == 200, response.text
                assert not permits[worker_id].exists()
                snapshot = response.json()
                temperature = SYNTHETIC_TABLES[tuple(expected_table)]
                payload = f'{{"temperature":{temperature}}}\n'.encode()
                assert snapshot["content_digest"] == hashlib.sha256(payload).hexdigest()
                assert snapshot["record_count"] == 1
                assert snapshot["source_id"] == source_id
                assert snapshot["source_version"] == "v1"
                assert (
                    snapshot["source_configuration_digest"]
                    == sources[source_id]["configuration_digest"]
                )
                stored = ingress.service.result(actor, result.json()["job_id"])
                assert ingress.store.verify(stored) == stored

                event = json.loads(traces[worker_id].read_text().splitlines()[-1])
                assert event["worker_id"] == worker_id
                assert event["pid"] == workers[worker_id].pid
                assert event["pid"] != api.pid
                assert event["table"] == expected_table
                assert event["delta_version"] == 128
                table_sql = ".".join("`" + part + "`" for part in expected_table)
                assert f"FROM {table_sql} VERSION AS OF 128" in event["query"]
                assert event["parameters"]["predicate_0"] == 0
                assert event["parameters"]["extract_limit"] == 100001
                assert snapshot["source_revision"] == "delta:" + event["template_digest"] + ":128"
                definition = ingress.sources.resolve(actor, source_id, "v1")
                assert (
                    event["template_digest"]
                    == json.loads(definition.configuration_json)["template_digest"]
                )
                executions.append({**event, "source_id": source_id, "snapshot": snapshot})
                return snapshot

            for source_id, table, worker_id in (
                ("trace-first", ["synthetic", "fdc", "trace_a"], "worker-a"),
                ("trace-second", ["synthetic", "fdc", "trace_b"], "worker-b"),
            ):
                registered = client.post(
                    "/api/v1/ingestion/sources",
                    json={**body, "source_id": source_id, "table": table},
                )
                assert registered.status_code == 201, registered.text
                sources[source_id] = registered.json()
                assert sources[source_id]["configuration"]["table"] == table
                ingest(source_id, worker_id, source_id)

            assert executions[0]["table"] != executions[1]["table"]
            assert executions[0]["pid"] != executions[1]["pid"]
            assert executions[0]["template_digest"] != executions[1]["template_digest"]
            assert (
                executions[0]["snapshot"]["content_digest"]
                != executions[1]["snapshot"]["content_digest"]
            )
            # Both original workers independently resolve the exact same source.
            ingest("trace-first", "worker-b", "same-source-other-worker")
            assert executions[0]["template_digest"] == executions[2]["template_digest"]
            assert executions[0]["query"] == executions[2]["query"]
            assert (
                executions[0]["snapshot"]["source_configuration_digest"]
                == executions[2]["snapshot"]["source_configuration_digest"]
            )
            assert api.poll() is None
            assert all(process.poll() is None for process in workers.values())

            # Restart every worker and the API, then resolve the other table
            # again from SQL, using a new worker PID and the original digest.
            for process in workers.values():
                process.terminate()
                process.wait(timeout=10)
            workers = {name: start("worker", name) for name in permits}
            api.terminate()
            api.wait(timeout=10)
            api = start("api")
            wait_api(client)
            metadata = client.get("/api/v1/ingestion/sources/trace-second/versions/v1")
            assert metadata.json() == sources["trace-second"]
            ingest("trace-second", "worker-a", "after-all-processes-restart")
            assert executions[3]["pid"] not in initial_worker_pids.values()
            assert executions[3]["template_digest"] == executions[1]["template_digest"]
            assert executions[3]["query"] == executions[1]["query"]
            assert (
                executions[3]["snapshot"]["source_configuration_digest"]
                == executions[1]["snapshot"]["source_configuration_digest"]
            )
            assert all(path.read_bytes() == data for path, data in original_files.items())
            assert not settings.databricks_sources_file
            report = {
                "report_version": "runtime-source-process-acceptance-v2",
                "qualification": "synthetic-databricks-client",
                "status": "PASS",
                "source_count": len(sources),
                "distinct_table_count": len({tuple(e["table"]) for e in executions}),
                "initial_worker_pids": initial_worker_pids,
                "executions": executions,
                "configuration_files_unchanged": True,
                "legacy_templates_configured": False,
            }
            destination = Path(
                os.environ.get("ML_RUNTIME_SOURCE_E2E_REPORT", str(tmp_path / "acceptance.json"))
            )
            destination.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        for log in logs:
            log.close()
