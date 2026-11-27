"""WP6 acceptance on actual immutable File Batch inputs and SQL publication."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial

import httpx
import pytest
from sqlalchemy import select

from bootstrap.dataset_build_wiring import build_preparation, build_worker
from bootstrap.ml_api import create_app
from src.api.datasets.build_jobs import BuildRequest
from src.application.datasets.build_jobs import DatasetBuildService
from src.domain.ml import Conflict, NotFound, Unavailable
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.persistence.dataset_build_jobs import (
    BUILD_DEADLINE_SECONDS,
    BUILD_LEASE_SECONDS,
    SQLDatasetBuildJobs,
)
from src.infrastructure.persistence.dataset_lineage_schema import dataset_lineage
from src.infrastructure.persistence.ml_database import Database, audit
from tests.ml.test_api import Identity

JOBS = "/api/v1/datasets/build-jobs"
AUTH = {"Authorization": "Bearer owner"}


def request_body(inputs, dataset_id="async-built"):
    return {
        "dataset_id": dataset_id,
        "version": "v1",
        "inputs": [item.to_dict() for item in inputs.references],
        "configuration": {"recipe_id": "recipe-a", "segment_id": 1},
    }


@pytest.mark.asyncio
async def test_api_only_dataset_build_produces_discoverable_verified_lineage(
    components, published_builder_inputs, monkeypatch, tmp_path
):
    fixture = published_builder_inputs
    actor = fixture.operator
    app = create_app(
        services=components.services,
        identity=Identity(actor),
        ingestion_service=fixture.ingress.service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/management"),
        base_url="http://ml.test",
        headers=AUTH,
    ) as client:
        prefix = "/management"
        builders = await client.get(f"{prefix}/api/v1/datasets/builders")
        assert builders.status_code == 200
        builder = builders.json()[0]
        detail = await client.get(
            f"{prefix}/api/v1/datasets/builders/{builder['builder_id']}/versions/{builder['builder_version']}"
        )
        assert detail.status_code == 200
        profile = detail.json()
        assert profile["input_roles"] == ["fdc", "wafer"]
        assert profile["feature_shape"] == [7, 4, 9, 31]
        discovered = (await client.get(f"{prefix}/api/v1/ingestion/snapshots")).json()
        snapshots = {row["snapshot_id"]: row["manifest_digest"] for row in discovered["items"]}
        payload = profile["submit_example"]
        payload["dataset_id"] = "async-built"
        payload["inputs"] = [item.to_dict() for item in fixture.references]
        assert all(
            snapshots[item["snapshot_id"]] == item["manifest_digest"] for item in payload["inputs"]
        )
        headers = {"Idempotency-Key": "e2e-build"}

        def forbidden_read(*args, **kwargs):
            pytest.fail("Management must never read raw payloads during build submission")

        with monkeypatch.context() as context:
            context.setattr(LocalRawSnapshotStore, "read_records", forbidden_read)
            accepted = await client.post(f"{prefix}{JOBS}", json=payload, headers=headers)
            assert accepted.status_code == 202, accepted.text
            queued = accepted.json()
            assert queued["state"] == "QUEUED" and queued["attempt_count"] == 0
            assert queued["status_url"] == f"{prefix}{JOBS}/{queued['job_id']}"
            payload["inputs"].reverse()
            replay = await client.post(f"{prefix}{JOBS}", json=payload, headers=headers)
            assert replay.json()["job_id"] == queued["job_id"]
            conflict = await client.post(
                f"{prefix}{JOBS}",
                json={**payload, "version": "v2"},
                headers=headers,
            )
            assert conflict.status_code == 409
        assert (await client.get(queued["result_url"])).status_code == 409
        assert (
            await client.get(queued["status_url"], headers={"Authorization": "Bearer other"})
        ).status_code == 404
        with pytest.raises(NotFound):
            components.services.datasets.metadata(actor, payload["dataset_id"], payload["version"])

        # Separate SQL engine and worker composition, with a controlled account lookup.
        database = Database(components.settings.database_url)
        checks = []

        def resolve(tenant, owner):
            checks.append((tenant, owner))
            return actor

        try:
            worker = build_worker(
                components.settings,
                SQLDatasetBuildJobs(database),
                tmp_path / "raw-snapshots",
                resolve,
            )
            assert worker.run_once() == queued["job_id"]
            assert worker.run_once() is None
        finally:
            database.close()
        assert checks == [(actor.tenant_id, actor.actor_id)] * 2
        status = (await client.get(queued["status_url"])).json()
        assert status["state"] == "SUCCEEDED" and status["attempt_count"] == 1
        result = (await client.get(queued["result_url"])).json()
        metadata = (await client.get(result["dataset_url"])).json()
        lineage = (await client.get(result["dataset_url"] + "/lineage")).json()
        assert result["content_digest"] == metadata["content_digest"]
        assert (
            lineage["status"] == "complete"
            and lineage["receipt_digest"] == result["receipt_digest"]
        )
        assert {row["snapshot_id"] for row in lineage["receipt"]["inputs"]} == set(snapshots)
        assert (
            not {"receipt_ref", "snapshot_ref", "tenant_id", "owner_id", "lease_token"}
            & result.keys()
        )
        page = (await client.get(f"{prefix}{JOBS}?state=SUCCEEDED&limit=1")).json()
        assert page["items"] == [status]
        assert (await client.get(f"{prefix}{JOBS}?tenant_id=other")).status_code == 422


def test_expired_worker_cannot_publish_and_next_lease_completes_atomically(
    components, published_builder_inputs, tmp_path
):
    fixture = published_builder_inputs
    actor = fixture.operator
    time = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: time[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(actor, "fenced-build", spec)
    with ThreadPoolExecutor(2) as pool:
        claimed = list(pool.map(lambda _: queue.claim(), range(2)))
    assert sum(claim is not None for claim in claimed) == 1
    stale = next(claim for claim in claimed if claim is not None)
    time[0] += BUILD_LEASE_SECONDS + 1
    recovered = queue.claim()
    assert recovered is not None and recovered.token != stale.token
    assert recovered.job.attempt_count == 2
    raw_root = tmp_path / "raw-snapshots"
    stale_service = build_preparation(
        components.settings,
        components.database,
        raw_root,
        complete_build=partial(queue.complete, stale),
    )
    with pytest.raises(Conflict, match="lease"):
        stale_service.build(actor, spec.dataset_id, spec.version, spec.inputs, spec.config)
    queue.fail(stale, "BUILD_REJECTED")
    assert service.get(actor, job.job_id).state == "RUNNING"
    with pytest.raises(NotFound):
        components.services.datasets.metadata(actor, spec.dataset_id, spec.version)
    with components.database.transaction() as connection:
        assert connection.execute(select(dataset_lineage)).first() is None
    current = build_preparation(
        components.settings,
        components.database,
        raw_root,
        complete_build=partial(queue.complete, recovered),
    )
    result = current.build(actor, spec.dataset_id, spec.version, spec.inputs, spec.config)
    persisted = service.get(actor, job.job_id)
    assert persisted.state == "SUCCEEDED" and persisted.receipt_digest == result.receipt_digest


def test_replayed_submission_and_lost_grants_at_publish(
    components, published_builder_inputs, tmp_path
):
    fixture = published_builder_inputs
    actor = fixture.operator
    queue = SQLDatasetBuildJobs(components.database)
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(lambda _: service.submit(actor, "one-job", spec), range(2)))
    assert rows[0].job_id == rows[1].job_id
    job = rows[0]
    with pytest.raises(NotFound):
        service.get(replace(actor, actor_id="peer"), job.job_id)
    reader = replace(actor, actor_id="peer", permissions=frozenset({"ml:jobs:read_all"}))
    assert service.get(reader, job.job_id) == job
    with pytest.raises(NotFound):
        service.get(replace(reader, tenant_id="foreign"), job.job_id)
    current = iter([actor, replace(actor, permissions=frozenset({"ml:dataset:manage"}))])
    worker = build_worker(
        components.settings, queue, tmp_path / "raw-snapshots", lambda *_: next(current)
    )
    worker.run_once()
    failed = service.get(actor, job.job_id)
    assert failed.state == "FAILED" and failed.error_code == "BUILD_INPUT_ACCESS_DENIED"
    assert failed.content_digest is None
    with pytest.raises(NotFound):
        components.services.datasets.metadata(actor, spec.dataset_id, spec.version)
    with components.database.transaction() as connection:
        events = set(
            connection.scalars(select(audit.c.action).where(audit.c.subject_id == job.job_id))
        )
    assert events == {"dataset.build.submit", "dataset.build.fail"}


def test_transient_identity_failure_requeues_and_claim_budget_is_bounded(
    components, published_builder_inputs, tmp_path
):
    fixture = published_builder_inputs
    time = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: time[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(fixture.operator, "retry", spec)

    def unavailable(*args):
        raise Unavailable("private dependency error")

    worker = build_worker(components.settings, queue, tmp_path / "raw-snapshots", unavailable)
    for attempts in (1, 2, 3):
        assert worker.run_once() == job.job_id
        state = service.get(fixture.operator, job.job_id)
        assert (
            state.attempt_count == attempts and state.error_code == "BUILD_DEPENDENCY_UNAVAILABLE"
        )
        assert state.state == ("FAILED" if attempts == 3 else "QUEUED")
        assert worker.run_once() is None
        time[0] += 5
    expired = service.submit(fixture.operator, "too-late", spec)
    time[0] += BUILD_DEADLINE_SECONDS
    assert queue.claim() is None
    assert service.get(fixture.operator, expired.job_id).error_code == "BUILD_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_submit_rejects_invisible_or_unapproved_inputs(components, published_builder_inputs):
    fixture = published_builder_inputs
    actor = fixture.operator

    class BuildIdentity:
        async def resolve(self, token):
            if token == "manager":
                return replace(actor, permissions=frozenset({"ml:dataset:manage"}))
            if token == "peer":
                return replace(
                    actor,
                    actor_id="peer",
                    permissions=frozenset({"ml:dataset:manage", "ml:ingestion:read"}),
                )
            return actor

    app = create_app(services=components.services, identity=BuildIdentity())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ml.test",
    ) as client:
        payload = request_body(fixture)
        headers = {"Idempotency-Key": "blocked"}
        assert (await client.post(JOBS, json=payload, headers=headers)).status_code == 401
        for caller, code in (("manager", 403), ("peer", 404)):
            response = await client.post(
                JOBS,
                json=payload,
                headers={**headers, "Authorization": f"Bearer {caller}"},
            )
            assert response.status_code == code, response.text
        payload["inputs"][0]["manifest_digest"] = "0" * 64
        assert (
            await client.post(JOBS, json=payload, headers={**headers, **AUTH})
        ).status_code == 409
        assert (
            await client.post(
                JOBS,
                json={**payload, "tenant_id": "other"},
                headers={**headers, **AUTH},
            )
        ).status_code == 422
        assert (await client.get(JOBS, headers=AUTH)).json()["items"] == []
