"""Framework-independent task contracts shared by API and worker processes."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.shared_models.types import Payload as Payload


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TaskRequest:
    job_id: str
    name: str
    payload: Payload
    submitted_at: datetime
    tenant_id: str | None = None
    owner_id: str | None = None
    schema_version: int = 1
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    name: str
    status: JobStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Payload | None = None
    error: str | None = None
    attempts: int = 0
    tenant_id: str | None = None
    owner_id: str | None = None

    @classmethod
    def queued(cls, request: TaskRequest) -> "JobSnapshot":
        return cls(
            job_id=request.job_id,
            name=request.name,
            status=JobStatus.QUEUED,
            submitted_at=request.submitted_at,
            tenant_id=request.tenant_id,
            owner_id=request.owner_id,
        )
