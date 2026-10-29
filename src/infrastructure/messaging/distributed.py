"""RabbitMQ publishes work; Redis serves job state across Uvicorn processes."""

import asyncio
from contextlib import suppress
from typing import Any

from taskiq.kicker import AsyncKicker
from taskiq_aio_pika import AioPikaBroker, Exchange, Queue, QueueType

from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.models import JobSnapshot, TaskRequest
from src.domain.jobs.ports import TaskResolver
from src.infrastructure.messaging.envelope import encode_request
from src.infrastructure.messaging.redis_store import RedisJobStore

TASK_NAME = "backend.jobs.execute"


def build_taskiq_broker(
    settings: Settings, *, broker_type: type[AioPikaBroker] = AioPikaBroker
) -> AioPikaBroker:
    """Declare durable topology, with persistent messages and publisher confirms.

    AioPikaBroker publishes DeliveryMode.PERSISTENT messages and opens channels
    with aio-pika's default publisher_confirms=True. No connection happens here.
    """
    return broker_type(
        settings.rabbitmq_url.get_secret_value(),
        exchange=Exchange(name=f"{settings.queue_name}.exchange", durable=True),
        task_queues=[Queue(name=settings.queue_name, type=QueueType.QUORUM, durable=True)],
        dead_letter_queue=Queue(
            name=f"{settings.queue_name}.dead_letter",
            type=QueueType.QUORUM,
            durable=True,
        ),
        qos=settings.task_concurrency,
        timeout=5,
    )


class DistributedJobDispatcher:
    """Publisher adapter. The HTTP process never consumes distributed jobs."""

    def __init__(self, registry: TaskResolver, settings: Settings) -> None:
        self._registry = registry
        self._store = RedisJobStore(
            settings.redis_url.get_secret_value(),
            settings.job_retention_seconds,
            settings.queue_name,
        )
        self._broker = build_taskiq_broker(settings)
        self._started = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closed:
            raise DispatcherUnavailable("Task publisher has been closed")
        if self._started:
            return
        try:
            await self._store.start()
            await self._broker.startup()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception:
            with suppress(Exception):
                await self._broker.shutdown()
            with suppress(Exception):
                await self._store.aclose()
            raise DispatcherUnavailable("Distributed task services are unavailable") from None
        self._started = True

    async def aclose(self) -> None:
        self._closed = True
        self._started = False
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self) -> None:
        try:
            await self._broker.shutdown()
        except Exception:
            raise DispatcherUnavailable("Task publisher could not be closed") from None
        finally:
            await self._store.aclose()

    async def is_ready(self) -> bool:
        if not self._started or not await self._store.is_ready():
            return False
        connection = self._broker.write_conn
        return connection is not None and not connection.is_closed and connection.connected.is_set()

    async def dispatch(self, request: TaskRequest) -> None:
        self._registry.resolve(request.name)
        if not self._started:
            raise DispatcherUnavailable("Task publisher has not started")
        # Invalid local input never creates an orphan QUEUED record. Only an
        # actual publication failure has the ambiguous confirmation semantics.
        encoded_request = encode_request(request)
        await self._store.save(JobSnapshot.queued(request))
        try:
            kicker: AsyncKicker[[dict[str, Any]], dict[str, Any]] = AsyncKicker(
                TASK_NAME,
                self._broker,
                {"ack_type": "when_saved"},
            )
            await kicker.with_task_id(request.job_id).kiq(encoded_request)
        except Exception:
            # A confirm timeout is ambiguous: RabbitMQ may already have accepted
            # this message. Keep the queued snapshot instead of declaring failure.
            raise DispatcherUnavailable(
                f"Task submission could not be confirmed (job_id={request.job_id})"
            ) from None

    async def get(self, job_id: str) -> JobSnapshot | None:
        return await self._store.get(job_id)
