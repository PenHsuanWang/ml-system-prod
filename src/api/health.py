from fastapi import APIRouter, Response

from src.api.dependencies import RuntimeDependency
from src.api.schemas import HealthResponse
from src.services.health import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
async def ready(runtime: RuntimeDependency, response: Response) -> HealthResponse:
    if not await check_readiness(runtime.dispatcher, runtime.resources):
        response.status_code = 503
        return HealthResponse(status="unavailable")
    return HealthResponse(status="ready")
