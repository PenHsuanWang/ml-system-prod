import asyncio
import re
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from src.config.settings import Settings
from src.domain.execution import ExecutionBusy, ResourceBusy, ResourceUnavailable
from src.infrastructure.locking import resources
from src.infrastructure.locking.resources import ResourceCoordinator
from src.services.concurrency import BoundedThreadExecutor


def execution_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "thread_workers": 1,
        "thread_queue_size": 0,
        "resource_limits": {"compute": 1, "io": 1},
        **overrides,
    }
    return Settings(**values)


def blocking_function(
    loop: asyncio.AbstractEventLoop,
    entered: asyncio.Event,
    release: threading.Event,
    result: int,
) -> int:
    loop.call_soon_threadsafe(entered.set)
    if not release.wait(timeout=5):
        raise TimeoutError("Test did not release its blocking function")
    return result


async def test_thread_executor_requires_start_and_rejects_after_close() -> None:
    executor = BoundedThreadExecutor(execution_settings())
    with pytest.raises(ExecutionBusy, match="unavailable"):
        await executor.run(lambda: 1)
    await executor.start()
    await executor.start()
    assert await executor.run(lambda value: value + 1, 2) == 3
    await executor.aclose()
    await executor.aclose()
    with pytest.raises(ExecutionBusy, match="unavailable"):
        await executor.run(lambda: 1)
    with pytest.raises(RuntimeError, match="closed"):
        await executor.start()


async def test_thread_admission_bounds_workers_plus_queue() -> None:
    executor = BoundedThreadExecutor(execution_settings(thread_queue_size=1))
    await executor.start()
    entered = asyncio.Event()
    release = threading.Event()
    first = asyncio.create_task(
        executor.run(blocking_function, asyncio.get_running_loop(), entered, release, 1)
    )
    queued: asyncio.Task[int] | None = None
    rejected: asyncio.Task[int] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        queued = asyncio.create_task(executor.run(lambda: 2))
        await asyncio.sleep(0)
        rejected = asyncio.create_task(executor.run(lambda: 3))
        await asyncio.sleep(0)
        assert rejected.done(), "Excess work must be rejected without entering an unbounded queue"
        with pytest.raises(ExecutionBusy, match="capacity"):
            await rejected
        assert not queued.done()
        release.set()
        assert await first == 1
        assert await queued == 2
        assert await executor.run(lambda: 4) == 4
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (first, queued, rejected) if task is not None),
            return_exceptions=True,
        )
        await executor.aclose()


async def test_thread_cancellation_waits_for_real_completion_and_preserves_capacity() -> None:
    executor = BoundedThreadExecutor(execution_settings())
    await executor.start()
    entered = asyncio.Event()
    release = threading.Event()
    pending = asyncio.create_task(
        executor.run(blocking_function, asyncio.get_running_loop(), entered, release, 7)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done(), "Cancellation must wait for the physical thread"
        with pytest.raises(ExecutionBusy, match="capacity"):
            await executor.run(lambda: 8)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await executor.run(lambda: 9) == 9
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await executor.aclose()


async def test_repeated_thread_cancellation_cannot_release_resource_lease_early() -> None:
    settings = execution_settings()
    executor = BoundedThreadExecutor(settings)
    coordinator = ResourceCoordinator(settings)
    await executor.start()
    await coordinator.start()
    entered = asyncio.Event()
    release = threading.Event()

    async def owner() -> int:
        async with coordinator.lease("compute", wait_seconds=1):
            return await executor.run(
                blocking_function, asyncio.get_running_loop(), entered, release, 1
            )

    pending = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done(), "Repeated cancellation must not outlive the held resource lease"
        with pytest.raises(ResourceBusy):
            async with coordinator.lease("compute", wait_seconds=0.01):
                pytest.fail("A physical thread still owns the resource")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with coordinator.lease("compute", wait_seconds=0.1):
            pass
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await executor.aclose()
        await coordinator.aclose()


async def test_thread_executor_preserves_context_without_modifying_callers_context() -> None:
    context: ContextVar[str] = ContextVar("test_execution_context", default="empty")
    executor = BoundedThreadExecutor(execution_settings())
    await executor.start()
    token = context.set("request-context")
    caller_thread = threading.get_ident()

    def observe() -> tuple[str, int]:
        value = context.get()
        context.set("thread-context")
        return value, threading.get_ident()

    try:
        value, thread_id = await executor.run(observe)
        assert value == "request-context"
        assert thread_id != caller_thread
        assert context.get() == "request-context"
    finally:
        context.reset(token)
        await executor.aclose()


async def test_thread_close_drains_running_work_and_rejects_new_work() -> None:
    executor = BoundedThreadExecutor(execution_settings())
    await executor.start()
    entered = asyncio.Event()
    release = threading.Event()
    pending = asyncio.create_task(
        executor.run(blocking_function, asyncio.get_running_loop(), entered, release, 1)
    )
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(ExecutionBusy):
            await executor.run(lambda: 2)
        release.set()
        assert await pending == 1
        await closing
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (pending, closing) if task is not None), return_exceptions=True
        )
        await executor.aclose()


async def test_repeated_close_cancellation_preserves_thread_and_resource_lease() -> None:
    settings = execution_settings()
    executor = BoundedThreadExecutor(settings)
    coordinator = ResourceCoordinator(settings)
    await executor.start()
    await coordinator.start()
    entered = asyncio.Event()
    release = threading.Event()

    async def owner() -> int:
        async with coordinator.lease("compute", wait_seconds=1):
            return await executor.run(
                blocking_function, asyncio.get_running_loop(), entered, release, 1
            )

    pending = asyncio.create_task(owner())
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(executor.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done(), "Cancelled shutdown must still drain the physical thread"
        assert not pending.done(), "Shutdown must not cancel the running future wrapper"
        with pytest.raises(ResourceBusy):
            async with coordinator.lease("compute", wait_seconds=0.01):
                pytest.fail("The physical thread still owns its resource")
        release.set()
        assert await pending == 1
        with pytest.raises(asyncio.CancelledError):
            await closing
        async with coordinator.lease("compute", wait_seconds=0.1):
            pass
        with pytest.raises(ExecutionBusy):
            await executor.run(lambda: 2)
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (pending, closing) if task is not None), return_exceptions=True
        )
        await executor.aclose()
        await coordinator.aclose()


async def test_drain_waits_for_threads_and_keeps_executor_available() -> None:
    executor = BoundedThreadExecutor(execution_settings())
    await executor.start()
    entered = asyncio.Event()
    release = threading.Event()
    pending = asyncio.create_task(
        executor.run(blocking_function, asyncio.get_running_loop(), entered, release, 1)
    )
    draining: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        draining = asyncio.create_task(executor.drain())
        await asyncio.sleep(0)
        assert not draining.done()
        release.set()
        assert await pending == 1
        await draining
        assert await executor.run(lambda: 2) == 2
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (pending, draining) if task is not None), return_exceptions=True
        )
        await executor.aclose()


async def test_thread_function_failure_does_not_leak_capacity() -> None:
    executor = BoundedThreadExecutor(execution_settings())
    await executor.start()

    def fail() -> None:
        raise ValueError("adapter failure")

    try:
        with pytest.raises(ValueError, match="adapter failure"):
            await executor.run(fail)
        assert await executor.run(lambda: 1) == 1
    finally:
        await executor.aclose()


async def test_memory_capacity_is_per_group_and_fencing_tokens_increase() -> None:
    coordinator = ResourceCoordinator(execution_settings())
    await coordinator.start()
    try:
        async with coordinator.lease("compute", wait_seconds=0.1) as first:
            assert first.resource == "compute"
            assert not first.lost.is_set()
            with pytest.raises(ResourceBusy):
                async with coordinator.lease("compute", wait_seconds=0.01):
                    pytest.fail("Group capacity must be enforced")
            async with coordinator.lease("io", wait_seconds=0.1) as second:
                assert second.fencing_token > first.fencing_token
                first.lost.set()
                assert not second.lost.is_set()
        async with coordinator.lease("compute", wait_seconds=0.1) as third:
            assert third.fencing_token > second.fencing_token
    finally:
        await coordinator.aclose()


async def test_resource_unknown_groups_and_closed_coordinator_are_rejected() -> None:
    coordinator = ResourceCoordinator(execution_settings())
    assert not await coordinator.is_ready()
    with pytest.raises(ResourceUnavailable):
        async with coordinator.lease("compute", wait_seconds=0.1):
            pytest.fail("Memory coordination must also start before issuing leases")
    await coordinator.start()
    await coordinator.start()
    assert await coordinator.is_ready()
    with pytest.raises(ResourceUnavailable):
        async with coordinator.lease("unknown", wait_seconds=0.1):
            pytest.fail("Unknown groups must not create implicit capacity")
    await coordinator.aclose()
    assert not await coordinator.is_ready()
    with pytest.raises(ResourceUnavailable):
        async with coordinator.lease("compute", wait_seconds=0.1):
            pytest.fail("A closed coordinator cannot issue a lease")
    with pytest.raises(ResourceUnavailable, match="closed"):
        await coordinator.start()


async def test_cancelling_memory_lease_owner_releases_capacity() -> None:
    coordinator = ResourceCoordinator(execution_settings())
    await coordinator.start()
    entered = asyncio.Event()

    async def hold() -> None:
        async with coordinator.lease("compute", wait_seconds=1):
            entered.set()
            await asyncio.Event().wait()

    holder = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        async with coordinator.lease("compute", wait_seconds=0.1):
            pass
    finally:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        await coordinator.aclose()


async def test_cancelling_memory_waiter_does_not_release_someone_elses_capacity() -> None:
    coordinator = ResourceCoordinator(execution_settings())
    await coordinator.start()

    async def wait_for_capacity() -> None:
        async with coordinator.lease("compute", wait_seconds=1):
            pytest.fail("The waiter should have been cancelled")

    try:
        async with coordinator.lease("compute", wait_seconds=0.1):
            waiter = asyncio.create_task(wait_for_capacity())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            with pytest.raises(ResourceBusy):
                async with coordinator.lease("compute", wait_seconds=0.01):
                    pytest.fail("Cancelling a waiter must not manufacture a semaphore permit")
        async with coordinator.lease("compute", wait_seconds=0.1):
            pass
    finally:
        await coordinator.aclose()


def fake_redis_client() -> SimpleNamespace:
    return SimpleNamespace(
        ping=AsyncMock(return_value=True),
        eval=AsyncMock(return_value=1),
        zrem=AsyncMock(return_value=1),
        aclose=AsyncMock(),
    )


async def test_redis_leases_require_start(monkeypatch: pytest.MonkeyPatch) -> None:
    client = fake_redis_client()
    monkeypatch.setattr(resources.Redis, "from_url", lambda *args, **kwargs: client)
    coordinator = ResourceCoordinator(execution_settings(resource_backend="redis"))
    assert not await coordinator.is_ready()
    with pytest.raises(ResourceUnavailable):
        async with coordinator.lease("compute", wait_seconds=0.1):
            pytest.fail("Redis coordination must connect before use")
    await coordinator.start()
    assert await coordinator.is_ready()
    ping_count = client.ping.await_count
    await coordinator.start()
    assert client.ping.await_count == ping_count
    await coordinator.aclose()
    client.aclose.assert_awaited_once()


@pytest.mark.parametrize("renewal_result", [0, RuntimeError("redis://user:secret@private/")])
async def test_redis_claim_and_renewal_use_same_owner_expiry_and_report_loss(
    monkeypatch: pytest.MonkeyPatch, renewal_result: int | Exception
) -> None:
    client = fake_redis_client()

    async def evaluate(script: str, key_count: int, *arguments: object) -> int:
        if script == resources._CLAIM:
            return 17
        if isinstance(renewal_result, Exception):
            raise renewal_result
        return renewal_result

    client.eval.side_effect = evaluate
    monkeypatch.setattr(resources.Redis, "from_url", lambda *args, **kwargs: client)
    coordinator = ResourceCoordinator(
        execution_settings(resource_backend="redis", resource_lease_seconds=3)
    )
    await coordinator.start()
    try:
        async with coordinator.lease("compute", wait_seconds=0.1) as lease:
            assert lease.fencing_token == 17
            await asyncio.wait_for(lease.lost.wait(), timeout=2)
            claim = client.eval.call_args_list[0].args
            renewal = client.eval.call_args_list[1].args
            assert claim[0] == resources._CLAIM
            assert claim[1] == 2
            lease_key, fence_key, capacity, ttl_ms, owner = claim[2:]
            assert lease_key.endswith(":leases")
            assert fence_key.endswith(":fence")
            assert lease_key.split("{")[1].split("}")[0] == fence_key.split("{")[1].split("}")[0]
            assert capacity == 1
            assert ttl_ms == 3000
            assert re.fullmatch(r"[a-f0-9]{32}", owner)
            assert renewal == (resources._RENEW, 1, lease_key, owner, ttl_ms)
        client.zrem.assert_awaited_once_with(lease_key, owner)
    finally:
        await coordinator.aclose()


async def test_redis_claim_failures_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    client = fake_redis_client()
    client.eval.side_effect = RuntimeError("redis://user:secret@private/")
    monkeypatch.setattr(resources.Redis, "from_url", lambda *args, **kwargs: client)
    coordinator = ResourceCoordinator(execution_settings(resource_backend="redis"))
    await coordinator.start()
    try:
        with pytest.raises(ResourceUnavailable) as failure:
            async with coordinator.lease("compute", wait_seconds=0.1):
                pytest.fail("A failed claim cannot issue a lease")
        assert "secret" not in str(failure.value)
        assert client.zrem.await_count == 0
    finally:
        await coordinator.aclose()


async def test_redis_full_capacity_times_out_as_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = fake_redis_client()
    client.eval.return_value = 0
    monkeypatch.setattr(resources.Redis, "from_url", lambda *args, **kwargs: client)
    coordinator = ResourceCoordinator(execution_settings(resource_backend="redis"))
    await coordinator.start()
    try:
        with pytest.raises(ResourceBusy):
            async with coordinator.lease("compute", wait_seconds=0.01):
                pytest.fail("A full resource group cannot issue a lease")
        assert client.zrem.await_count == 0
    finally:
        await coordinator.aclose()


async def test_redis_start_failure_closes_client_without_leaking_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fake_redis_client()
    client.ping.side_effect = RuntimeError("redis://user:secret@private/")
    monkeypatch.setattr(resources.Redis, "from_url", lambda *args, **kwargs: client)
    coordinator = ResourceCoordinator(execution_settings(resource_backend="redis"))
    with pytest.raises(ResourceUnavailable) as failure:
        await coordinator.start()
    assert "secret" not in str(failure.value)
    client.aclose.assert_awaited_once()
    assert not await coordinator.is_ready()


@pytest.fixture
async def execution_redis_url() -> AsyncIterator[str]:
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is unavailable")
    with tempfile.TemporaryDirectory(prefix="er-", dir="/tmp") as temporary:
        socket_path = Path(temporary) / "redis.sock"
        process = await asyncio.create_subprocess_exec(
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(socket_path),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "no",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(3):
                while not socket_path.exists():
                    if process.returncode is not None:
                        output, _ = await process.communicate()
                        diagnostic = output.decode()
                        if "bind: Operation not permitted" in diagnostic:
                            pytest.skip("Sandbox does not permit Redis to bind a Unix socket")
                        pytest.fail(f"redis-server exited during startup: {diagnostic}")
                    await asyncio.sleep(0.01)
            yield f"unix://{socket_path}"
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=3)


@pytest.mark.integration
async def test_real_redis_shared_capacity_renewal_fencing_and_owner_safe_release(
    execution_redis_url: str,
) -> None:
    settings = execution_settings(
        resource_backend="redis",
        redis_url=execution_redis_url,
        queue_name=f"execution-{uuid4().hex}",
        resource_lease_seconds=3,
    )
    first = ResourceCoordinator(settings)
    second = ResourceCoordinator(settings)
    observer = Redis.from_url(execution_redis_url)
    await first.start()
    await second.start()
    first_context = first.lease("compute", wait_seconds=0.2)
    second_context = None
    first_active = False
    second_active = False
    try:
        first_lease = await first_context.__aenter__()
        first_active = True
        key, _ = first._keys("compute")
        owners = await observer.zrange(key, 0, -1)
        assert len(owners) == 1
        original_owner = owners[0]
        original_expiry = await observer.zscore(key, original_owner)
        assert original_expiry is not None
        with pytest.raises(ResourceBusy):
            async with second.lease("compute", wait_seconds=0.05):
                pytest.fail("Separate coordinators must share Redis capacity")

        # Hold longer than the original TTL. Heartbeats must keep the capacity
        # reserved even when the original claim would otherwise have expired.
        await asyncio.sleep(3.2)
        assert not first_lease.lost.is_set()
        renewed_expiry = await observer.zscore(key, original_owner)
        assert renewed_expiry is not None and renewed_expiry > original_expiry
        with pytest.raises(ResourceBusy):
            async with second.lease("compute", wait_seconds=0.05):
                pytest.fail("A renewed lease must still own capacity")

        # Simulate an externally expired/revoked owner, then acquire a new owner.
        # The old context's cleanup must never remove the replacement lease.
        await observer.zrem(key, original_owner)
        second_context = second.lease("compute", wait_seconds=0.2)
        replacement = await second_context.__aenter__()
        second_active = True
        assert replacement.fencing_token > first_lease.fencing_token
        await asyncio.wait_for(first_lease.lost.wait(), timeout=2)
        await first_context.__aexit__(None, None, None)
        first_active = False
        with pytest.raises(ResourceBusy):
            async with first.lease("compute", wait_seconds=0.05):
                pytest.fail("Old-owner release must not remove the replacement lease")
        await second_context.__aexit__(None, None, None)
        second_active = False
        async with first.lease("compute", wait_seconds=0.2) as next_lease:
            assert next_lease.fencing_token > replacement.fencing_token
    finally:
        if first_active:
            await first_context.__aexit__(None, None, None)
        if second_active:
            assert second_context is not None
            await second_context.__aexit__(None, None, None)
        await first.aclose()
        await second.aclose()
        await observer.aclose()
