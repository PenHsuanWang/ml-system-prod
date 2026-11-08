"""Real SQL admission and File Adapter ingestion; PostgreSQL is opt-in via ML fixtures."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ingestion.errors import AcquisitionFailed, SourceRejected
from src.domain.ingestion.models import ExtractionLimits, IngestionRequest, SourceDefinition
from src.domain.ml import Forbidden, IdempotencyConflict, NotFound
from src.infrastructure.ingestion.persistence.schema import (
    ingestion_attempts,
    ingestion_jobs,
    raw_snapshots,
)
from src.infrastructure.persistence.ml_database import datasets, metadata

PAYLOAD = b'{"event_id":1,"value":"first"}\n{"event_id":2,"value":"second"}\n'


def ingress_environment(components, actor, tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    path = incoming / "events.jsonl"
    path.write_bytes(PAYLOAD)
    settings = IngestionSettings(
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw",
        file_roots=(("test-root", incoming),),
        lease_seconds=3,
        retry_delay=0,
    )
    ingress = build_ingestion(settings, database=components.database)
    metadata.create_all(components.database.engine)
    operator = replace(
        actor,
        permissions=actor.permissions
        | {
            "ml:source:manage",
            "ml:ingest",
            "ml:ingestion:read",
            "ml:ingestion:cancel",
        },
    )
    source = SourceDefinition(
        "events",
        "v1",
        actor.tenant_id,
        "file",
        "1",
        json.dumps({"root_key": "test-root", "relative_path": "events.jsonl"}),
        limits=ExtractionLimits(4096, 20, 1024, 5),
        allowed_actor_ids=(actor.actor_id,),
    )
    ingress.registry.validate_source(source)
    ingress.sources.register(operator, source)
    return ingress, settings, operator, source, path


@pytest.fixture
def ingress_env(components, actor, tmp_path):
    environment = ingress_environment(components, actor, tmp_path)
    try:
        yield environment
    finally:
        environment[0].close()


def count_rows(database, table):
    with database.transaction() as connection:
        return connection.scalar(select(func.count()).select_from(table))


def test_real_file_ingestion_publishes_raw_snapshot_without_registering_ml_dataset(ingress_env):
    ingress, settings, actor, source, path = ingress_env
    datasets_before = count_rows(ingress.database, datasets)
    job = ingress.service.submit(actor, "file-ingestion", IngestionRequest("events", "v1"))
    assert job.state == "QUEUED" and job.attempt_id is None
    assert count_rows(ingress.database, raw_snapshots) == 0
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "PUBLISHED"
    snapshot = ingress.service.result(actor, job.job_id)
    assert snapshot.snapshot_id == job.snapshot_id and snapshot.job_id == job.job_id
    assert snapshot.source_configuration_digest == source.configuration_digest
    assert snapshot.content_digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert snapshot.size_bytes == len(PAYLOAD) and snapshot.record_count == 2
    assert snapshot.data_format == "jsonl" and snapshot.schema_version == "raw-json-object-v1"
    assert ingress.store.verify(snapshot) == snapshot
    assert ingress.service.snapshot(actor, snapshot.snapshot_id) == snapshot
    assert count_rows(ingress.database, datasets) == datasets_before
    assert count_rows(ingress.database, raw_snapshots) == 1
    assert count_rows(ingress.database, ingestion_attempts) == 1
    assert path.read_bytes() == PAYLOAD


def test_concurrent_idempotent_submissions_commit_one_durable_job(ingress_env):
    ingress, _, actor, _, _ = ingress_env
    request = IngestionRequest("events", "v1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        submitted = list(
            pool.map(lambda _: ingress.service.submit(actor, "concurrent", request), range(16))
        )
    assert len({job.job_id for job in submitted}) == 1
    assert count_rows(ingress.database, ingestion_jobs) == 1
    with pytest.raises(IdempotencyConflict):
        ingress.service.submit(actor, "concurrent", replace(request, max_records=1))


def test_concurrent_different_keys_same_source_create_distinct_snapshot_identities(ingress_env):
    ingress, _, actor, _, _ = ingress_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(
            pool.map(
                lambda key: ingress.service.submit(actor, key, IngestionRequest("events", "v1")),
                ("first", "second"),
            )
        )
    assert first.snapshot_id != second.snapshot_id and first.job_id != second.job_id
    assert ingress.supervisor().run_once(job_id=first.job_id).state == "PUBLISHED"
    assert ingress.supervisor().run_once(job_id=second.job_id).state == "PUBLISHED"
    a, b = ingress.service.result(actor, first.job_id), ingress.service.result(actor, second.job_id)
    assert a.content_digest == b.content_digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert count_rows(ingress.database, raw_snapshots) == 2


def test_terminal_redelivery_never_acquires_or_republishes(ingress_env, monkeypatch):
    ingress, _, actor, _, _ = ingress_env
    job = ingress.service.submit(actor, "redelivery", IngestionRequest("events", "v1"))
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    result = ingress.service.result(actor, job.job_id)

    def forbidden(*args, **kwargs):
        pytest.fail("Terminal ingestion must not acquire or publish again")

    monkeypatch.setattr(ingress.registry.resolve("file", "1"), "acquire", forbidden)
    monkeypatch.setattr(ingress.store, "publish", forbidden)
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
    assert ingress.service.result(actor, job.job_id) == result
    assert count_rows(ingress.database, ingestion_attempts) == 1


def test_worker_rechecks_source_revocation_before_acquisition(ingress_env, monkeypatch):
    ingress, _, actor, source, _ = ingress_env
    job = ingress.service.submit(actor, "revoked", IngestionRequest("events", "v1"))
    ingress.sources.set_enabled(actor, source.source_id, source.version, False)

    def forbidden(*args, **kwargs):
        pytest.fail("Revoked source must not be acquired")

    monkeypatch.setattr(ingress.registry.resolve("file", "1"), "acquire", forbidden)
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "FAILED" and completed.error_code == "SOURCE_REJECTED"
    assert count_rows(ingress.database, raw_snapshots) == 0


def test_transient_acquisition_is_bounded_and_uses_a_new_attempt(ingress_env, monkeypatch):
    ingress, _, actor, _, _ = ingress_env
    adapter = ingress.registry.resolve("file", "1")
    original = adapter.acquire
    calls = []

    def fail_once(extraction, control):
        calls.append(extraction.execution.attempt_id)
        if len(calls) == 1:
            raise AcquisitionFailed("Safe transient acquisition error")
        return original(extraction, control)

    monkeypatch.setattr(adapter, "acquire", fail_once)
    job = ingress.service.submit(actor, "retry", IngestionRequest("events", "v1"))
    first = ingress.supervisor().run_once(job_id=job.job_id)
    assert first.state == "RETRY_WAIT" and first.error_code == "ACQUISITION_FAILED"
    assert count_rows(ingress.database, raw_snapshots) == 0
    second = ingress.supervisor().run_once(job_id=job.job_id)
    assert second.state == "PUBLISHED" and second.attempt_count == 2
    assert len(calls) == 2 and calls[0] != calls[1]


def test_service_enforces_permission_tenant_owner_and_operator_scope(ingress_env):
    ingress, _, actor, _, _ = ingress_env
    job = ingress.service.submit(actor, "scope", IngestionRequest("events", "v1"))
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    snapshot = ingress.service.result(actor, job.job_id)
    denied = replace(actor, permissions=frozenset())
    with pytest.raises(Forbidden):
        ingress.service.submit(denied, "denied", IngestionRequest("events", "v1"))
    with pytest.raises(Forbidden):
        ingress.service.get(denied, job.job_id)
    peer = replace(actor, actor_id="peer")
    other = replace(actor, tenant_id="other")
    for identity in (peer, other):
        with pytest.raises(NotFound):
            ingress.service.get(identity, job.job_id)
        with pytest.raises(NotFound):
            ingress.service.snapshot(identity, snapshot.snapshot_id)
    operator = replace(peer, permissions=frozenset({"ml:ingestion:operate"}))
    assert ingress.service.get(operator, job.job_id).job_id == job.job_id
    assert ingress.service.snapshot(operator, snapshot.snapshot_id) == snapshot
    with pytest.raises(SourceRejected):
        ingress.service.submit(other, "other-tenant", IngestionRequest("events", "v1"))


def test_acquisition_retries_stop_at_the_approved_budget(ingress_env, monkeypatch):
    ingress, settings, actor, _, _ = ingress_env
    calls = []

    def unavailable(extraction, control):
        calls.append(extraction.execution.attempt_id)
        raise AcquisitionFailed("Safe simulated source outage")

    monkeypatch.setattr(ingress.registry.resolve("file", "1"), "acquire", unavailable)
    job = ingress.service.submit(actor, "bounded-retries", IngestionRequest("events", "v1"))
    states = [
        ingress.supervisor().run_once(job_id=job.job_id).state for _ in range(settings.max_attempts)
    ]
    assert states == ["RETRY_WAIT"] * (settings.max_attempts - 1) + ["FAILED"]
    assert len(calls) == len(set(calls)) == settings.max_attempts
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
    assert count_rows(ingress.database, raw_snapshots) == 0


def test_queue_deadline_does_not_leave_an_accepted_job_running_forever(ingress_env):
    ingress, _, actor, _, _ = ingress_env
    job = ingress.service.submit(actor, "expired", IngestionRequest("events", "v1"))
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs)
            .where(ingestion_jobs.c.id == job.job_id)
            .values(deadline=time.time() - 1)
        )
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
    expired = ingress.service.get(actor, job.job_id)
    assert expired.state == "FAILED" and expired.error_code == "INGESTION_DEADLINE"
    assert count_rows(ingress.database, raw_snapshots) == 0
