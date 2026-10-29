"""At-least-once relay: leases are separate from network publication."""

from typing import Protocol

from src.application.training.ports import CommandPublisher
from src.domain.training.models import LeasedCommand


class OutboxRepository(Protocol):
    def lease(self, limit: int) -> tuple[LeasedCommand, ...]: ...
    def published(self, item: LeasedCommand) -> None: ...
    def retry(self, item: LeasedCommand) -> None: ...


class OutboxRelay:
    def __init__(self, repository: OutboxRepository, publisher: CommandPublisher) -> None:
        self.repository = repository
        self.publisher = publisher

    async def run_once(self, limit: int = 10) -> int:
        import asyncio
        items = await asyncio.to_thread(self.repository.lease, limit)
        published = 0
        for item in items:
            try:
                await self.publisher.publish(item.command)
            except Exception:
                await asyncio.to_thread(self.repository.retry, item)
            else:
                await asyncio.to_thread(self.repository.published, item)
                published += 1
        return published
