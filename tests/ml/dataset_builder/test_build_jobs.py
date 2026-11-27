"""WP6 acceptance on actual immutable File Batch inputs and SQL publication."""

import errno
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from pymongo.errors import AutoReconnect, OperationFailure
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from bootstrap.dataset_build_wiring import build_preparation, build_worker
from bootstrap.ml_api import create_app
from src.api.datasets.build_jobs import BuildRequest
from src.application.datasets.build_jobs import DatasetBuildService
from src.domain.ml import Conflict, NotFound, TransientDependency
from src.infrastructure.datasets.ebm_builder import LocalEBMDatasetPublisher
from src.infrastructure.ingestion.storage import raw_snapshot_writer
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from src.infrastructure.ml_identity import MongoActiveActorResolver
from src.infrastructure.persistence.dataset_build_jobs import (
    BUILD_DEADLINE_SECONDS,
    BUILD_LEASE_SECONDS,
    SQLDatasetBuildJobs,
)
from src.infrastructure.persistence.dataset_build_schema import dataset_build_jobs
from src.infrastructure.persistence.dataset_lineage_schema import dataset_lineage
from src.infrastructure.persistence.ml_database import Database, audit, datasets
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
        abandoned = SQLDatasetBuildJobs(components.database).claim(queued["job_id"])
        assert abandoned is not None
        replacement_clock = abandoned.job.updated_at + BUILD_LEASE_SECONDS + 1
        original_directory_fd = raw_snapshot_writer.directory_fd

        def read_only_directory(path, *, create=False):
            assert not create, "The worker cannot create directories in RawSnapshot storage"
            return original_directory_fd(path)

        monkeypatch.setattr(raw_snapshot_writer, "directory_fd", read_only_directory)
        database = Database(components.settings.database_url)
        checks = []

        def resolve(tenant, owner):
            checks.append((tenant, owner))
            return actor

        try:
            worker = build_worker(
                components.settings,
                SQLDatasetBuildJobs(database, clock=lambda: replacement_clock),
                tmp_path / "raw-snapshots",
                resolve,
            )
            assert worker.run_once() == queued["job_id"]
            assert worker.run_once() is None
        finally:
            database.close()
        assert checks == [(actor.tenant_id, actor.actor_id)] * 2
        status = (await client.get(queued["status_url"])).json()
        assert status["state"] == "SUCCEEDED" and status["attempt_count"] == 2
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
    time[0] += BUILD_LEASE_SECONDS
    assert not queue.renew(stale)
    recovered = queue.claim()
    assert recovered is not None and recovered.token != stale.token
    assert recovered.job.attempt_count == 2
    assert not queue.renew(stale)
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
    with pytest.raises(Conflict, match="lease"):
        stale_service.build(actor, spec.dataset_id, spec.version, spec.inputs, spec.config)
    assert service.get(actor, job.job_id) == persisted


def test_worker_renews_during_long_running_build(
    components, published_builder_inputs, monkeypatch, tmp_path
):
    fixture = published_builder_inputs
    actor = fixture.operator
    now = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: now[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(actor, "long-build", spec)
    started, renewed, finish = Event(), Event(), Event()
    read_records = LocalRawSnapshotStore.read_records
    renew = queue.renew
    missed = False

    def slow_read(store, snapshot):
        started.set()
        assert finish.wait(10)
        return read_records(store, snapshot)

    def observe_renewal(claim):
        nonlocal missed
        if now[0] == job.created_at + BUILD_LEASE_SECONDS / 2:
            if not missed:
                missed = True
                raise TransientDependency("Temporary heartbeat outage")
            alive = renew(claim)
            assert alive
            renewed.set()
            return alive
        return renew(claim)

    monkeypatch.setattr(LocalRawSnapshotStore, "read_records", slow_read)
    monkeypatch.setattr(queue, "renew", observe_renewal)
    worker = build_worker(components.settings, queue, tmp_path / "raw-snapshots", lambda *_: actor)
    worker.heartbeat_interval = 0.01
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(worker.run_once)
        try:
            assert started.wait(10)
            now[0] += BUILD_LEASE_SECONDS / 2
            assert renewed.wait(10)
            now[0] = job.created_at + BUILD_LEASE_SECONDS + 1
            assert queue.claim() is None
        finally:
            finish.set()
        assert result.result(timeout=10) == job.job_id
    completed = service.get(actor, job.job_id)
    assert completed.state == "SUCCEEDED" and completed.attempt_count == 1
    assert completed.receipt_digest is not None


def test_claim_and_renewal_are_capped_by_deadline(components, published_builder_inputs, tmp_path):
    fixture = published_builder_inputs
    actor = fixture.operator
    now = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: now[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(actor, "near-deadline", spec)
    now[0] = job.deadline - 10
    claim = queue.claim()
    assert claim is not None

    def lease_until():
        with components.database.transaction() as connection:
            return connection.scalar(
                select(dataset_build_jobs.c.lease_until).where(
                    dataset_build_jobs.c.id == job.job_id
                )
            )

    assert lease_until() == job.deadline
    now[0] = job.deadline - 1
    assert queue.renew(claim)
    assert lease_until() == job.deadline
    now[0] = job.deadline
    assert not queue.renew(claim)
    preparation = build_preparation(
        components.settings,
        components.database,
        tmp_path / "raw-snapshots",
        complete_build=partial(queue.complete, claim),
    )
    with pytest.raises(Conflict, match="lease"):
        preparation.build(actor, spec.dataset_id, spec.version, spec.inputs, spec.config)
    assert queue.claim() is None
    assert service.get(actor, job.job_id).error_code == "BUILD_DEADLINE_EXCEEDED"
    with pytest.raises(NotFound):
        components.services.datasets.metadata(actor, spec.dataset_id, spec.version)


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


@pytest.mark.parametrize("error_type", [AutoReconnect, OperationFailure])
def test_identity_failure_retries_only_connection_errors(
    components, published_builder_inputs, error_type, tmp_path
):
    fixture = published_builder_inputs
    time = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: time[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(fixture.operator, "retry", spec)

    def unavailable(*args):
        raise error_type("private identity error")

    identity = MongoActiveActorResolver.__new__(MongoActiveActorResolver)
    identity.db = SimpleNamespace(identity_users=SimpleNamespace(find_one=unavailable))
    worker = build_worker(
        components.settings, queue, tmp_path / "raw-snapshots", identity.resolve_active
    )
    attempts_allowed = 3 if error_type is AutoReconnect else 1
    for attempts in range(1, attempts_allowed + 1):
        assert worker.run_once() == job.job_id
        state = service.get(fixture.operator, job.job_id)
        assert (
            state.attempt_count == attempts and state.error_code == "BUILD_DEPENDENCY_UNAVAILABLE"
        )
        assert state.state == ("FAILED" if attempts == attempts_allowed else "QUEUED")
        assert worker.run_once() is None
        time[0] += 5
    expired = service.submit(fixture.operator, "too-late", spec)
    time[0] += BUILD_DEADLINE_SECONDS
    assert queue.claim() is None
    assert service.get(fixture.operator, expired.job_id).error_code == "BUILD_DEADLINE_EXCEEDED"


@pytest.mark.parametrize(
    ("failure", "state", "code"),
    [
        ("stale", "QUEUED", "BUILD_DEPENDENCY_UNAVAILABLE"),
        ("artifact-timeout", "QUEUED", "BUILD_DEPENDENCY_UNAVAILABLE"),
        ("access", "FAILED", "BUILD_REJECTED"),
        ("digest", "FAILED", "BUILD_REJECTED"),
    ],
)
def test_only_known_shared_storage_errors_retry(
    components, published_builder_inputs, monkeypatch, tmp_path, failure, state, code
):
    fixture = published_builder_inputs
    actor = fixture.operator
    now = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: now[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(actor, "storage-failure", spec)
    worker = build_worker(components.settings, queue, tmp_path / "raw-snapshots", lambda *_: actor)

    def read_error(*args):
        raise OSError(errno.ESTALE if failure == "stale" else errno.EACCES, "private path")

    def write_error(*args):
        raise OSError(errno.ETIMEDOUT, "private path")

    with monkeypatch.context() as context:
        if failure == "digest":
            Path(fixture.fdc_snapshot.snapshot_ref).with_name("data.jsonl").write_bytes(b"{}\n")
        elif failure == "artifact-timeout":
            context.setattr(LocalEBMDatasetPublisher, "_write_receipt", write_error)
        else:
            context.setattr(raw_snapshot_writer, "_read_file", read_error)
        assert worker.run_once() == job.job_id
    result = service.get(actor, job.job_id)
    assert (result.state, result.error_code, result.attempt_count) == (state, code, 1)
    with pytest.raises(NotFound):
        components.services.datasets.metadata(actor, spec.dataset_id, spec.version)
    now[0] += 5
    if state == "QUEUED":
        assert worker.run_once() == job.job_id
        assert service.get(actor, job.job_id).state == "SUCCEEDED"
    else:
        assert worker.run_once() is None


@pytest.mark.parametrize("committed", [False, True])
def test_publication_commit_disconnect_resolves_without_duplicate_rows(
    components, published_builder_inputs, monkeypatch, tmp_path, committed
):
    fixture = published_builder_inputs
    actor = fixture.operator
    now = [1000.0]
    queue = SQLDatasetBuildJobs(components.database, clock=lambda: now[0])
    service = DatasetBuildService(queue, fixture.ingress.jobs)
    spec = BuildRequest.model_validate(request_body(fixture)).spec()
    job = service.submit(actor, "commit-disconnect", spec)
    worker = build_worker(components.settings, queue, tmp_path / "raw-snapshots", lambda *_: actor)
    dialect = components.database.engine.dialect
    commit, complete = dialect.do_commit, queue.complete
    armed = False

    def arm_commit(*args):
        nonlocal armed
        complete(*args)
        armed = True

    def disconnect(connection):
        nonlocal armed
        if not armed:
            return commit(connection)
        armed = False
        if committed:
            commit(connection)
        else:
            connection.rollback()
        raise DBAPIError(
            "COMMIT", None, OSError("private database error"), connection_invalidated=True
        )

    with monkeypatch.context() as context:
        context.setattr(queue, "complete", arm_commit)
        context.setattr(dialect, "do_commit", disconnect)
        assert worker.run_once() == job.job_id

    observed = service.get(actor, job.job_id)
    assert observed.state == ("SUCCEEDED" if committed else "QUEUED")
    now[0] += 5
    assert worker.run_once() == (None if committed else job.job_id)
    result = service.get(actor, job.job_id)
    assert result.state == "SUCCEEDED"
    assert result.attempt_count == (1 if committed else 2)
    with components.database.transaction() as connection:
        assert (
            len(
                connection.execute(
                    select(datasets).where(datasets.c.dataset_key == spec.dataset_id)
                ).all()
            )
            == 1
        )
        assert connection.execute(select(dataset_lineage)).scalar_one() is not None
        assert list(
            connection.scalars(
                select(audit.c.action).where(
                    audit.c.subject_id == job.job_id, audit.c.action == "dataset.build.succeed"
                )
            )
        ) == ["dataset.build.succeed"]


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
