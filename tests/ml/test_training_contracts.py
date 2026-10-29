import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import func, select, update

from src.domain.ml import canonical_json, CapacityExceeded, Conflict, IdempotencyConflict, MLError, NotFound
from src.domain.training.models import JobState, validate_transition
from src.infrastructure.persistence.ml_database import attempts, jobs, keys, outbox
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


def test_canonical_equivalence_and_unicode():
    assert canonical_json({"b": 1.0, "a": "e\u0301"}) == canonical_json({"a": "é", "b": 1})
    assert canonical_json(-0.0) == "0"
    assert canonical_json(10**50+1) != canonical_json(10**50+2)
    with pytest.raises(MLError):
        canonical_json({"é": 1, "e\u0301": 2})
    with pytest.raises(MLError):
        canonical_json(float("nan"))
    with pytest.raises(MLError):
        canonical_json("\ud800")


def test_concurrent_acceptance_is_one_job(components, actor, intent):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: components.services.training.submit(
            actor, "retry", intent), range(20)))
    assert len({j.job_id for j in results}) == 1
    with components.database.transaction() as c:
        for table in (jobs, attempts, outbox, keys):
            assert c.scalar(select(func.count()).select_from(table)) == 1


def test_same_key_conflict_and_backend_drift(components, actor, intent):
    service = components.services.training
    job = service.submit(actor, "retry", intent)

    class UnavailableResolver:
        def resolve(self, value):
            raise AssertionError("Replays must not resolve a new backend")
    service.recipes = UnavailableResolver()
    assert service.submit(actor, "retry", intent).job_id == job.job_id
    value = json.loads(intent)
    value["training"]["epochs"] = 1
    with pytest.raises(IdempotencyConflict):
        service.submit(actor, "retry", canonical_json(value))


def test_expiration_reuse_is_atomic(components, actor, intent):
    service = components.services.training
    old = service.submit(actor, "expired", intent)
    with components.database.transaction() as c:
        c.execute(update(keys).values(expires_at=time.time()-1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        new = list(pool.map(lambda _: service.submit(actor, "expired", intent), range(12)))
    assert len({j.job_id for j in new}) == 1
    assert new[0].job_id != old.job_id


def test_scope_and_visibility(components, actor, intent):
    job = components.services.training.submit(actor, "same-key", intent)
    intruder = replace(actor, actor_id="other")
    tenant = replace(actor, tenant_id="tenant-two")
    for denied in (intruder, tenant):
        with pytest.raises(NotFound):
            components.services.training.get(denied, job.job_id)
        with pytest.raises(NotFound):
            components.services.training.cancel(denied, job.job_id)
    assert components.services.training.submit(intruder, "same-key", intent).job_id != job.job_id


def test_backlog_admission_and_cancel_before_launch(components, actor, intent):
    components.training.max_pending = 1
    job = components.services.training.submit(actor, "one", intent)
    with pytest.raises(CapacityExceeded):
        components.services.training.submit(actor, "two", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    cancelled = components.services.training.cancel(actor, job.job_id)
    assert cancelled.state == "CANCELLED"
    assert components.training.attempts(job.job_id)[0].state == "CANCELLED"
    assert components.training.claim(command).already_complete
    assert components.services.training.submit(actor, "two", intent).job_id != job.job_id


def test_atomic_worker_claim_and_cancel_evidence(components, actor, intent):
    job = components.services.training.submit(actor, "one", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command

    def claim(_):
        try:
            return components.training.claim(command)
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    owner = winners[0]
    assert components.services.training.cancel(actor, job.job_id).state == "CANCEL_REQUESTED"
    components.training.stop(owner, "STOP_UNKNOWN", confirmed=False)
    assert components.training.get(job.job_id).state == "NEEDS_RECONCILIATION"
    components.training.stop(owner, "CANCELLED", confirmed=True)
    assert components.training.get(job.job_id).state == "CANCELLED"
    with pytest.raises(Conflict):
        components.training.execution_started(owner, "stale-process")


def test_delayed_relay_cannot_regress_job(components, actor, intent):
    job = components.services.training.submit(actor, "one", intent)
    store = SQLOutboxRepository(components.database)
    item = store.lease(1)[0]
    components.training.claim(item.command)
    store.published(item)
    assert components.training.get(job.job_id).state == "RUNNING"


def test_outbox_lease_fences_expired_publisher(components, actor, intent):
    components.services.training.submit(actor, "one", intent)
    store = SQLOutboxRepository(components.database)
    old = store.lease(1)[0]
    with components.database.transaction() as c:
        c.execute(update(outbox).values(lease_until=0))
    new = store.lease(1)[0]
    assert old.command.command_id == new.command.command_id
    with pytest.raises(Conflict):
        store.published(old)
    store.published(new)


def test_terminal_states_do_not_reopen():
    for old in ("FAILED", "CANCELLED", "SUCCEEDED"):
        for new in JobState:
            with pytest.raises(Conflict):
                validate_transition(old, new.value)


def test_retry_records_origin_and_checks_access(components, actor, intent):
    old = components.services.training.submit(actor, "original", intent)
    components.services.training.cancel(actor, old.job_id)
    value = json.loads(intent)
    value["retry_of_job_id"] = old.job_id
    new = components.services.training.submit(actor, "retry", canonical_json(value))
    assert new.job_id != old.job_id
    assert json.loads(new.spec_json)["retry_of_job_id"] == old.job_id
    with pytest.raises(NotFound):
        components.services.training.submit(
            replace(actor, tenant_id="other"), "retry", canonical_json(value))


def test_reconciliation_budget_never_falsely_confirms_stop(components, actor, intent):
    job = components.services.training.submit(actor, "lost", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    claim = components.training.claim(command)
    components.training.prepare_execution(claim, '{}')
    for _ in range(5):
        with components.database.transaction() as c:
            c.execute(update(attempts).where(attempts.c.id ==
                      job.attempt_id).values(heartbeat_at=0))
        assert len(components.training.recoverable(4)) == 1
    assert components.training.recoverable(4) == ()
    assert components.training.get(job.job_id).state == "NEEDS_RECONCILIATION"
    assert components.training.get(job.job_id).error_code == "RECONCILIATION_BUDGET_EXHAUSTED"


def test_queue_deadline_does_not_depend_on_a_broker_delivery(components, actor, intent):
    job = components.services.training.submit(actor, "expired-queue", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    with components.database.transaction() as c:
        c.execute(update(jobs).where(jobs.c.id == job.job_id).values(queue_deadline=0))
    assert components.training.expire_queued() == 1
    assert components.training.get(job.job_id).state == "FAILED"
    assert components.training.attempts(job.job_id)[0].state == "FAILED"
    assert components.training.claim(command).already_complete
