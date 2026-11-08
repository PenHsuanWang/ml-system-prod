"""Filesystem/SQL failure windows and ownership fencing with real durable state."""

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import update

from bootstrap.ingestion_wiring import build_ingestion
from src.domain.ingestion.errors import PublicationUncertain, SnapshotConflict
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    IngestionRequest,
    PublicationIntent,
)
from src.domain.ml import Conflict
from src.infrastructure.ingestion.persistence.schema import (
    ingestion_attempts,
    ingestion_jobs,
    raw_snapshots,
)
from src.infrastructure.persistence.ml_database import datasets
from tests.ml.ingestion.test_service import PAYLOAD, count_rows, ingress_environment


@pytest.fixture
def ingress_env(components, actor, tmp_path):
    environment = ingress_environment(components, actor, tmp_path)
    try:
        yield environment
    finally:
        environment[0].close()


@contextmanager
def reject_terminal_sql_write(database):
    """An actual DB trigger aborts the SQL finish transaction after filesystem publish."""
    sqlite = database.engine.dialect.name == "sqlite"
    with database.transaction() as connection:
        if sqlite:
            connection.exec_driver_sql(
                "CREATE TRIGGER ingress_test_reject_finish "
                "BEFORE UPDATE OF state ON ingestion_jobs "
                "WHEN NEW.state = 'PUBLISHED' BEGIN "
                "SELECT RAISE(FAIL, 'test terminal persistence failure'); END"
            )
        else:
            connection.exec_driver_sql(
                "CREATE FUNCTION ingress_test_reject_finish() RETURNS trigger AS $$ "
                "BEGIN IF NEW.state = 'PUBLISHED' THEN "
                "RAISE EXCEPTION 'test terminal persistence failure'; END IF; RETURN NEW; END; "
                "$$ LANGUAGE plpgsql"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER ingress_test_reject_finish BEFORE UPDATE ON ingestion_jobs "
                "FOR EACH ROW EXECUTE FUNCTION ingress_test_reject_finish()"
            )
    try:
        yield
    finally:
        with database.transaction() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER ingress_test_reject_finish"
                if sqlite
                else "DROP TRIGGER ingress_test_reject_finish ON ingestion_jobs"
            )
            if not sqlite:
                connection.exec_driver_sql("DROP FUNCTION ingress_test_reject_finish()")


def test_published_filesystem_but_failed_sql_commit_recovers_same_snapshot_without_reacquisition(
    ingress_env, monkeypatch
):
    ingress, settings, actor, _, input_path = ingress_env
    datasets_before = count_rows(ingress.database, datasets)
    job = ingress.service.submit(actor, "sql-failure", IngestionRequest("events", "v1"))
    with reject_terminal_sql_write(ingress.database):
        uncertain = ingress.supervisor().run_once(job_id=job.job_id)
    assert uncertain.state == "NEEDS_RECONCILIATION"
    assert uncertain.publication_intent_json is not None
    intent = PublicationIntent.parse(json.loads(uncertain.publication_intent_json))
    published = ingress.store.lookup(actor.tenant_id, job.snapshot_id, intent.manifest_digest)
    assert published is not None and published.content_digest == intent.staged.content_digest
    manifest_path = Path(published.snapshot_ref)
    original_manifest = manifest_path.read_bytes()
    original_mtime = manifest_path.stat().st_mtime_ns
    assert count_rows(ingress.database, raw_snapshots) == 0
    assert ingress.service.get(actor, job.job_id).state != "PUBLISHED"
    assert job.job_id in ingress.jobs.protected_job_ids()
    assert (
        ingress.store.cleanup_abandoned(ingress.jobs.protected_job_ids(), time.time() + 3600) == 0
    )
    assert Path(intent.staged.staging_ref).exists()
    input_path.write_bytes(b'{"event_id":"changed-after-publication"}\n')

    restarted = build_ingestion(settings, database=ingress.database)
    try:

        def forbidden(*args, **kwargs):
            pytest.fail("Publication reconciliation must not fetch changed source data")

        monkeypatch.setattr(restarted.registry.resolve("file", "1"), "acquire", forbidden)
        recovered = restarted.supervisor().run_once(job_id=job.job_id)
        assert recovered.state == "PUBLISHED" and recovered.attempt_count == 2
        result = restarted.service.result(actor, job.job_id)
        assert result == published
        assert manifest_path.read_bytes() == original_manifest
        assert manifest_path.stat().st_mtime_ns == original_mtime
        assert (manifest_path.parent / "data.jsonl").read_bytes() == PAYLOAD
        assert count_rows(ingress.database, raw_snapshots) == 1
        assert count_rows(ingress.database, datasets) == datasets_before
        assert count_rows(ingress.database, ingestion_attempts) == 2
        assert job.job_id not in restarted.jobs.protected_job_ids()
        assert not Path(intent.staged.staging_ref).exists()
    finally:
        restarted.close()


def test_failure_before_visible_publication_retains_private_intent_and_recovers(
    ingress_env, monkeypatch
):
    ingress, settings, actor, _, input_path = ingress_env
    job = ingress.service.submit(actor, "publication-failure", IngestionRequest("events", "v1"))

    def fail_publication(intent):
        assert ingress.jobs.get(job.job_id).publication_intent_json is not None
        raise PublicationUncertain("Safe simulated publication interruption")

    monkeypatch.setattr(ingress.store, "publish", fail_publication)
    uncertain = ingress.supervisor().run_once(job_id=job.job_id)
    assert uncertain.state == "NEEDS_RECONCILIATION"
    intent = PublicationIntent.parse(json.loads(uncertain.publication_intent_json))
    assert ingress.store.lookup(actor.tenant_id, job.snapshot_id, intent.manifest_digest) is None
    assert Path(intent.staged.staging_ref).exists()
    assert count_rows(ingress.database, raw_snapshots) == 0
    input_path.write_bytes(b'{"event_id":"do-not-use-this-replacement"}\n')
    restarted = build_ingestion(settings, database=ingress.database)
    try:

        def forbidden(*args, **kwargs):
            pytest.fail("Durable publication intent must use its already validated stage")

        monkeypatch.setattr(restarted.registry.resolve("file", "1"), "acquire", forbidden)
        assert restarted.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
        result = restarted.service.result(actor, job.job_id)
        assert result.content_digest == intent.staged.content_digest
        assert (Path(result.snapshot_ref).parent / "data.jsonl").read_bytes() == PAYLOAD
    finally:
        restarted.close()


def test_cancel_before_acquisition_is_terminal_without_file_side_effects(ingress_env):
    ingress, _, actor, _, _ = ingress_env
    job = ingress.service.submit(actor, "cancel-queued", IngestionRequest("events", "v1"))
    cancelled = ingress.service.cancel(actor, job.job_id)
    assert cancelled.state == "CANCELLED"
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
    assert count_rows(ingress.database, ingestion_attempts) == 0
    assert count_rows(ingress.database, raw_snapshots) == 0


def test_cancel_during_acquisition_prevents_publication_and_cleans_stage(ingress_env, monkeypatch):
    ingress, _, actor, _, _ = ingress_env
    adapter = ingress.registry.resolve("file", "1")
    original = adapter.acquire
    stages = []

    def cancel_then_acquire(extraction, control):
        ingress.service.cancel(actor, extraction.execution.job_id)
        staged = original(extraction, control)
        stages.append(staged)
        return staged

    monkeypatch.setattr(adapter, "acquire", cancel_then_acquire)
    job = ingress.service.submit(actor, "cancel-active", IngestionRequest("events", "v1"))
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "CANCELLED" and completed.publication_intent_json is None
    assert count_rows(ingress.database, raw_snapshots) == 0
    for stage in stages:
        assert not Path(stage.staging_ref).exists()


def test_cancel_after_durable_publication_intent_is_rejected_and_result_commits(
    ingress_env, monkeypatch
):
    ingress, _, actor, _, _ = ingress_env
    original = ingress.store.publish
    observed = []

    def attempt_late_cancel(intent):
        observed.append(intent)
        with pytest.raises(Conflict, match="publication"):
            ingress.service.cancel(actor, intent.job_id)
        return original(intent)

    monkeypatch.setattr(ingress.store, "publish", attempt_late_cancel)
    job = ingress.service.submit(actor, "cancel-late", IngestionRequest("events", "v1"))
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    assert len(observed) == 1 and count_rows(ingress.database, raw_snapshots) == 1


def test_expired_owner_cannot_heartbeat_fail_or_complete_a_new_owners_stage(ingress_env):
    ingress, _, actor, source, _ = ingress_env
    job = ingress.service.submit(actor, "fencing", IngestionRequest("events", "v1"))
    old = ingress.jobs.claim_next(job_id=job.job_id)

    class Control:
        def check(self):
            pass

    extraction = BatchExtraction(
        source, old.execution, ExtractionLimits.parse(json.loads(old.job.limits_json))
    )
    staged = ingress.registry.resolve("file", "1").acquire(extraction, Control())
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(lease_until=0)
        )
    current = ingress.jobs.claim_next(job_id=job.job_id)
    assert current.owner_token != old.owner_token
    assert current.job.attempt_id != old.job.attempt_id
    with pytest.raises(Conflict):
        ingress.jobs.heartbeat(old)
    with pytest.raises(Conflict):
        ingress.jobs.stage_complete(old, staged)
    with pytest.raises(Conflict):
        ingress.jobs.fail(old, "STALE_OWNER", retryable=False)
    assert ingress.jobs.heartbeat(current).attempt_id == current.job.attempt_id
    ingress.jobs.fail(current, "TEST_COMPLETE", retryable=False)
    ingress.store.discard(staged)


def test_worker_restart_before_publication_reacquires_and_abandoned_stage_is_cleanable(ingress_env):
    ingress, settings, actor, source, _ = ingress_env
    job = ingress.service.submit(actor, "restart-before-intent", IngestionRequest("events", "v1"))
    old = ingress.jobs.claim_next(job_id=job.job_id)

    class Control:
        def check(self):
            pass

    staged = ingress.registry.resolve("file", "1").acquire(
        BatchExtraction(source, old.execution, source.limits), Control()
    )
    ingress.jobs.stage_complete(old, staged)
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(lease_until=0)
        )
    restarted = build_ingestion(settings, database=ingress.database)
    try:
        assert restarted.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
        assert restarted.service.get(actor, job.job_id).attempt_count == 2
        assert count_rows(ingress.database, raw_snapshots) == 1
        assert Path(staged.staging_ref).exists()
        assert (
            restarted.store.cleanup_abandoned(
                restarted.jobs.protected_job_ids(), time.time() + 3600
            )
            >= 1
        )
        assert not Path(staged.staging_ref).exists()
        assert restarted.store.verify(restarted.service.result(actor, job.job_id))
    finally:
        restarted.close()


def test_publication_collision_is_explicitly_reconciled_not_mistaken_for_owner_fencing(
    ingress_env, monkeypatch
):
    ingress, _, actor, _, _ = ingress_env

    def collision(*args, **kwargs):
        raise SnapshotConflict("Immutable raw snapshot identity conflict")

    monkeypatch.setattr(ingress.store, "lookup", collision)
    job = ingress.service.submit(actor, "collision", IngestionRequest("events", "v1"))
    result = ingress.supervisor().run_once(job_id=job.job_id)
    assert result.state == "NEEDS_RECONCILIATION"
    assert result.error_code == "RAW_SNAPSHOT_CONFLICT"
    assert result.publication_intent_json is not None
    assert count_rows(ingress.database, raw_snapshots) == 0
