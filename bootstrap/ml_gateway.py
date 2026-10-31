"""Reference local/VM ingress: route reads reflect the authoritative fenced revision."""

import asyncio
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import MLTraceMiddleware, capture_context
from src.api.ml_http import install_errors
from src.api.ml_schemas import PredictionRequest
from src.api.ebm_schemas import EBMPredictionRequest
from src.infrastructure.deployment.local_serving import LocalServingExecutor
from src.infrastructure.ml_identity import MongoActorResolver
from src.domain.ml import Unavailable


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager
    state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = MLSettings.from_env()
        configure_telemetry("gateway")
        components = build_components(settings)
        executor = LocalServingExecutor(settings.execution_root/"serving")
        identity = MongoActorResolver(os.environ["APP_MONGO_URI"],
                                      os.environ.get("APP_MONGO_DATABASE",
                                                     "backend"), os.environ["APP_JWT_SECRET"],
                                      os.environ.get("APP_JWT_ISSUER", "backend"), os.environ.get("APP_JWT_AUDIENCE", "backend-api"))
        async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=32)) as client:
            state.update(components=components, executor=executor, identity=identity, client=client)
            try:
                yield
            finally:
                identity.close()
                components.database.close()

    app = FastAPI(title="ML prediction ingress", lifespan=lifespan)
    install_errors(app, maximum_body_bytes=8 * 1024 * 1024)
    app.add_middleware(MLTraceMiddleware)

    @app.get("/health/live")
    def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        try:
            await asyncio.to_thread(state["components"].database.ready)
            await asyncio.to_thread(state["identity"].client.admin.command, "ping")
            return JSONResponse({"ready": True})
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)

    @app.post("/api/v1/prediction/predict")
    async def predict(body: PredictionRequest | EBMPredictionRequest, request: Request) -> JSONResponse:
        from src.infrastructure.ml_identity import AuthenticationRequired
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthenticationRequired("Authentication required")
        actor = await state["identity"].resolve(authorization[7:])
        actor.require("ml:predict")
        route = await asyncio.to_thread(state["components"].deployments.route,
                                        actor.tenant_id, body.endpoint_id)
        if route.deployment_id is None:
            raise Unavailable("No active model")
        # Address is controller-owned, never taken from request input.
        try:
            address = state["executor"].address(route.deployment_id)
            response = await state["client"].post(address+"/api/v1/prediction/predict",
                                                  json=body.model_dump(), headers={"Authorization": authorization, **capture_context()})
            return JSONResponse(response.json(), status_code=response.status_code)
        except (httpx.HTTPError, OSError, ValueError):
            raise Unavailable("Serving generation is unavailable") from None

    return app
