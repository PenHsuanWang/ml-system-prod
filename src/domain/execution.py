import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol


class ExecutionBusy(Exception):
    pass


class ResourceBusy(Exception):
    pass


class ResourceUnavailable(Exception):
    pass


class ResourceLeaseLost(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ResourceLease:
    resource: str
    fencing_token: int
    lost: asyncio.Event = field(default_factory=asyncio.Event)


class CapacityCoordinator(Protocol):
    def lease(
        self, name: str, *, wait_seconds: float
    ) -> AbstractAsyncContextManager[ResourceLease]: ...
