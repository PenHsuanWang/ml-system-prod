"""Dedicated async worker composition and its persistence acknowledgement gate."""

import asyncio
import json
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Any

from httpx import AsyncClient, Limits, Timeout
from taskiq import (
    Context,
    TaskiqDepends,
    TaskiqEvents,
    TaskiqMessage,
    TaskiqMiddleware,
    TaskiqResult,
    TaskiqState,
)
from taskiq_aio_pika import AioPikaBroker

from bootstrap.components import ModuleCatalog, ModuleDefinition
from bootstrap.container import build_platform_services
from bootstrap.wiring import build_modules
from src.application.jobs.service import JobService
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.models import JobSnapshot, JobStatus, TaskRequest
from src.infrastructure.messaging.distributed import (
    TASK_NAME,
    DistributedJobDispatcher,
    build_taskiq_broker,
)
from src.infrastructure.messaging.redis_store import (
    RedisJobStore,
    decode_request,
    decode_snapshot,
    encode_snapshot,
)
from src.services.concurrency import BoundedThreadExecutor
from src.services.lifecycle import register_execution_cleanup
from src.services.observability import configure_logging
from src.services.resource_manager import open_resources
from src.shared_models.context import bind_job_context

TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
logger = logging.getLogger(__name__)


async def execute_request(
    request: TaskRequest,
    registry: TaskRegistry,
    store: RedisJobStore,
    timeout_seconds: float,
) -> dict[str, Any]:
    with bind_job_context(request.job_id, request.request_id):
        return await _execute_request(request, registry, store, timeout_seconds)


async def _execute_request(
    request: TaskRequest,
    registry: TaskRegistry,
    store: RedisJobStore,
    timeout_seconds: float,
) -> dict[str, Any]:
    previous = await store.get(request.job_id)
    if previous is not None and previous.status in TERMINAL_STATUSES:
        # A worker can crash after committing a terminal snapshot but before ack.
        logger.info("Completed job redelivered status=%s", previous.status.value)
        return encode_snapshot(previous)
    running = replace(
        previous or JobSnapshot.queued(request),
        status=JobStatus.RUNNING,
        started_at=datetime.now(UTC),
        finished_at=None,
        error=None,
        result=None,
        attempts=(previous.attempts if previous is not None else 0) + 1,
    )
    await store.save(running)
    logger.info("Job started attempt=%s", running.attempts)
    try:
        handler = registry.resolve(request.name)
        async with asyncio.timeout(timeout_seconds):
            result = await handler(request.payload)
        if not isinstance(result, dict):
            raise TypeError("Task results must be JSON objects")
        json.dumps(result, allow_nan=False)
    except asyncio.CancelledError:
        # Shutdown must leave work unacknowledged so RabbitMQ can redeliver it.
        raise
    except TimeoutError:
        finished = replace(running, status=JobStatus.FAILED, error="Task timed out")
    except Exception as exc:
        # Exception text can contain credentials or customer data.
        finished = replace(
            running,
            status=JobStatus.FAILED,
            error=f"Task failed ({type(exc).__name__})",
        )
    else:
        finished = replace(running, status=JobStatus.SUCCEEDED, result=result)
    logger.info("Job execution finished status=%s", finished.status.value)
    return encode_snapshot(replace(finished, finished_at=datetime.now(UTC)))


async def execute_job(
    request_data: dict[str, Any],
    context: Annotated[Context, TaskiqDepends()],
) -> dict[str, Any]:
    request = decode_request(request_data)
    with bind_job_context(request.job_id, request.request_id):
        return await _execute_job(request, context)


async def _execute_job(request: TaskRequest, context: Context) -> dict[str, Any]:
    warned = False
    while True:
        try:
            return await execute_request(
                request,
                context.state.registry,
                context.state.store,
                context.state.settings.job_timeout_seconds,
            )
        except DispatcherUnavailable:
            if not warned:
                logger.warning("Job storage unavailable; retaining unacknowledged delivery")
                warned = True
            # Hold bounded worker capacity while Redis recovers. Cancellation
            # still propagates, and RabbitMQ redelivers after the channel closes.
            await asyncio.sleep(1)


class SnapshotPersistenceMiddleware(TaskiqMiddleware):
    """Save state before Taskiq reaches its acknowledgement code.

    Taskiq's default receiver catches result-backend save errors and can still
    acknowledge the message. post_execute runs before that catch and retries
    until state is durable; cancellation escapes without acknowledging delivery.
    """

    def __init__(self, store: RedisJobStore, retry_delay_seconds: float = 1) -> None:
        self._store = store
        self._retry_delay_seconds = retry_delay_seconds

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        request = decode_request(message.args[0])
        with bind_job_context(request.job_id, request.request_id):
            await self._persist_result(request, result)

    async def _persist_result(self, request: TaskRequest, result: TaskiqResult[Any]) -> None:
        if result.is_err:
            if isinstance(result.error, (DispatcherUnavailable, asyncio.CancelledError)):
                raise result.error
            previous = await self._store.get(request.job_id)
            snapshot = replace(
                previous or JobSnapshot.queued(request),
                status=JobStatus.FAILED,
                finished_at=datetime.now(UTC),
                error="Task execution failed",
            )
        else:
            snapshot = decode_snapshot(result.return_value)
        warned = False
        while True:
            try:
                await self._store.save(snapshot)
                return
            except DispatcherUnavailable:
                if not warned:
                    logger.warning(
                        "Job result storage unavailable; retaining unacknowledged delivery"
                    )
                    warned = True
                # Persist the already computed result after recovery, without
                # repeating handler side effects or losing the delivery.
                await asyncio.sleep(self._retry_delay_seconds)


class LifespanWorkerBroker(AioPikaBroker):
    async def startup(self) -> None:
        try:
            await super().startup()
        except BaseException:
            # Taskiq startup hooks run before AMQP connects. Roll their resources
            # back if a later connection or topology declaration fails.
            with suppress(Exception):
                await self.shutdown()
            raise


def build_worker_broker(
    settings: Settings, *, modules: Sequence[ModuleDefinition] | None = None
) -> AioPikaBroker:
    """Each worker process creates its own resources during worker startup."""
    if settings.task_backend != "distributed":
        raise ValueError("Task workers require the distributed task backend")
    catalog = ModuleCatalog(build_modules(settings) if modules is None else modules)
    broker = build_taskiq_broker(settings, broker_type=LifespanWorkerBroker)
    store = RedisJobStore(
        settings.redis_url.get_secret_value(),
        settings.job_retention_seconds,
        settings.queue_name,
    )
    broker.with_middlewares(SnapshotPersistenceMiddleware(store))
    broker.register_task(execute_job, task_name=TASK_NAME, ack_type="when_saved")

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def startup(state: TaskiqState) -> None:
        configure_logging(
            settings.log_level,
            service_name=settings.app_name,
            environment=settings.environment,
            role="worker",
        )
        stack = AsyncExitStack()
        state.module_stack = stack
        try:
            stack.push_async_callback(store.aclose)
            await store.start()
            http_client = await stack.enter_async_context(
                AsyncClient(
                    timeout=Timeout(settings.http_timeout_seconds),
                    limits=Limits(max_connections=100, max_keepalive_connections=20),
                )
            )
            resources = await open_resources(stack, settings)
            registry = TaskRegistry()
            dispatcher = DistributedJobDispatcher(registry, settings)
            jobs = JobService(dispatcher, registry)
            bindings = await catalog.start(
                stack,
                settings=settings,
                http_client=http_client,
                role="worker",
                resources=resources,
                registry=registry,
                jobs=jobs,
            )
            if bindings.kernels and settings.resource_backend != "redis":
                raise ValueError("Task-worker kernels require Redis resource coordination")
            kernels, exports = build_platform_services(settings, bindings, resources)
            threads = resources.get("threads")
            register_execution_cleanup(
                stack,
                dispatcher=dispatcher,
                active_operations=kernels,
                threads=threads if isinstance(threads, BoundedThreadExecutor) else None,
            )
            await dispatcher.start()
            state.registry = bindings.registry
            state.modules = bindings.services
            state.resources = resources
            state.kernels = kernels
            state.exports = exports
            state.jobs = jobs
            state.store = store
            state.settings = settings
        except BaseException:
            await stack.aclose()
            raise

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def shutdown(state: TaskiqState) -> None:
        stack = getattr(state, "module_stack", None)
        if stack is not None:
            await stack.aclose()

    return broker
