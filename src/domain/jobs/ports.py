from typing import Protocol

from src.domain.jobs.models import JobSnapshot, Payload, TaskRequest


class TaskHandler(Protocol):
    async def __call__(self, payload: Payload) -> Payload: ...


class TaskResolver(Protocol):
    """Resolve explicitly registered handlers without exposing registry mutation."""

    def resolve(self, name: str) -> TaskHandler: ...


class JobDispatcher(Protocol):
    """A small port: application code never imports a concrete queue library."""

    async def start(self) -> None: ...

    async def aclose(self) -> None: ...

    async def is_ready(self) -> bool: ...

    async def dispatch(self, request: TaskRequest) -> None: ...

    async def get(self, job_id: str) -> JobSnapshot | None: ...
