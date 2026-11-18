"""Manual acknowledgement follows SQL terminal evidence and a technical snapshot."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Protocol

from src.domain.training.models import TERMINAL, Job, TrainingCommand


@dataclass(frozen=True)
class AcknowledgementSnapshot:
    command_id: str
    job_id: str
    attempt_id: str
    spec_digest: str
    state: str
    revision: int


class DeliveryRepository(Protocol):
    def validate_delivery(self, command: TrainingCommand) -> Job: ...


class SnapshotStore(Protocol):
    async def persist(self, snapshot: AcknowledgementSnapshot) -> None: ...


async def acknowledge_terminal(
    repository: DeliveryRepository,
    command: TrainingCommand,
    store: SnapshotStore,
    ack: Callable[[], Awaitable[None]],
) -> dict[str, object] | None:
    job = await asyncio.to_thread(repository.validate_delivery, command)
    if job.state not in TERMINAL:
        return None
    snapshot = AcknowledgementSnapshot(
        command.command_id, job.job_id, job.attempt_id, job.spec_digest, job.state, job.revision
    )
    await store.persist(snapshot)
    await ack()
    return asdict(snapshot)
