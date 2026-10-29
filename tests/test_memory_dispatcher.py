import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from src.application.registry import TaskRegistry
from src.domain.jobs.errors import (
    DispatcherUnavailable,
    QueueFull,
    UnknownTask,
)
from src.domain.jobs.models import JobSnapshot, JobStatus, TaskRequest
from src.infrastructure.messaging.memory import MemoryJobDispatcher


def request(job_id: str, *, name: str = "echo", payload: dict | None = None) -> TaskRequest:
    return TaskRequest(job_id, name, payload or {}, datetime.now(UTC))


async def wait_for_status(
    dispatcher: MemoryJobDispatcher, job_id: str, status: JobStatus
) -> JobSnapshot:
    async with asyncio.timeout(2):
        while True:
            snapshot = await dispatcher.get(job_id)
            if snapshot is not None and snapshot.status == status:
                return snapshot
            await asyncio.sleep(0)


async def echo(payload: dict) -> dict:
    return payload


def registry_with_echo() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register("echo", echo)
    return registry


@pytest.mark.asyncio
async def test_lifecycle_is_idempotent_and_closed_dispatcher_cannot_restart():
    dispatcher = MemoryJobDispatcher(registry_with_echo())
    assert not await dispatcher.is_ready()
    with pytest.raises(DispatcherUnavailable):
        await dispatcher.dispatch(request("before-start"))
    await dispatcher.start()
    await dispatcher.start()
    assert await dispatcher.is_ready()
    await dispatcher.aclose()
    await dispatcher.aclose()
    assert not await dispatcher.is_ready()
    with pytest.raises(DispatcherUnavailable):
        await dispatcher.start()
    with pytest.raises(DispatcherUnavailable):
        await dispatcher.dispatch(request("after-close"))


@pytest.mark.asyncio
async def test_queue_backpressure_has_no_ghost_record():
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(payload: dict) -> dict:
        started.set()
        await release.wait()
        return payload

    registry = TaskRegistry()
    registry.register("echo", blocked)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1, queue_size=1)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("running"))
        await started.wait()
        await dispatcher.dispatch(request("queued"))
        with pytest.raises(QueueFull):
            await dispatcher.dispatch(request("rejected"))
        assert await dispatcher.get("rejected") is None
        assert (await dispatcher.get("queued")).status == JobStatus.QUEUED
    finally:
        release.set()
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_worker_count_bounds_parallel_execution():
    release = asyncio.Event()
    both_started = asyncio.Event()
    active = 0
    peak = 0

    async def blocked(payload: dict) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        try:
            await release.wait()
            return payload
        finally:
            active -= 1

    registry = TaskRegistry()
    registry.register("echo", blocked)
    dispatcher = MemoryJobDispatcher(registry, concurrency=2, queue_size=10)
    await dispatcher.start()
    try:
        for index in range(6):
            await dispatcher.dispatch(request(str(index)))
        await asyncio.wait_for(both_started.wait(), timeout=2)
        assert peak == 2
        assert (await dispatcher.get("2")).status == JobStatus.QUEUED
    finally:
        release.set()
        await dispatcher.aclose()
    assert peak == 2
    assert (await dispatcher.get("5")).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_handler_failure_is_isolated_and_next_job_runs():
    async def sometimes_fails(payload: dict) -> dict:
        if payload.get("fail"):
            raise RuntimeError("expected failure")
        return payload

    registry = TaskRegistry()
    registry.register("echo", sometimes_fails)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("failed", payload={"fail": True}))
        await dispatcher.dispatch(request("successful", payload={"ok": True}))
        failed = await wait_for_status(dispatcher, "failed", JobStatus.FAILED)
        successful = await wait_for_status(dispatcher, "successful", JobStatus.SUCCEEDED)
        assert failed.error == "Task failed (RuntimeError)"
        assert failed.attempts == 1
        assert successful.result == {"ok": True}
        assert successful.started_at.tzinfo is UTC
        assert successful.finished_at.tzinfo is UTC
        assert await dispatcher.is_ready()
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_failure_error_never_exposes_exception_details():
    secret = "https://user:private-password@service.example/private-data"

    async def fails_with_private_data(payload: dict) -> dict:
        raise RuntimeError(secret)

    registry = TaskRegistry()
    registry.register("echo", fails_with_private_data)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("private-failure"))
        failed = await wait_for_status(dispatcher, "private-failure", JobStatus.FAILED)
        assert failed.error == "Task failed (RuntimeError)"
        assert secret not in failed.error
        assert failed.result is None
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "exception_type"),
    [
        ({"invalid": {1, 2}}, "TypeError"),
        ({"invalid": float("nan")}, "ValueError"),
        ({"invalid": datetime.now(UTC)}, "TypeError"),
        (["not", "an", "object"], "TypeError"),
    ],
)
async def test_invalid_json_results_are_recorded_as_failures(result, exception_type):
    async def returns_invalid_result(payload: dict) -> dict:
        return result

    registry = TaskRegistry()
    registry.register("echo", returns_invalid_result)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("invalid-result"))
        failed = await wait_for_status(dispatcher, "invalid-result", JobStatus.FAILED)
        assert failed.error == f"Task failed ({exception_type})"
        assert failed.result is None
        assert await dispatcher.is_ready()
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_job_timeout_releases_worker_for_next_job():
    async def sometimes_blocks(payload: dict) -> dict:
        if payload.get("block"):
            await asyncio.Event().wait()
        return payload

    registry = TaskRegistry()
    registry.register("echo", sometimes_blocks)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1, job_timeout_seconds=0.02)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("timeout", payload={"block": True}))
        await dispatcher.dispatch(request("next"))
        failed = await wait_for_status(dispatcher, "timeout", JobStatus.FAILED)
        assert "exceeded" in failed.error
        await wait_for_status(dispatcher, "next", JobStatus.SUCCEEDED)
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_input_and_result_snapshots_do_not_share_mutable_payloads():
    dispatcher = MemoryJobDispatcher(registry_with_echo(), concurrency=1)
    await dispatcher.start()
    try:
        payload = {"nested": {"items": [1]}}
        await dispatcher.dispatch(request("copy", payload=payload))
        payload["nested"]["items"].append(2)
        first = await wait_for_status(dispatcher, "copy", JobStatus.SUCCEEDED)
        assert first.result == {"nested": {"items": [1]}}
        first.result["nested"]["items"].append(3)
        assert (await dispatcher.get("copy")).result == {"nested": {"items": [1]}}
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_retention_expires_terminal_records():
    dispatcher = MemoryJobDispatcher(registry_with_echo(), retention_seconds=0.02)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("expire"))
        await wait_for_status(dispatcher, "expire", JobStatus.SUCCEEDED)
        await asyncio.sleep(0.03)
        assert await dispatcher.get("expire") is None
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_record_capacity_evicts_oldest_terminal_record():
    dispatcher = MemoryJobDispatcher(registry_with_echo(), max_records=2)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("oldest"))
        await wait_for_status(dispatcher, "oldest", JobStatus.SUCCEEDED)
        await dispatcher.dispatch(request("newer"))
        await wait_for_status(dispatcher, "newer", JobStatus.SUCCEEDED)
        await dispatcher.dispatch(request("newest"))
        assert await dispatcher.get("oldest") is None
        assert await dispatcher.get("newer") is not None
        await wait_for_status(dispatcher, "newest", JobStatus.SUCCEEDED)
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_record_capacity_never_evicts_active_jobs():
    started = asyncio.Event()

    async def blocked(payload: dict) -> dict:
        started.set()
        await asyncio.Event().wait()
        return payload

    registry = TaskRegistry()
    registry.register("echo", blocked)
    dispatcher = MemoryJobDispatcher(
        registry, max_records=1, concurrency=1, shutdown_timeout_seconds=0
    )
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("active"))
        await started.wait()
        with pytest.raises(QueueFull):
            await dispatcher.dispatch(request("rejected"))
        assert await dispatcher.get("rejected") is None
        assert (await dispatcher.get("active")).status == JobStatus.RUNNING
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_unknown_task_and_duplicate_id_do_not_corrupt_existing_job():
    dispatcher = MemoryJobDispatcher(registry_with_echo())
    await dispatcher.start()
    try:
        with pytest.raises(UnknownTask):
            await dispatcher.dispatch(request("unknown", name="missing"))
        assert await dispatcher.get("unknown") is None
        await dispatcher.dispatch(request("duplicate", payload={"original": True}))
        with pytest.raises(ValueError, match="already exists"):
            await dispatcher.dispatch(request("duplicate"))
        snapshot = await wait_for_status(dispatcher, "duplicate", JobStatus.SUCCEEDED)
        assert snapshot.result == {"original": True}
    finally:
        await dispatcher.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"request_id": "invalid\r\ncorrelation"},
        {"tenant_id": "tenant-without-owner"},
        {"payload": {"number": float("nan")}},
    ],
)
async def test_invalid_envelope_does_not_enqueue_or_evict_existing_history(changes):
    dispatcher = MemoryJobDispatcher(registry_with_echo(), max_records=1, concurrency=1)
    await dispatcher.start()
    try:
        await dispatcher.dispatch(request("existing", payload={"original": True}))
        original = await wait_for_status(dispatcher, "existing", JobStatus.SUCCEEDED)
        with pytest.raises(ValueError):
            await dispatcher.dispatch(replace(request("invalid"), **changes))
        assert await dispatcher.get("invalid") is None
        assert await dispatcher.get("existing") == original
        assert dispatcher._queue.empty()
    finally:
        await dispatcher.aclose()


@pytest.mark.asyncio
async def test_shutdown_timeout_cancels_running_and_queued_jobs_and_balances_queue():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(payload: dict) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return payload

    registry = TaskRegistry()
    registry.register("echo", blocked)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1, shutdown_timeout_seconds=0.02)
    await dispatcher.start()
    await dispatcher.dispatch(request("running"))
    await started.wait()
    await dispatcher.dispatch(request("queued"))
    await asyncio.wait_for(dispatcher.aclose(), timeout=2)
    assert cancelled.is_set()
    assert (await dispatcher.get("running")).status == JobStatus.CANCELLED
    queued = await dispatcher.get("queued")
    assert queued.status == JobStatus.CANCELLED
    assert queued.attempts == 0
    await asyncio.wait_for(dispatcher._queue.join(), timeout=0.1)
    assert all(worker.done() for worker in dispatcher._workers)
    await dispatcher.aclose()


@pytest.mark.asyncio
async def test_cancelling_close_caller_propagates_while_cleanup_continues():
    started = asyncio.Event()

    async def blocked(payload: dict) -> dict:
        started.set()
        await asyncio.Event().wait()
        return payload

    registry = TaskRegistry()
    registry.register("echo", blocked)
    dispatcher = MemoryJobDispatcher(registry, concurrency=1, shutdown_timeout_seconds=0.02)
    await dispatcher.start()
    await dispatcher.dispatch(request("running"))
    await started.wait()
    close_caller = asyncio.create_task(dispatcher.aclose())
    await asyncio.sleep(0)
    close_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_caller
    assert not await dispatcher.is_ready()
    await asyncio.wait_for(dispatcher.aclose(), timeout=2)
    assert (await dispatcher.get("running")).status == JobStatus.CANCELLED


@pytest.mark.parametrize(
    "configuration",
    [
        {"concurrency": 0},
        {"queue_size": 0},
        {"max_records": 0},
        {"retention_seconds": 0},
        {"job_timeout_seconds": 0},
        {"shutdown_timeout_seconds": -1},
    ],
)
def test_invalid_limits_are_rejected(configuration):
    with pytest.raises(ValueError):
        MemoryJobDispatcher(registry_with_echo(), **configuration)
