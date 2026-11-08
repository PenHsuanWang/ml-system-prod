"""Lease renewal and phase-aware outcomes over real SQL and immutable storage."""

import json
import time
from threading import Event, get_ident
from threading import enumerate as live_threads

import pytest
from sqlalchemy import update

from src.domain.ingestion.errors import SnapshotConflict
from src.domain.ingestion.models import IngestionRequest, PublicationIntent
from src.domain.ml import Conflict, Unavailable
from src.infrastructure.ingestion.persistence.schema import ingestion_jobs, raw_snapshots
from tests.ml.ingestion.test_service import count_rows, ingress_environment


@pytest.fixture
def runtime_env(components, actor, tmp_path):
    environment = ingress_environment(components, actor, tmp_path)
    try:
        yield environment
    finally:
        environment[0].close()


@pytest.mark.parametrize("phase", ["prepare", "lookup", "publish"])
def test_slow_io_beyond_initial_lease_completes_under_live_owner(runtime_env, monkeypatch, phase):
    ingress, _, actor, _, _ = runtime_env
    ingress.jobs.lease_seconds = 0.2
    original = getattr(ingress.store, phase)

    def slow(*args, **kwargs):
        time.sleep(0.35)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingress.store, phase, slow)
    job = ingress.service.submit(actor, "slow-" + phase, IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "PUBLISHED" and result.attempt_count == 1
    assert count_rows(ingress.database, raw_snapshots) == 1
    assert not any(thread.name == "ingestion-lease" for thread in live_threads())


def test_pre_intent_snapshot_conflict_fails_without_invalid_reconciliation(
    runtime_env, monkeypatch
):
    ingress, _, actor, _, _ = runtime_env

    def conflict(*args):
        raise SnapshotConflict("Publication preparation rejected the candidate")

    monkeypatch.setattr(ingress.store, "prepare", conflict)
    job = ingress.service.submit(actor, "prepare-conflict", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "FAILED" and result.error_code == "RAW_SNAPSHOT_CONFLICT"
    assert result.publication_intent_json is None
    assert count_rows(ingress.database, raw_snapshots) == 0
    assert not list((ingress.store.root / ".staging").rglob("stage.json"))


def test_adapter_business_conflict_is_not_treated_as_an_ownership_loss(runtime_env, monkeypatch):
    ingress, _, actor, _, _ = runtime_env

    def conflict(*args):
        raise Conflict("The approved source cannot satisfy this request")

    monkeypatch.setattr(ingress.registry.resolve("file", "1"), "acquire", conflict)
    job = ingress.service.submit(actor, "source-conflict", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "FAILED" and result.error_code == "STALE_REVISION"
    assert result.publication_intent_json is None


def test_malformed_staged_result_fails_without_attempting_untrusted_cleanup(
    runtime_env, monkeypatch
):
    ingress, _, actor, _, _ = runtime_env
    monkeypatch.setattr(ingress.registry.resolve("file", "1"), "acquire", lambda *args: object())
    job = ingress.service.submit(actor, "bad-contract", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "FAILED" and result.error_code == "INGESTION_INVALID"
    assert result.publication_intent_json is None


def test_keepalive_does_not_override_acquisition_timeout(runtime_env, monkeypatch):
    ingress, _, actor, _, _ = runtime_env
    adapter = ingress.registry.resolve("file", "1")
    original = adapter.acquire

    def slow(*args):
        time.sleep(0.1)
        return original(*args)

    monkeypatch.setattr(adapter, "acquire", slow)
    job = ingress.service.submit(
        actor, "acquisition-timeout", IngestionRequest("events", "v1", timeout_seconds=0.05)
    )
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "RETRY_WAIT" and result.error_code == "ACQUISITION_DEADLINE"
    assert result.publication_intent_json is None


def test_worker_takeover_during_publication_fences_old_owner_and_retains_intent(
    runtime_env, monkeypatch
):
    ingress, _, actor, _, source = runtime_env
    original_heartbeat = ingress.jobs.heartbeat
    original_publish = ingress.store.publish
    main_thread = get_ident()
    takeover = Event()
    replacement_claims = []

    def heartbeat(claim):
        if get_ident() != main_thread and takeover.is_set():
            raise Conflict("Old owner lost its lease")
        return original_heartbeat(claim)

    def publish_with_takeover(intent):
        takeover.set()
        with ingress.database.transaction() as connection:
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == intent.job_id)
                .values(lease_until=0)
            )
        replacement = ingress.jobs.claim_next(job_id=intent.job_id)
        assert replacement is not None
        replacement_claims.append(replacement)
        return original_publish(intent)

    monkeypatch.setattr(ingress.jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(ingress.store, "publish", publish_with_takeover)
    job = ingress.service.submit(actor, "takeover", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    replacement = replacement_claims[0]
    assert result.state == "PUBLISHING" and result.attempt_count == 2
    assert result.owner_token == replacement.owner_token
    assert result.attempt_id == replacement.job.attempt_id
    assert result.publication_intent_json == replacement.job.publication_intent_json
    assert count_rows(ingress.database, raw_snapshots) == 0
    frozen = PublicationIntent.parse(json.loads(result.publication_intent_json))
    published = ingress.store.lookup(actor.tenant_id, job.snapshot_id, frozen.manifest_digest)
    assert published is not None

    monkeypatch.setattr(ingress.jobs, "heartbeat", original_heartbeat)
    monkeypatch.setattr(ingress.store, "publish", original_publish)
    source.write_bytes(b'{"changed":"must not be reacquired"}\n')
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(lease_until=0)
        )
    recovered = ingress.supervisor().run_once(job_id=job.job_id)
    assert recovered.state == "PUBLISHED" and recovered.attempt_count == 3
    assert ingress.service.result(actor, job.job_id) == published
    assert not any(thread.name == "ingestion-lease" for thread in live_threads())


def test_failed_background_heartbeat_retains_post_intent_evidence_for_recovery(
    runtime_env, monkeypatch
):
    ingress, _, actor, _, _ = runtime_env
    original_heartbeat = ingress.jobs.heartbeat
    original_publish = ingress.store.publish
    publishing, lost = Event(), Event()
    main_thread = get_ident()

    def heartbeat(claim):
        if get_ident() != main_thread and publishing.is_set():
            lost.set()
            raise Unavailable("Safe heartbeat outage")
        return original_heartbeat(claim)

    def interrupted(intent):
        publishing.set()
        assert lost.wait(5), "The renewal failure was not observed"
        return original_publish(intent)

    monkeypatch.setattr(ingress.jobs, "heartbeat", heartbeat)
    monkeypatch.setattr(ingress.store, "publish", interrupted)
    job = ingress.service.submit(actor, "heartbeat-outage", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "NEEDS_RECONCILIATION"
    assert result.error_code == "DEPENDENCY_UNAVAILABLE"
    assert result.publication_intent_json is not None
    assert count_rows(ingress.database, raw_snapshots) == 0
    monkeypatch.setattr(ingress.jobs, "heartbeat", original_heartbeat)
    monkeypatch.setattr(ingress.store, "publish", original_publish)
    recovered = ingress.supervisor().run_once(job_id=job.job_id)
    assert recovered.state == "PUBLISHED" and recovered.attempt_count == 2


def test_committed_terminal_result_survives_lost_finish_response(runtime_env, monkeypatch):
    ingress, _, actor, _, _ = runtime_env
    original_finish = ingress.jobs.finish

    def finish_then_lose_response(*args):
        original_finish(*args)
        raise Unavailable("Terminal commit response was lost")

    monkeypatch.setattr(ingress.jobs, "finish", finish_then_lose_response)
    job = ingress.service.submit(actor, "finish-response", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "PUBLISHED" and result.attempt_count == 1
    assert count_rows(ingress.database, raw_snapshots) == 1
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
