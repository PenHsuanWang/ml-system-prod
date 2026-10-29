from collections.abc import Mapping
from contextlib import AsyncExitStack
from types import MappingProxyType

from src.config.settings import Settings
from src.infrastructure.locking.resources import ResourceCoordinator
from src.infrastructure.persistence.mongo import MongoResource
from src.services.concurrency import BoundedThreadExecutor


async def open_resources(stack: AsyncExitStack, settings: Settings) -> Mapping[str, object]:
    resources: dict[str, object] = {}
    threads = BoundedThreadExecutor(settings)
    stack.push_async_callback(threads.aclose)
    await threads.start()
    resources["threads"] = threads
    if settings.mongo_enabled:
        mongodb = MongoResource(settings)
        stack.push_async_callback(mongodb.aclose)
        await mongodb.start()
        resources["mongodb"] = mongodb
    coordinator = ResourceCoordinator(settings)
    stack.push_async_callback(coordinator.aclose)
    await coordinator.start()
    resources["leases"] = coordinator
    return MappingProxyType(resources)
