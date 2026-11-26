"""Ingress validates the authoritative SQL route and fixed-model response identity."""

import asyncio
import os
from typing import Annotated

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import MLTraceMiddleware, capture_context
from src.api.ml_http import install_errors
from src.api.prediction_docs import (
    GATEWAY_DESCRIPTION,
    PREDICT_DESCRIPTION,
    PREDICT_EXAMPLES,
    PREDICTION_ERRORS,
    GatewayPredictionResult,
)
from src.api.ml_schemas import PredictionRequest
from src.api.ebm_schemas import EBMPredictionRequest
from bootstrap.ml_serving_wiring import endpoint_resolver
from src.domain.deployments.identity import matches_identity
from src.infrastructure.ml_identity import MongoActorResolver
from src.domain.ml import Conflict, Unavailable


def create_app(*, components=None, identity=None, resolver=None, transport=None) -> FastAPI:
    from contextlib import asynccontextmanager
    state = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = MLSettings.from_env()
        configure_telemetry("gateway")
        runtime = components or build_components(settings)
        endpoints = resolver or endpoint_resolver(settings)
        provider = identity or MongoActorResolver(os.environ["APP_MONGO_URI"],
                                      os.environ.get("APP_MONGO_DATABASE",
                                                     "backend"), os.environ["APP_JWT_SECRET"],
                                      os.environ.get("APP_JWT_ISSUER", "backend"), os.environ.get("APP_JWT_AUDIENCE", "backend-api"))
        async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=32),
                                     transport=transport, follow_redirects=False, trust_env=False) as client:
            state.update(components=runtime, resolver=endpoints, identity=provider, client=client)
            try:
                yield
            finally:
                if identity is None:
                    provider.close()
                if components is None:
                    runtime.database.close()

    bearer = HTTPBearer(
        auto_error=False, bearerFormat="JWT", description="Management Identity access_token"
    )
    app = FastAPI(title="ML prediction ingress", description=GATEWAY_DESCRIPTION, lifespan=lifespan)
    install_errors(app, maximum_body_bytes=8 * 1024 * 1024)
    app.add_middleware(MLTraceMiddleware)

    @app.get("/", include_in_schema=False)
    async def documentation(request: Request) -> RedirectResponse:
        return RedirectResponse(request.url_for("swagger_ui_html").path)

    @app.get("/health/live", summary="檢查 Gateway 程序是否存活")
    def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/health/ready", summary="檢查 Gateway 的 SQL 與身分服務連線",
             description="此檢查僅回報 Gateway 相依服務的狀態。個別模型的就緒狀態在預測時檢查。",
             response_model=dict[str, bool], responses={503: {"model": dict[str, bool]}})
    async def ready() -> JSONResponse:
        try:
            await asyncio.to_thread(state["components"].database.ready)
            await asyncio.to_thread(state["identity"].client.admin.command, "ping")
            return JSONResponse({"ready": True})
        except Exception:
            return JSONResponse({"ready": False}, status_code=503)

    @app.post("/api/v1/prediction/predict", summary="執行端點預測", description=PREDICT_DESCRIPTION,
              response_model=GatewayPredictionResult, responses=PREDICTION_ERRORS)
    async def predict(
        body: Annotated[PredictionRequest | EBMPredictionRequest, Body(openapi_examples=PREDICT_EXAMPLES)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> JSONResponse:
        from src.infrastructure.ml_identity import AuthenticationRequired
        if credentials is None or credentials.scheme != "Bearer":
            raise AuthenticationRequired("Authentication required")
        actor = await state["identity"].resolve(credentials.credentials)
        authorization = f"Bearer {credentials.credentials}"
        actor.require("ml:predict")
        route = await asyncio.to_thread(state["components"].deployments.route,
                                        actor.tenant_id, body.endpoint_id)
        if route.deployment_id is None:
            raise Unavailable("No active model")
        # Address is controller-owned, never taken from request input.
        try:
            deployment = await asyncio.to_thread(state["components"].deployments.get,
                                                  actor, route.deployment_id)
            if deployment.generation != route.generation or deployment.resources_released:
                raise Unavailable("Serving route identity mismatch")
            address = state["resolver"].resolve(deployment)
            health = await state["client"].get(address+"/health/ready")
            actual = health.json()
            if (health.status_code != 200 or not isinstance(actual, dict)
                    or actual.get("ready") is not True
                    or actual.get("tenant_id") != actor.tenant_id
                    or not matches_identity(deployment, actual)):
                raise Unavailable("Serving route identity mismatch")
            response = await state["client"].post(address+"/api/v1/prediction/predict",
                                                  json=body.model_dump(), headers={"Authorization": authorization, **capture_context()})
            value = response.json()
            if response.is_success and (not isinstance(value, dict)
                    or not matches_identity(deployment, value.get("provenance"))
                    or value["provenance"].get("pod_uid", "") != actual.get("pod_uid", "")):
                raise Unavailable("Serving response identity mismatch")
            current = await asyncio.to_thread(state["components"].deployments.route,
                                              actor.tenant_id, body.endpoint_id)
            if current != route or response.is_redirect:
                raise Unavailable("Serving route changed during prediction")
            return JSONResponse(value, status_code=response.status_code)
        except (httpx.HTTPError, OSError, ValueError, KeyError, Conflict):
            raise Unavailable("Serving generation is unavailable") from None

    return app
