"""Process readiness checks, independent of HTTP transport and application workflows."""

import asyncio
from collections.abc import Mapping

from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.ports import JobDispatcher
from src.infrastructure.locking.resources import ResourceCoordinator
from src.infrastructure.persistence.mongo import MongoResource


async def check_readiness(dispatcher: JobDispatcher, resources: Mapping[str, object]) -> bool:
    """Check the dispatcher and managed storage/lease resources within three seconds."""
    try:
        async with asyncio.timeout(3):
            healthy = await dispatcher.is_ready()
            for resource in resources.values():
                if isinstance(resource, (MongoResource, ResourceCoordinator)):
                    healthy = await resource.is_ready() and healthy
    except (DispatcherUnavailable, OSError, TimeoutError):
        return False
    return healthy
