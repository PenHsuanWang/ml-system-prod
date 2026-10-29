from datetime import UTC, datetime
from uuid import uuid4

from src.domain.jobs.errors import JobNotFound
from src.domain.jobs.models import JobSnapshot, Payload, TaskRequest
from src.domain.jobs.ports import JobDispatcher, TaskResolver
from src.shared_models.context import current_request_id


class JobService:
    def __init__(self, dispatcher: JobDispatcher, registry: TaskResolver) -> None:
        self._dispatcher = dispatcher
        self._registry = registry

    async def submit(
        self,
        name: str,
        payload: Payload,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> JobSnapshot:
        self._registry.resolve(name)
        if (tenant_id is None) != (owner_id is None):
            raise ValueError("Job ownership requires both tenant_id and owner_id")
        request = TaskRequest(
            uuid4().hex,
            name,
            payload,
            datetime.now(UTC),
            tenant_id,
            owner_id,
            request_id=current_request_id(),
        )
        await self._dispatcher.dispatch(request)
        return JobSnapshot.queued(request)

    async def get(self, job_id: str) -> JobSnapshot:
        job = await self._dispatcher.get(job_id)
        if job is None:
            raise JobNotFound("Job does not exist or its retention period has expired")
        return job
