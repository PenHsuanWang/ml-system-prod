from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.api.auth import PrincipalDependency
from src.api.dependencies import RuntimeDependency
from src.api.jobs.schemas import JobResponse

router = APIRouter(prefix="/kernels", tags=["kernels"])
jobs_router = APIRouter(prefix="/kernels", tags=["kernels"])


class KernelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    input: dict[str, Any]


@router.get("")
async def list_kernels(
    runtime: RuntimeDependency, principal: PrincipalDependency
) -> list[dict[str, Any]]:
    if runtime.kernels is None:
        raise HTTPException(503, "Kernel services are unavailable")
    return runtime.kernels.list_kernels(principal)


@router.post("/{name}/execute")
async def execute_kernel(
    name: str,
    body: KernelInput,
    request: Request,
    runtime: RuntimeDependency,
    principal: PrincipalDependency,
) -> dict[str, Any]:
    if runtime.kernels is None:
        raise HTTPException(503, "Kernel services are unavailable")
    return await runtime.kernels.execute(
        name, body.version, body.input, principal, request.state.request_id
    )


@jobs_router.post("/{name}/runs", response_model=JobResponse, status_code=202)
async def queue_kernel(
    name: str,
    body: KernelInput,
    request: Request,
    response: Response,
    runtime: RuntimeDependency,
    principal: PrincipalDependency,
) -> JobResponse:
    if runtime.kernels is None:
        raise HTTPException(503, "Kernel services are unavailable")
    runtime.kernels.validate_request(name, body.version, body.input, principal)
    job = await runtime.jobs.submit(
        "platform.kernel.execute",
        {
            "kernel": name,
            "version": body.version,
            "input": body.input,
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "request_id": request.state.request_id,
        },
        tenant_id=principal.tenant_id,
        owner_id=principal.user_id,
    )
    response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
    return JobResponse.model_validate(job)
