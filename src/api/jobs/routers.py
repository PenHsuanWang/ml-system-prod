from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials

from src.api.auth import bearer, current_principal
from src.api.dependencies import JobServiceDependency, get_runtime
from src.api.jobs.schemas import JobResponse
from src.application.identity.policy import require_permission
from src.domain.identity.models import Principal
from src.domain.jobs.errors import JobNotFound

router = APIRouter(prefix="/jobs", tags=["jobs"])


async def job_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal | None:
    if not get_runtime(request).settings.identity_enabled:
        return None
    return await current_principal(request, credentials)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")],
    service: JobServiceDependency,
    principal: Annotated[Principal | None, Depends(job_principal)],
) -> JobResponse:
    if principal is not None:
        require_permission(principal, "jobs.read")
    snapshot = await service.get(job_id)
    if principal is not None and (
        snapshot.tenant_id != principal.tenant_id
        or (snapshot.owner_id != principal.user_id and "jobs.read_all" not in principal.permissions)
    ):
        raise JobNotFound("Job does not exist or access is not permitted")
    return JobResponse.model_validate(snapshot)
