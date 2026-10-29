"""A test-only echo module with no business rules or production registration."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from src.api.dependencies import JobServiceDependency
from src.api.jobs.schemas import JobResponse
from src.domain.jobs.models import Payload

router = APIRouter()


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int = Field(strict=True)


async def echo(payload: Payload) -> Payload:
    await asyncio.sleep(0)
    return payload


@router.post("/tasks", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_probe(
    body: ProbeRequest,
    response: Response,
    service: JobServiceDependency,
) -> JobResponse:
    job = await service.submit("probe.echo", body.model_dump())
    response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
    return JobResponse.model_validate(job)


@asynccontextmanager
async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
    yield ModuleExports(services={"role": context.role}, tasks={"echo": echo})


module = ModuleDefinition(name="probe", routers=(router,), lifespan=lifespan)
