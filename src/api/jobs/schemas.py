from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.domain.jobs.models import JobStatus


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    name: str
    status: JobStatus
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    tenant_id: str | None = None
    owner_id: str | None = None
