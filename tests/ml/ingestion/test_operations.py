"""Explicit operator recovery retains the original immutable publication candidate."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from src.domain.ingestion.errors import PublicationUncertain
from src.domain.ingestion.models import BatchExtraction, IngestionRequest
from src.domain.ml import Actor, Conflict, Forbidden, NotFound
from src.infrastructure.ingestion.persistence.schema import ingestion_jobs
from tests.ml.ingestion.test_service import ingress_environment


def test_identity_permission_aliases_include_ingestion_scopes():
    actor = Actor.from_identity(
        "tenant",
        "owner",
        frozenset(
            {
                "ml.ingest",
                "ml.ingestion.read",
                "ml.ingestion.cancel",
                "ml.ingestion.operate",
                "ml.source.manage",
            }
        ),
    )
    assert {
        "ml:ingest",
        "ml:ingestion:read",
        "ml:ingestion:cancel",
        "ml:ingestion:operate",
        "ml:source:manage",
    } <= actor.permissions


def test_operator_reconciliation_preserves_intent_and_cannot_reopen_terminal(
    components, actor, tmp_path, monkeypatch
):
    ingress, _, owner, _, source_path = ingress_environment(components, actor, tmp_path)
    original = ingress.store.publish

    def interrupted(intent):
        raise PublicationUncertain("Simulated storage interruption")

    monkeypatch.setattr(ingress.store, "publish", interrupted)
    job = ingress.service.submit(owner, "operator-recovery", IngestionRequest("events", "v1"))
    uncertain = ingress.supervisor().run_once(job_id=job.job_id)
    frozen = uncertain.publication_intent_json
    with ingress.database.transaction() as connection:
        connection.execute(
            update(ingestion_jobs)
            .where(ingestion_jobs.c.id == job.job_id)
            .values(
                reconciliation_count=ingress.jobs.reconcile_limit, error_code=None, lease_until=0
            )
        )
    assert ingress.supervisor().run_once(job_id=job.job_id) is None
    exhausted = ingress.jobs.get(job.job_id)
    assert exhausted.state == "NEEDS_RECONCILIATION"
    assert exhausted.error_code == "RECONCILIATION_BUDGET_EXHAUSTED"
    assert exhausted.publication_intent_json == frozen
    with pytest.raises(Forbidden):
        ingress.service.reconcile(owner, job.job_id)
    operator = replace(owner, permissions=owner.permissions | {"ml:ingestion:operate"})
    with pytest.raises(NotFound):
        ingress.service.reconcile(replace(operator, tenant_id="other"), job.job_id)
    requested = ingress.service.reconcile(operator, job.job_id)
    assert requested.publication_intent_json == frozen
    # A fresh acquire would use this changed source; recovery must retain old staged bytes.
    source_path.write_bytes(b'{"changed":"after-operator-review"}\n')
    monkeypatch.setattr(ingress.store, "publish", original)
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "PUBLISHED"
    snapshot = ingress.service.result(owner, job.job_id)
    assert snapshot.manifest_digest == json.loads(frozen)["manifest_digest"]
    with pytest.raises(Conflict):
        ingress.service.reconcile(operator, job.job_id)


def test_exhausted_publications_do_not_starve_later_queued_ingestions(
    components, actor, tmp_path, monkeypatch
):
    ingress, _, owner, source, _ = ingress_environment(components, actor, tmp_path)
    ingress.jobs.max_pending = 200
    # Build queue state without making this admission-policy test depend on a
    # three-second wall-clock lease while SQL/fsync runs on a busy CI host.
    now = time.time()
    monkeypatch.setattr(
        "src.infrastructure.ingestion.persistence.ingestion_jobs.time",
        SimpleNamespace(time=lambda: now),
    )

    class Control:
        def check(self):
            pass

    # Queue policy must filter exhausted work rather than repeatedly scanning its first page.
    blocked = []
    for index in range(101):
        job = ingress.service.submit(owner, f"old-{index}", IngestionRequest("events", "v1"))
        blocked.append(job.job_id)
        claim = ingress.jobs.claim_next(job_id=job.job_id)
        extraction = BatchExtraction(source, claim.execution, source.limits)
        staged = ingress.registry.resolve("file", "1").acquire(extraction, Control())
        claim = ingress.jobs.stage_complete(claim, staged)
        ingress.jobs.prepare_publication(claim, ingress.store.prepare(extraction, staged))
        with ingress.database.transaction() as connection:
            connection.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.id == job.job_id)
                .values(
                    state="NEEDS_RECONCILIATION",
                    lease_until=0,
                    reconciliation_count=ingress.jobs.reconcile_limit,
                    error_code="RECONCILIATION_BUDGET_EXHAUSTED",
                    deadline=now - 1,
                )
            )
    queued = ingress.service.submit(owner, "new-work", IngestionRequest("events", "v1"))
    claim = ingress.jobs.claim_next()
    assert claim is not None and claim.job.job_id == queued.job_id
    assert set(blocked) <= ingress.jobs.protected_job_ids()
