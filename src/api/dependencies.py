from typing import Annotated

from fastapi import Depends, HTTPException, Request

from bootstrap.container import Runtime
from src.application.use_cases import JobService


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if not isinstance(runtime, Runtime):
        raise HTTPException(status_code=503, detail="Application is not ready")
    return runtime


RuntimeDependency = Annotated[Runtime, Depends(get_runtime)]


def get_job_service(runtime: RuntimeDependency) -> JobService:
    return runtime.jobs


JobServiceDependency = Annotated[JobService, Depends(get_job_service)]
