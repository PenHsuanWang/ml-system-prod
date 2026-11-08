"""HTTP transport tests with an explicit service double; no acquisition in handlers."""

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from src.api.ingestion.routers import router
from src.api.ml_http import install_errors
from src.domain.ingestion.models import IngestionJob, RawSnapshot
from src.domain.ml import Actor, Conflict, Forbidden, IdempotencyConflict, NotFound

OWNER = Actor(
    "tenant-a",
    "owner",
    frozenset(
        {
            "ml:ingest",
            "ml:ingestion:read",
            "ml:ingestion:cancel",
        }
    ),
)
ACTORS = {
    "owner": OWNER,
    "other": replace(OWNER, tenant_id="tenant-b"),
    "peer": replace(OWNER, actor_id="peer"),
    "readonly": replace(OWNER, permissions=frozenset({"ml:ingestion:read"})),
    "operator": replace(
        OWNER, actor_id="operator", permissions=frozenset({"ml:ingestion:operate"})
    ),
    "denied": replace(OWNER, permissions=frozenset()),
}


class Identity:
    async def resolve(self, token):
        if token not in ACTORS:
            raise Forbidden("Token is not authorized")
        return ACTORS[token]


class RecordingService:
    """Only implements the API's consumed surface, not source acquisition."""

    def __init__(self):
        self.submissions = []
        self.keys = {}
        self.job = IngestionJob(
            "job-1",
            "tenant-a",
            "owner",
            "events",
            "v1",
            "a" * 64,
            '{"PRIVATE_LIMITS":"/private/source"}',
            "QUEUED",
            1,
            "snapshot-1",
            1000,
            2000,
            owner_token="PRIVATE_OWNER_TOKEN",
            staged_json="PRIVATE_STAGING_PATH",
            publication_intent_json="PRIVATE_MANIFEST",
            result_json="PRIVATE_RESULT",
        )
        self.raw = RawSnapshot(
            "snapshot-1",
            "tenant-a",
            "events",
            "v1",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "/PRIVATE_VOLUME/raw/snapshot-1/manifest.json",
            "jsonl",
            "raw-json-object-v1",
            12,
            1,
            1000,
            "job-1",
            "sha256:" + "b" * 64,
        )

    def submit(self, actor, key, request):
        actor.require("ml:ingest")
        if request.source_id != "events":
            raise NotFound("Approved source not found")
        scope = (actor.tenant_id, actor.actor_id, key)
        previous = self.keys.get(scope)
        if previous is not None and previous != request:
            raise IdempotencyConflict("Idempotency key identifies a different request")
        if previous is None:
            self.submissions.append((actor, key, request))
            self.keys[scope] = request
        return self.job

    def _visible(self, actor, job_id):
        if (
            actor.tenant_id != self.job.tenant_id
            or job_id != self.job.job_id
            or actor.actor_id != self.job.owner_id
            and "ml:ingestion:operate" not in actor.permissions
        ):
            raise NotFound("Ingestion job not found")
        return self.job

    def get(self, actor, job_id):
        if not actor.permissions.intersection(
            {"ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"}
        ):
            raise Forbidden("Permission denied")
        return self._visible(actor, job_id)

    def cancel(self, actor, job_id):
        if not actor.permissions.intersection({"ml:ingestion:cancel", "ml:ingestion:operate"}):
            raise Forbidden("Permission denied")
        self._visible(actor, job_id)
        self.job = replace(self.job, state="CANCELLED", cancel_requested=True)
        return self.job

    def result(self, actor, job_id):
        if self.get(actor, job_id).state != "PUBLISHED":
            raise Conflict("Raw snapshot is not durably published")
        return self.raw

    def snapshot(self, actor, snapshot_id):
        self.get(actor, self.job.job_id)
        if snapshot_id != self.raw.snapshot_id or self.job.state != "PUBLISHED":
            raise NotFound("Raw snapshot not found")
        return self.raw


def app_for(service=None):
    app = FastAPI()
    app.state.ml_identity = Identity()
    if service is not None:
        app.state.ingestion_service = service
    app.include_router(router, prefix="/api/v1/ingestion")
    install_errors(app)
    return app


def headers(token="owner", key="ingestion-key"):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


@pytest.mark.asyncio
async def test_submit_returns_stable_202_without_processing_a_source():
    service = RecordingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://test"
    ) as client:
        body = {
            "source_id": "events",
            "source_version": "v1",
            "max_bytes": 1024,
            "max_records": 20,
            "timeout_seconds": 2.0,
        }
        first = await client.post("/api/v1/ingestion/jobs", headers=headers(), json=body)
        repeat = await client.post("/api/v1/ingestion/jobs", headers=headers(), json=body)
        assert first.status_code == repeat.status_code == 202
        assert first.json()["job_id"] == repeat.json()["job_id"] == "job-1"
        assert first.json()["state"] == "QUEUED"
        assert first.json()["status_url"] == "/api/v1/ingestion/jobs/job-1"
        assert len(service.submissions) == 1
        actor, key, request = service.submissions[0]
        assert actor == OWNER and key == "ingestion-key"
        assert request.max_bytes == 1024 and request.max_records == 20
        assert request.timeout_seconds == 2
        assert "PRIVATE" not in first.text
        assert "owner_token" not in first.json() and "limits_json" not in first.json()
        conflict = await client.post(
            "/api/v1/ingestion/jobs", headers=headers(), json={**body, "max_bytes": 2048}
        )
        assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_job_queries_and_published_metadata_never_expose_private_references():
    service = RecordingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://test"
    ) as client:
        status = await client.get("/api/v1/ingestion/jobs/job-1", headers=headers())
        assert status.status_code == 200 and status.json()["state"] == "QUEUED"
        unavailable = await client.get("/api/v1/ingestion/jobs/job-1/result", headers=headers())
        assert unavailable.status_code == 409
        service.job = replace(service.job, state="PUBLISHED")
        result = await client.get("/api/v1/ingestion/jobs/job-1/result", headers=headers())
        direct = await client.get("/api/v1/ingestion/snapshots/snapshot-1", headers=headers())
        assert result.status_code == direct.status_code == 200
        assert result.json() == direct.json()
        value = result.json()
        assert value["content_digest"] == "b" * 64 and value["manifest_digest"] == "c" * 64
        assert value["record_count"] == 1 and value["size_bytes"] == 12
        assert value["source_id"] == "events" and value["source_version"] == "v1"
        assert value["data_format"] == "jsonl" and value["schema_version"] == "raw-json-object-v1"
        assert value["job_id"] == "job-1" and value["snapshot_id"] == "snapshot-1"
        assert "PRIVATE" not in result.text and "snapshot_ref" not in value
        assert "tenant_id" not in value


@pytest.mark.asyncio
async def test_authentication_permissions_tenant_and_owner_errors_are_safe():
    service = RecordingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://test"
    ) as client:
        body = {"source_id": "events", "source_version": "v1"}
        assert (await client.post("/api/v1/ingestion/jobs", json=body)).status_code == 401
        assert (
            await client.post("/api/v1/ingestion/jobs", headers=headers("denied"), json=body)
        ).status_code == 403
        for token in ("other", "peer"):
            response = await client.get("/api/v1/ingestion/jobs/job-1", headers=headers(token))
            assert response.status_code == 404
            assert "PRIVATE" not in response.text
        assert (
            await client.get("/api/v1/ingestion/jobs/job-1", headers=headers("readonly"))
        ).status_code == 200
        assert (
            await client.get("/api/v1/ingestion/jobs/job-1", headers=headers("operator"))
        ).status_code == 200
        assert (
            await client.post("/api/v1/ingestion/jobs/job-1/cancel", headers=headers("readonly"))
        ).status_code == 403
        cancelled = await client.post("/api/v1/ingestion/jobs/job-1/cancel", headers=headers())
        assert cancelled.status_code == 202 and cancelled.json()["state"] == "CANCELLED"
        assert "PRIVATE" not in cancelled.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"path": "/etc/PRIVATE_TOKEN"},
        {"root": "/arbitrary"},
        {"uri": "https://unapproved"},
        {"configuration_json": '{"token":"PRIVATE_TOKEN"}'},
        {"adapter_type": "module.Class"},
        {"tenant_id": "other"},
        {"actor_id": "other"},
        {"max_bytes": True},
        {"max_records": 0},
        {"timeout_seconds": 301},
        {"source_id": "../source"},
    ],
)
async def test_requests_cannot_supply_source_configuration_paths_or_actor_identity(change):
    service = RecordingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/ingestion/jobs",
            headers=headers(),
            json={"source_id": "events", "source_version": "v1", **change},
        )
        assert response.status_code == 422
        assert "PRIVATE_TOKEN" not in response.text
        assert service.submissions == []


@pytest.mark.asyncio
async def test_body_and_idempotency_header_limits_are_enforced_before_submission():
    service = RecordingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(service)), base_url="http://test"
    ) as client:
        body = {"source_id": "events", "source_version": "v1"}
        missing = await client.post(
            "/api/v1/ingestion/jobs", headers={"Authorization": "Bearer owner"}, json=body
        )
        long_key = await client.post(
            "/api/v1/ingestion/jobs", headers=headers(key="k" * 129), json=body
        )
        large = await client.post("/api/v1/ingestion/jobs", headers=headers(), content=b"x" * 65537)
        assert missing.status_code == long_key.status_code == 422
        assert large.status_code == 413
        assert service.submissions == []


@pytest.mark.asyncio
async def test_disabled_ingestion_fails_closed():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/ingestion/jobs",
            headers=headers(),
            json={"source_id": "events", "source_version": "v1"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
