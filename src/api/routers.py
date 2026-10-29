"""Mount inbound HTTP routes from explicit, transport-facing route group contracts."""

from collections.abc import Sequence
from typing import Literal, Protocol

from fastapi import APIRouter, Depends, FastAPI

from src.api.auth import current_principal
from src.api.health import router as health_router
from src.api.jobs.routers import router as jobs_router


class RouteGroup(Protocol):
    """Read-only routing metadata supplied by the outer composition root."""

    @property
    def name(self) -> str: ...

    @property
    def routers(self) -> Sequence[APIRouter]: ...

    @property
    def kind(self) -> Literal["application", "platform"]: ...

    @property
    def allow_anonymous(self) -> bool: ...


def mount_routes(
    app: FastAPI,
    *,
    expose_job_api: bool,
    kernel_api_enabled: bool,
    exports_api_enabled: bool,
    identity_enabled: bool,
    definitions: Sequence[RouteGroup],
    jobs_enabled: bool = True,
) -> None:
    """Apply the platform's existing URL and application authentication policy."""
    app.include_router(health_router)
    if jobs_enabled and (expose_job_api or kernel_api_enabled):
        app.include_router(jobs_router, prefix="/api/v1")
    if kernel_api_enabled:
        from src.api.kernels.routers import router as kernels_router

        app.include_router(kernels_router, prefix="/api/v1")
        if jobs_enabled:
            from src.api.kernels.routers import jobs_router as kernel_jobs_router

            app.include_router(kernel_jobs_router, prefix="/api/v1")
    if exports_api_enabled:
        from src.api.exports.routers import router as exports_router

        app.include_router(exports_router, prefix="/api/v1")
    for definition in definitions:
        for router in definition.routers:
            dependencies = []
            if (
                identity_enabled
                and definition.kind == "application"
                and not definition.allow_anonymous
            ):
                dependencies.append(Depends(current_principal))
            app.include_router(
                router, prefix=f"/api/v1/{definition.name}", dependencies=dependencies
            )
