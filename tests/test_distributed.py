import asyncio
import json
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from taskiq import AckableMessage, InMemoryBroker, TaskiqMessage
from taskiq.receiver.receiver import Receiver

from bootstrap.worker import SnapshotPersistenceMiddleware, execute_job
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.models import JobSnapshot, JobStatus, TaskRequest
from src.infrastructure.messaging import distributed, redis_store
from src.infrastructure.messaging.distributed import TASK_NAME, DistributedJobDispatcher
from src.infrastructure.messaging.redis_store import (
    RedisJobStore,
    decode_snapshot,
    encode_request,
)


class FakeStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, JobSnapshot] = {}
        self.fail_terminal = False
        self.fail_all = False
        self.started = False
        self.save_failed = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.started = False

    async def is_ready(self) -> bool:
        return self.started

    async def get(self, job_id: str) -> JobSnapshot | None:
        return self.snapshots.get(job_id)

    async def save(self, snapshot: JobSnapshot) -> None:
        if self.fail_all or (self.fail_terminal and snapshot.status != JobStatus.RUNNING):
            self.save_failed.set()
            raise DispatcherUnavailable("Job state could not be stored")
        self.snapshots[snapshot.job_id] = snapshot


def task_request() -> TaskRequest:
    return TaskRequest("job-123", "echo", {"value": 42}, datetime.now(UTC))


async def echo(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def echo_registry() -> TaskRegistry:
    registry = TaskRegistry()
    registry.register("echo", echo)
    return registry


def prepare_receiver(
    store: FakeStore, registry: TaskRegistry | None = None
) -> tuple[Receiver, AckableMessage, AsyncMock]:
    broker = InMemoryBroker()
    broker.state.registry = registry or echo_registry()
    broker.state.store = store
    broker.state.settings = Settings(_env_file=None)
    broker.with_middlewares(SnapshotPersistenceMiddleware(cast(RedisJobStore, store), 0.001))
    broker.register_task(execute_job, task_name=TASK_NAME, ack_type="when_saved")
    message = TaskiqMessage(
        task_id="job-123",
        task_name=TASK_NAME,
        args=[encode_request(task_request())],
        kwargs={},
        labels={"ack_type": "when_saved"},
    )
    ack = AsyncMock()
    delivery = AckableMessage(data=broker.formatter.dumps(message).message, ack=ack)
    return Receiver(broker, max_async_tasks=2), delivery, ack


async def test_receiver_commits_terminal_snapshot_before_acknowledgement() -> None:
    store = FakeStore()
    receiver, message, ack = prepare_receiver(store)

    async def assert_saved_before_ack() -> None:
        assert store.snapshots["job-123"].status == JobStatus.SUCCEEDED

    ack.side_effect = assert_saved_before_ack
    await receiver.callback(message)
    snapshot = store.snapshots["job-123"]
    assert snapshot.result == {"value": 42}
    assert snapshot.attempts == 1
    ack.assert_awaited_once()


@pytest.mark.parametrize("fail_all", [False, True])
async def test_storage_failure_never_acknowledges_delivery(fail_all: bool) -> None:
    store = FakeStore()
    store.fail_terminal = True
    store.fail_all = fail_all
    receiver, message, ack = prepare_receiver(store)
    callback = asyncio.create_task(receiver.callback(message))
    await asyncio.wait_for(store.save_failed.wait(), timeout=1)
    assert not callback.done()
    ack.assert_not_awaited()
    callback.cancel()
    with pytest.raises(asyncio.CancelledError):
        await callback
    ack.assert_not_awaited()


async def test_storage_recovery_commits_result_without_repeating_handler() -> None:
    store = FakeStore()
    store.fail_terminal = True
    handler = AsyncMock(return_value={"value": 42})
    registry = TaskRegistry()
    registry.register("echo", handler)
    receiver, message, ack = prepare_receiver(store, registry)
    callback = asyncio.create_task(receiver.callback(message))
    await asyncio.wait_for(store.save_failed.wait(), timeout=1)
    ack.assert_not_awaited()
    store.fail_terminal = False
    await asyncio.wait_for(callback, timeout=1)
    handler.assert_awaited_once()
    ack.assert_awaited_once()


async def test_worker_cancellation_leaves_delivery_unacknowledged() -> None:
    async def cancelled(payload: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    registry = TaskRegistry()
    registry.register("echo", cancelled)
    store = FakeStore()
    receiver, message, ack = prepare_receiver(store, registry)
    with pytest.raises(asyncio.CancelledError):
        await receiver.callback(message)
    assert store.snapshots["job-123"].status == JobStatus.RUNNING
    ack.assert_not_awaited()


async def test_terminal_redelivery_does_not_execute_handler_again() -> None:
    store = FakeStore()
    handler = AsyncMock(return_value={"value": 42})
    registry = TaskRegistry()
    registry.register("echo", handler)
    receiver, message, ack = prepare_receiver(store, registry)
    await receiver.callback(message)
    await receiver.callback(message)
    handler.assert_awaited_once()
    assert ack.await_count == 2


async def test_handler_failure_is_sanitized_persisted_and_acknowledged() -> None:
    async def fail(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("amqp://private:password@internal/")

    registry = TaskRegistry()
    registry.register("echo", fail)
    store = FakeStore()
    receiver, message, ack = prepare_receiver(store, registry)
    await receiver.callback(message)
    snapshot = store.snapshots["job-123"]
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.error == "Task failed (RuntimeError)"
    ack.assert_awaited_once()


async def test_worker_deadline_records_failure_and_acknowledges() -> None:
    async def blocked(payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()
        return payload

    registry = TaskRegistry()
    registry.register("echo", blocked)
    store = FakeStore()
    receiver, message, ack = prepare_receiver(store, registry)
    receiver.broker.state.settings = Settings(_env_file=None, job_timeout_seconds=0.01)
    await asyncio.wait_for(receiver.callback(message), timeout=1)
    assert store.snapshots["job-123"].status == JobStatus.FAILED
    assert store.snapshots["job-123"].error == "Task timed out"
    ack.assert_awaited_once()


async def test_dispatch_publishes_json_and_preserves_application_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    broker = InMemoryBroker()
    broker.kick = AsyncMock()
    monkeypatch.setattr(distributed, "build_taskiq_broker", lambda settings: broker)
    monkeypatch.setattr(distributed, "RedisJobStore", lambda *args: store)
    dispatcher = DistributedJobDispatcher(echo_registry(), Settings(_env_file=None))
    await dispatcher.start()
    try:
        request = task_request()
        await dispatcher.dispatch(request)
        published = broker.kick.call_args.args[0]
        data = json.loads(published.message)
        assert data["task_id"] == request.job_id
        assert data["task_name"] == TASK_NAME
        assert data["args"][0]["payload"] == request.payload
        assert (await dispatcher.get(request.job_id)).status == JobStatus.QUEUED
    finally:
        await dispatcher.aclose()


async def test_publish_failure_retains_queued_snapshot_and_hides_connection_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    broker = InMemoryBroker()
    broker.kick = AsyncMock(side_effect=RuntimeError("amqp://user:password@private/"))
    monkeypatch.setattr(distributed, "build_taskiq_broker", lambda settings: broker)
    monkeypatch.setattr(distributed, "RedisJobStore", lambda *args: store)
    dispatcher = DistributedJobDispatcher(echo_registry(), Settings(_env_file=None))
    await dispatcher.start()
    try:
        with pytest.raises(DispatcherUnavailable, match="job_id=job-123") as caught:
            await dispatcher.dispatch(task_request())
        assert "password" not in str(caught.value)
        assert store.snapshots["job-123"].status == JobStatus.QUEUED
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
async def test_invalid_envelope_is_rejected_before_storage_or_publication(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    store = FakeStore()
    store.save = AsyncMock(wraps=store.save)
    broker = InMemoryBroker()
    broker.kick = AsyncMock()
    monkeypatch.setattr(distributed, "build_taskiq_broker", lambda settings: broker)
    monkeypatch.setattr(distributed, "RedisJobStore", lambda *args: store)
    dispatcher = DistributedJobDispatcher(echo_registry(), Settings(_env_file=None))
    await dispatcher.start()
    try:
        with pytest.raises(ValueError):
            await dispatcher.dispatch(replace(task_request(), **changes))
        store.save.assert_not_awaited()
        broker.kick.assert_not_awaited()
        assert store.snapshots == {}
    finally:
        await dispatcher.aclose()


async def test_redis_snapshot_json_roundtrip_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        set=AsyncMock(),
        get=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(redis_store.Redis, "from_url", lambda *args, **kwargs: client)
    store = RedisJobStore("redis://localhost", 1.25, "test")
    await store.start()
    snapshot = JobSnapshot.queued(task_request())
    await store.save(snapshot)
    call = client.set.call_args
    assert call.args[0] == "test:job:job-123"
    assert call.kwargs == {"px": 1250}
    data = call.args[1]
    assert decode_snapshot(json.loads(data)) == snapshot
    client.get.return_value = data.encode()
    assert await store.get("job-123") == snapshot
    await store.aclose()
    client.aclose.assert_awaited_once()


async def test_redis_read_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        get=AsyncMock(side_effect=RuntimeError("redis://user:secret@private/")),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(redis_store.Redis, "from_url", lambda *args, **kwargs: client)
    store = RedisJobStore("redis://localhost", 60, "test")
    await store.start()
    try:
        with pytest.raises(DispatcherUnavailable) as caught:
            await store.get("job-123")
        assert "secret" not in str(caught.value)
    finally:
        await store.aclose()


async def test_redis_start_cancellation_closes_temporary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def wait_for_connection() -> None:
        started.set()
        await asyncio.Event().wait()

    client = SimpleNamespace(ping=wait_for_connection, aclose=AsyncMock())
    monkeypatch.setattr(redis_store.Redis, "from_url", lambda *args, **kwargs: client)
    store = RedisJobStore("redis://localhost", 60, "test")
    startup = asyncio.create_task(store.start())
    await asyncio.wait_for(started.wait(), timeout=1)
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    client.aclose.assert_awaited_once()
    assert not await store.is_ready()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server is unavailable")
async def test_real_redis_shared_snapshots_and_expiry() -> None:
    # macOS limits Unix socket paths to approximately 104 bytes; pytest's
    # default temporary-directory path can exceed that limit.
    temporary = tempfile.TemporaryDirectory(prefix="fr-", dir="/tmp")
    socket_path = Path(temporary.name) / "redis.sock"
    process = await asyncio.create_subprocess_exec(
        cast(str, shutil.which("redis-server")),
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
    publisher = RedisJobStore(f"unix://{socket_path}", 0.05, "smoke")
    worker = RedisJobStore(f"unix://{socket_path}", 0.05, "smoke")
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
        await publisher.start()
        await worker.start()
        queued = JobSnapshot.queued(task_request())
        await publisher.save(queued)
        assert await worker.get(queued.job_id) == queued
        completed = replace(queued, status=JobStatus.SUCCEEDED, result={"value": 42})
        await worker.save(completed)
        assert await publisher.get(queued.job_id) == completed
        await asyncio.sleep(0.08)
        assert await publisher.get(queued.job_id) is None
    finally:
        await publisher.aclose()
        await worker.aclose()
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=3)
        temporary.cleanup()


def test_broker_factory_declares_durable_queues_without_opening_connections() -> None:
    broker = distributed.build_taskiq_broker(Settings(_env_file=None))
    assert broker.write_conn is None
    assert broker.read_conn is None
    assert broker._exchange.durable
    assert all(queue.durable for queue in broker._task_queues)
