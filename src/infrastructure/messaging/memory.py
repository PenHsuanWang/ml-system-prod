"""Bounded, process-local dispatcher for development and isolated tests.

Every Uvicorn worker gets its own queue and records. Use the shared broker
adapter when dispatching and querying jobs across multiple server processes.
"""

import asyncio
import json
import logging
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from src.domain.jobs.errors import DispatcherUnavailable, QueueFull
from src.domain.jobs.models import JobSnapshot, JobStatus, Payload, TaskRequest
from src.domain.jobs.ports import TaskResolver
from src.infrastructure.messaging.envelope import encode_request
from src.shared_models.context import bind_job_context

_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
logger = logging.getLogger(__name__)


class MemoryJobDispatcher:
    """Fixed worker pool with immediate backpressure and bounded retention.

    Methods run on one event loop. Handlers must cooperate with asyncio
    cancellation; CPU-bound work belongs in the external worker deployment.
    """

    def __init__(
        self,
        registry: TaskResolver,
        *,
        concurrency: int = 4,
        queue_size: int = 100,
        retention_seconds: float = 3600,
        max_records: int = 1000,
        job_timeout_seconds: float = 60,
        shutdown_timeout_seconds: float = 10,
    ) -> None:
        for name, value in (
            ("concurrency", concurrency),
            ("queue_size", queue_size),
            ("max_records", max_records),
            ("retention_seconds", retention_seconds),
            ("job_timeout_seconds", job_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if shutdown_timeout_seconds < 0:
            raise ValueError("shutdown_timeout_seconds must not be negative")
        self._registry = registry
        self._concurrency = concurrency
        self._queue: asyncio.Queue[TaskRequest] = asyncio.Queue(maxsize=queue_size)
        self._retention_seconds = retention_seconds
        self._max_records = max_records
        self._job_timeout_seconds = job_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._records: dict[str, JobSnapshot] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closed:
            raise DispatcherUnavailable("A closed dispatcher cannot be restarted")
        if self._started:
            return
        self._workers = [
            asyncio.create_task(self._worker(), name=f"memory-job-worker-{index}")
            for index in range(self._concurrency)
        ]
        self._started = True

    async def is_ready(self) -> bool:
        return (
            self._started
            and not self._closed
            and all(not worker.done() for worker in self._workers)
        )

    async def dispatch(self, request: TaskRequest) -> None:
        if not await self.is_ready():
            raise DispatcherUnavailable("Job dispatcher is not accepting work")
        self._registry.resolve(request.name)
        # Apply the same admission contract as the external worker envelope,
        # before pruning records, evicting history, or inserting queued state.
        encode_request(request)
        self._prune_expired()
        if request.job_id in self._records:
            raise ValueError(f"Job already exists: {request.job_id}")
        if self._queue.full():
            raise QueueFull("The local job queue is full")

        # Copy before committing a record so invalid or uncopyable input cannot
        # leave a job behind. No await separates capacity checks and insertion.
        queued_request = replace(request, payload=deepcopy(request.payload))
        self._make_room()
        self._records[request.job_id] = JobSnapshot.queued(queued_request)
        try:
            self._queue.put_nowait(queued_request)
        except asyncio.QueueFull as exc:
            del self._records[request.job_id]
            raise QueueFull("The local job queue is full") from exc

    async def get(self, job_id: str) -> JobSnapshot | None:
        self._prune_expired()
        snapshot = self._records.get(job_id)
        return deepcopy(snapshot) if snapshot is not None else None

    async def aclose(self) -> None:
        # Stop admission before the first suspension. Shield only the cleanup;
        # cancellation of the caller still propagates immediately to its owner.
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown(), name="memory-job-shutdown")
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                await self._queue.join()
        except TimeoutError:
            pass
        finally:
            for worker in self._workers:
                worker.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            while True:
                try:
                    request = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    self._finish(
                        request.job_id,
                        JobStatus.CANCELLED,
                        error="Job cancelled during shutdown",
                    )
                finally:
                    self._queue.task_done()
            self._started = False

    async def _worker(self) -> None:
        while True:
            request = await self._queue.get()
            with bind_job_context(request.job_id, request.request_id):
                await self._execute(request)

    async def _execute(self, request: TaskRequest) -> None:
        logger.info("Job started")
        try:
            current = self._records[request.job_id]
            self._records[request.job_id] = replace(
                current,
                status=JobStatus.RUNNING,
                started_at=datetime.now(UTC),
                attempts=current.attempts + 1,
            )
            try:
                handler = self._registry.resolve(request.name)
                async with asyncio.timeout(self._job_timeout_seconds):
                    result = await handler(request.payload)
                result = deepcopy(result)
                if not isinstance(result, dict):
                    raise TypeError("Task result must be a JSON object")
                json.dumps(result, allow_nan=False)
                self._finish(
                    request.job_id,
                    JobStatus.SUCCEEDED,
                    result=result,
                )
            except asyncio.CancelledError:
                self._finish(
                    request.job_id,
                    JobStatus.CANCELLED,
                    error="Job cancelled",
                )
                raise
            except TimeoutError:
                self._finish(
                    request.job_id,
                    JobStatus.FAILED,
                    error=f"Job exceeded {self._job_timeout_seconds:g} seconds",
                )
            except Exception as exc:
                self._finish(
                    request.job_id,
                    JobStatus.FAILED,
                    error=f"Task failed ({type(exc).__name__})",
                )
        finally:
            self._queue.task_done()

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Payload | None = None,
        error: str | None = None,
    ) -> None:
        logger.info("Job finished status=%s", status.value)
        self._records[job_id] = replace(
            self._records[job_id],
            status=status,
            finished_at=datetime.now(UTC),
            result=result,
            error=error,
        )
        self._prune_expired()

    def _prune_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [
            job_id
            for job_id, snapshot in self._records.items()
            if snapshot.status in _TERMINAL
            and snapshot.finished_at is not None
            and (now - snapshot.finished_at).total_seconds() >= self._retention_seconds
        ]
        for job_id in expired:
            del self._records[job_id]

    def _make_room(self) -> None:
        if len(self._records) < self._max_records:
            return
        terminal = [snapshot for snapshot in self._records.values() if snapshot.status in _TERMINAL]
        if not terminal:
            raise QueueFull("The local job record capacity is full")
        oldest = min(terminal, key=lambda snapshot: snapshot.finished_at or snapshot.submitted_at)
        del self._records[oldest.job_id]
