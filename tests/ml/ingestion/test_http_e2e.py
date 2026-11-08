"""API admission plus an actual separate SQL-polling ingestion worker process."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import func, select

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from bootstrap.ml_api import create_app
from src.domain.ingestion.models import SourceDefinition
from src.domain.ml import canonical_json
from src.infrastructure.persistence.ml_database import datasets
from tests.ml.test_api import Identity


@pytest.mark.asyncio
async def test_http_admission_worker_process_and_raw_metadata_are_model_independent(
    components, actor, tmp_path
):
    owner = replace(
        actor,
        permissions=actor.permissions
        | {
            "ml:ingest",
            "ml:ingestion:read",
            "ml:ingestion:cancel",
            "ml:source:manage",
        },
    )
    approved = tmp_path / "approved"
    approved.mkdir()
    data = b'{"sensor":"generic","value":1}\n{"sensor":"other","value":2}\n'
    (approved / "events.jsonl").write_bytes(data)
    settings = IngestionSettings(
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw",
        file_roots=(("inbox", approved),),
        retry_delay=0,
    )
    ingress = build_ingestion(settings, database=components.database)
    source = SourceDefinition(
        "events",
        "v1",
        owner.tenant_id,
        "file",
        "1",
        canonical_json(
            {
                "root_key": "inbox",
                "relative_path": "events.jsonl",
                "expected_sha256": hashlib.sha256(data).hexdigest(),
            }
        ),
        allowed_actor_ids=(owner.actor_id,),
    )
    ingress.registry.validate_source(source)
    ingress.sources.register(owner, source)
    with components.database.transaction() as connection:
        dataset_count = connection.scalar(select(func.count()).select_from(datasets))
    app = create_app(
        services=components.services, identity=Identity(owner), ingestion_service=ingress.service
    )
    headers = {"Authorization": "Bearer owner", "Idempotency-Key": "raw-events"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        request = {"source_id": "events", "source_version": "v1"}
        assert (await client.post("/api/v1/ingestion/jobs", json=request)).status_code == 401
        response = await client.post("/api/v1/ingestion/jobs", headers=headers, json=request)
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert response.json()["state"] == "QUEUED"
        assert not list((settings.raw_root / "published").rglob("manifest.json"))
        assert (await client.get(response.json()["result_url"], headers=headers)).status_code == 409
        replay = await client.post("/api/v1/ingestion/jobs", headers=headers, json=request)
        assert replay.json()["job_id"] == job_id

        environment = {
            **os.environ,
            "ML_DATABASE_URL": settings.database_url,
            "ML_RAW_SNAPSHOT_ROOT": str(settings.raw_root),
            "ML_INGESTION_FILE_ROOTS_JSON": json.dumps({"inbox": str(approved)}),
        }
        worker = subprocess.run(
            [sys.executable, "-m", "bootstrap.ingestion_cli", "work", "--once", "--job-id", job_id],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert worker.returncode == 0, worker.stderr
        assert json.loads(worker.stdout) == {"job_id": job_id, "state": "PUBLISHED"}
        result = await client.get(response.json()["result_url"], headers=headers)
        assert result.status_code == 200, result.text
        snapshot = result.json()
        assert snapshot["content_digest"] == hashlib.sha256(data).hexdigest()
        assert snapshot["record_count"] == 2
        assert snapshot["schema_version"] == "raw-json-object-v1"
        assert "snapshot_ref" not in snapshot and "configuration_json" not in snapshot
        assert str(approved) not in result.text and "owner_token" not in result.text
        denied = await client.get(
            response.json()["status_url"], headers={"Authorization": "Bearer other"}
        )
        assert denied.status_code == 404
        stored = ingress.service.result(owner, job_id)
        assert ingress.store.verify(stored) == stored
        with components.database.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(datasets)) == dataset_count


def test_optional_capability_does_not_change_default_management_routes(components, actor):
    app = create_app(services=components.services, identity=Identity(actor))
    assert not any(
        getattr(route, "path", "").startswith("/api/v1/ingestion") for route in app.routes
    )
