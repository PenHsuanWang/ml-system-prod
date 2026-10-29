"""Explicitly disabled background capability without queues or external connections."""

from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.models import JobSnapshot, TaskRequest


class DisabledJobDispatcher:
    """Keep the lifecycle port while rejecting accidental background operations.

    Readiness means the selected configuration has started; it does not advertise
    job execution capacity. Bootstrap does not expose job routes in this mode.
    """

    def __init__(self) -> None:
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise DispatcherUnavailable("A closed dispatcher cannot be restarted")
        self._started = True

    async def aclose(self) -> None:
        self._closed = True

    async def is_ready(self) -> bool:
        return self._started and not self._closed

    async def dispatch(self, request: TaskRequest) -> None:
        raise DispatcherUnavailable("Background jobs are disabled")

    async def get(self, job_id: str) -> JobSnapshot | None:
        raise DispatcherUnavailable("Background jobs are disabled")
