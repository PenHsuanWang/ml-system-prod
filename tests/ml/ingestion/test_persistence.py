"""Real SQL claims, source governance and publication-commit recovery."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError

from src.domain.ingestion.errors import IngestionCancelled, SourceRejected
from src.domain.ingestion.models import (
    ExtractionLimits,
    PublicationIntent,
    RawSnapshot,
    SourceDefinition,
    StagedBatch,
)
from src.domain.ml import (
    Actor,
    Conflict,
    IdempotencyConflict,
    NotFound,
    Unavailable,
    canonical_json,
    digest,
)
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository
from src.infrastructure.ingestion.persistence.schema import (
    ingestion_attempts,
    ingestion_jobs,
    ingestion_keys,
    metadata,
    raw_snapshots,
)
from src.infrastructure.ingestion.persistence.source_catalog import SQLSourceCatalog
from src.infrastructure.persistence.ml_database import Database, audit


@pytest.fixture
def persistence(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "ingestion.db"))
    metadata.create_all(db.engine)
    actor = Actor(
        "tenant-a", "scientist", frozenset({"ml:ingest", "ml:source:manage", "ml:ingestion:cancel"})
    )
    source = SourceDefinition(
        "approved-file",
        "v1",
        actor.tenant_id,
        "file",
        "1",
        canonical_json({"root_key": "approved", "relative_path": "events.jsonl"}),
        limits=ExtractionLimits(timeout_seconds=5),
        allowed_actor_ids=(actor.actor_id,),
    )
    catalog = SQLSourceCatalog(db)
    catalog.register(actor, source)
    repository = SQLIngestionJobRepository(db, retry_delay=0)
    try:
        yield db, actor, source, catalog, repository
    finally:
        db.close()


def submit(persistence, key="ingestion-request", fingerprint="fingerprint"):
    _, actor, source, _, repository = persistence
    return repository.submit(actor, key, fingerprint, source, source.limits)


def staged(claim):
    data = b'{"value":1}\n'
    return StagedBatch(
        "private-stage:" + claim.job.attempt_id,
        claim.job.tenant_id,
        claim.job.job_id,
        claim.job.attempt_id,
        hashlib.sha256(data).hexdigest(),
        len(data),
        1,
        "jsonl",
        "raw-json-object-v1",
        "sha256:" + "a" * 64,
    )


def prepare(persistence, job):
    *_, repository = persistence
    claim = repository.claim_next(job_id=job.job_id)
    value = staged(claim)
    claim = repository.stage_complete(claim, value)
    manifest = {
        "schema_version": "raw-snapshot-manifest-v1",
        "snapshot_id": job.snapshot_id,
        "tenant_id": job.tenant_id,
        "source_id": job.source_id,
        "source_version": job.source_version,
        "source_configuration_digest": job.source_digest,
        "content_digest": value.content_digest,
        "job_id": job.job_id,
        "created_at": job.created_at,
    }
    intent = PublicationIntent(
        job.snapshot_id,
        job.tenant_id,
        job.job_id,
        value,
        canonical_json(manifest),
        digest(manifest),
    )
    claim = repository.prepare_publication(claim, intent)
    result = RawSnapshot(
        job.snapshot_id,
        job.tenant_id,
        job.source_id,
        job.source_version,
        job.source_digest,
        value.content_digest,
        intent.manifest_digest,
        "raw-snapshot:" + job.snapshot_id,
        value.data_format,
        value.schema_version,
        value.size_bytes,
        value.record_count,
        job.created_at,
        job.job_id,
        value.source_revision,
    )
    return claim, intent, result


def test_source_versions_are_immutable_revocable_and_tenant_scoped(persistence):
    _, actor, source, catalog, repository = persistence
    assert catalog.register(actor, source) == source
    assert catalog.resolve(actor, source.source_id, source.version) == source
    with pytest.raises(Conflict, match="immutable"):
        catalog.register(actor, replace(source, limits=ExtractionLimits(max_bytes=100)))
    with pytest.raises(NotFound):
        catalog.register(replace(actor, tenant_id="other"), source)
    with pytest.raises(SourceRejected):
        catalog.resolve(replace(actor, tenant_id="other"), source.source_id, source.version)
    with pytest.raises(SourceRejected):
        catalog.resolve(replace(actor, actor_id="intruder"), source.source_id, source.version)
    job = submit(persistence)
    assert catalog.for_execution(job) == source
    catalog.set_enabled(actor, source.source_id, source.version, False)
    with pytest.raises(SourceRejected):
        catalog.for_execution(job)
    # Durable same-key replay does not resolve/reapprove or replace the accepted source.
    assert repository.find_submission(actor, "ingestion-request", "fingerprint") == job
    catalog.set_enabled(actor, source.source_id, source.version, True)
    assert catalog.for_execution(job) == source


def test_concurrent_same_key_acceptance_is_one_durable_job(persistence):
    db, actor, source, _, repository = persistence
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(
            pool.map(
                lambda _: repository.submit(
                    actor, "same", "same-fingerprint", source, source.limits
                ),
                range(20),
            )
        )
    assert len({job.job_id for job in jobs}) == 1
    assert len({job.snapshot_id for job in jobs}) == 1
    with db.transaction() as connection:
        assert connection.scalar(select(func.count()).select_from(ingestion_jobs)) == 1
        assert connection.scalar(select(func.count()).select_from(ingestion_keys)) == 1
        assert connection.scalar(select(func.count()).select_from(ingestion_attempts)) == 0
    with pytest.raises(IdempotencyConflict):
        repository.submit(actor, "same", "changed", source, source.limits)


def test_concurrent_claims_have_one_owner_and_preserve_stale_owner_fencing(persistence):
    db, _, _, _, repository = persistence
    job = submit(persistence)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: repository.claim_next(job_id=job.job_id), range(12)))
    (first,) = [claim for claim in claims if claim is not None]
    with db.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(lease_until=0)
        )
    second = repository.claim_next(job_id=job.job_id)
    assert second.owner_token != first.owner_token
    assert second.job.attempt_id != first.job.attempt_id
    with pytest.raises(Conflict, match="ownership"):
        repository.heartbeat(first)
    with pytest.raises(Conflict, match="ownership"):
        repository.stage_complete(first, staged(first))
    with db.transaction() as connection:
        assert connection.scalar(select(func.count()).select_from(ingestion_attempts)) == 2
    assert repository.get(job.job_id).owner_token == second.owner_token


def test_publication_intent_is_the_irreversible_cancel_boundary(persistence):
    _, actor, _, _, repository = persistence
    pending = submit(persistence, key="queued")
    assert repository.cancel(actor, pending.job_id).state == "CANCELLED"
    assert repository.claim_next(job_id=pending.job_id) is None
    active = submit(persistence, key="active")
    claim = repository.claim_next(job_id=active.job_id)
    repository.cancel(actor, active.job_id)
    with pytest.raises(IngestionCancelled):
        repository.stage_complete(claim, staged(claim))
    assert repository.fail(claim, "INGESTION_CANCELLED", retryable=False).state == "CANCELLED"
    publishing = submit(persistence, key="publishing")
    claim, _, result = prepare(persistence, publishing)
    with pytest.raises(Conflict, match="publication"):
        repository.cancel(actor, publishing.job_id)
    assert repository.finish(claim, result).state == "PUBLISHED"


def test_sql_finish_failure_retains_intent_and_recovers_without_a_different_snapshot(persistence):
    db, actor, source, catalog, repository = persistence
    job = submit(persistence)
    claim, intent, result = prepare(persistence, job)

    # The filesystem publisher has already returned this exact immutable result.
    def fail_terminal_state(connection, cursor, statement, parameters, context, executemany):
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if statement.startswith("UPDATE ingestion_jobs") and "PUBLISHED" in values:
            raise OperationalError(
                "injected terminal persistence failure", {}, OSError("unavailable")
            )

    event.listen(db.engine, "before_cursor_execute", fail_terminal_state)
    try:
        with pytest.raises(Unavailable):
            repository.finish(claim, result)
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_terminal_state)
    with db.transaction() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_snapshots)) == 0
    assert repository.get(job.job_id).publication_intent_json == canonical_json(intent.to_dict())
    repository.reconcile_later(claim, "PUBLICATION_RECONCILIATION_REQUIRED")
    # Restarted worker uses the stored intent even if source acquisition is no longer approved.
    catalog.set_enabled(actor, source.source_id, source.version, False)
    restarted = SQLIngestionJobRepository(db, retry_delay=0)
    recovered = restarted.claim_next(job_id=job.job_id)
    assert recovered.job.state == "PUBLISHING"
    assert recovered.job.publication_intent_json == canonical_json(intent.to_dict())
    assert recovered.job.snapshot_id == result.snapshot_id
    with pytest.raises(Conflict):
        repository.finish(claim, result)
    completed = restarted.finish(recovered, result)
    assert completed.state == "PUBLISHED"
    assert restarted.snapshot(actor, result.snapshot_id) == result
    assert restarted.claim_next(job_id=job.job_id) is None
    with db.transaction() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_snapshots)) == 1
        assert (
            connection.scalar(
                select(func.count())
                .select_from(audit)
                .where(audit.c.action == "ingestion.published")
            )
            == 1
        )


def test_finish_rejects_conflicting_snapshot_metadata_and_hides_other_owners(persistence):
    _, actor, _, _, repository = persistence
    job = submit(persistence)
    claim, _, result = prepare(persistence, job)
    with pytest.raises(Conflict):
        repository.finish(claim, replace(result, content_digest="b" * 64))
    repository.finish(claim, result)
    with pytest.raises(NotFound):
        repository.snapshot(replace(actor, tenant_id="other"), result.snapshot_id)
    with pytest.raises(NotFound):
        repository.snapshot(replace(actor, actor_id="other"), result.snapshot_id)
    operator = replace(actor, actor_id="operator", permissions=frozenset({"ml:ingestion:operate"}))
    assert repository.snapshot(operator, result.snapshot_id) == result


def test_transient_acquisition_retry_is_bounded_and_does_not_publish(persistence):
    db, _, _, _, repository = persistence
    job = submit(persistence)
    states = []
    for _ in range(3):
        claim = repository.claim_next(job_id=job.job_id)
        states.append(repository.fail(claim, "ACQUISITION_FAILED", retryable=True).state)
    assert states == ["RETRY_WAIT", "RETRY_WAIT", "FAILED"]
    assert repository.claim_next(job_id=job.job_id) is None
    with db.transaction() as connection:
        assert connection.scalar(select(func.count()).select_from(raw_snapshots)) == 0


def test_unknown_publication_remains_reconcilable_after_deadline_and_budget(persistence):
    db, _, _, _, repository = persistence
    job = submit(persistence)
    claim, intent, _ = prepare(persistence, job)
    repository.reconcile_later(claim, "PUBLICATION_UNCERTAIN")
    with db.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(deadline=0)
        )
    bounded = SQLIngestionJobRepository(db, retry_delay=0, reconcile_limit=1)
    recovered = bounded.claim_next(job_id=job.job_id)
    assert recovered.job.state == "PUBLISHING"
    bounded.reconcile_later(recovered, "PUBLICATION_UNCERTAIN")
    assert bounded.claim_next(job_id=job.job_id) is None
    final = bounded.get(job.job_id)
    assert final.state == "NEEDS_RECONCILIATION"
    assert final.error_code == "RECONCILIATION_BUDGET_EXHAUSTED"
    assert final.publication_intent_json == canonical_json(intent.to_dict())
    assert job.job_id in bounded.protected_job_ids()


def test_prepublication_deadline_and_error_translation_are_terminal(persistence):
    db, _, _, _, repository = persistence
    job = submit(persistence, key="expired")
    with db.transaction() as connection:
        connection.execute(
            update(ingestion_jobs).where(ingestion_jobs.c.id == job.job_id).values(deadline=0)
        )
    assert repository.claim_next(job_id=job.job_id) is None
    assert repository.get(job.job_id).state == "FAILED"
    failed = submit(persistence, key="unsafe-detail")
    claim = repository.claim_next(job_id=failed.job_id)
    result = repository.fail(claim, "password=do-not-store", retryable=False)
    assert result.error_code == "INGESTION_FAILED"
    assert failed.job_id not in repository.protected_job_ids()


def test_explicit_ingestion_migration_is_idempotent(tmp_path):
    from bootstrap.ml_cli import migrate
    from bootstrap.ml_config import MLSettings

    path = tmp_path / "migrated" / "database.db"
    settings = MLSettings(database_url="sqlite:///" + str(path))
    migrate(settings)
    migrate(settings)
    db = Database(settings.database_url)
    try:
        assert db.ready()
        with db.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(ingestion_jobs)) == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    "options",
    [
        {"lease_seconds": float("nan")},
        {"retry_delay": float("inf")},
        {"queue_timeout": 0},
        {"max_attempts": True},
        {"reconcile_limit": 0},
    ],
)
def test_queue_rejects_unbounded_or_invalid_server_configuration(persistence, options):
    db, *_ = persistence
    with pytest.raises(ValueError):
        SQLIngestionJobRepository(db, **options)
