"""Management factory. ML is explicitly selected without changing server defaults."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import MLTraceMiddleware
from src.api.ml_http import ActorDependency, dataset_router, deployment_router, install_errors, mount_management, registry_router, training_router
from src.application.ml_services import ActorResolver, MLServices
from src.domain.ml import Actor


def create_app(*, services: MLServices | None = None,
               identity: ActorResolver | None = None) -> FastAPI:
    if services is not None:
        if identity is None:
            raise ValueError("An explicit identity provider is required")
        app = FastAPI(title="ML Management")
        app.state.ml_services, app.state.ml_identity = services, identity
        mount_management(app)
        app.add_middleware(MLTraceMiddleware)
        return app

    # Imports stay inside the selected production factory; generic startup is unchanged.
    from bootstrap.components import ModuleDefinition
    from bootstrap.config import load_settings
    from bootstrap.wiring import build_modules
    from server import create_app as template_factory
    config = load_settings()
    if not config.identity_enabled:
        raise ValueError("ML APIs require APP_IDENTITY_ENABLED=true")
    components = build_components(MLSettings.from_env())
    definitions = [*build_modules(config),
                   ModuleDefinition("training", requires=("identity",), routers=(training_router,)),
                   ModuleDefinition("model_registry", requires=(
                       "training",), routers=(registry_router,)),
                   ModuleDefinition("deployments", requires=(
                       "training",), routers=(deployment_router,)),
                   ModuleDefinition("datasets", requires=("identity",), routers=(dataset_router,))]
    app = template_factory(config, modules=definitions)
    original_lifespan = app.router.lifespan_context

    class TemplateIdentity:
        async def resolve(self, token: str) -> Actor:
            service = app.state.runtime.modules["identity"]
            principal = await service.authenticate(token)
            return Actor.from_identity(principal.tenant_id, principal.user_id, principal.permissions)

    @asynccontextmanager
    async def lifespan(current: FastAPI):
        async with original_lifespan(current):
            configure_telemetry("management")
            current.state.ml_services = components.services
            current.state.ml_identity = TemplateIdentity()
            try:
                yield
            finally:
                components.database.close()

    app.router.lifespan_context = lifespan
    install_errors(app)
    app.add_middleware(MLTraceMiddleware)
    # Replace the generic readiness probe with the ML role's dependency checks.
    app.router.routes = [route for route in app.router.routes
                         if getattr(route, "path", None) != "/health/ready"]

    @app.get("/health/ready")
    async def ready():
        import asyncio
        try:
            await asyncio.to_thread(components.database.ready)
            mongo = app.state.runtime.resources.get("mongodb")
            if mongo is None or not await mongo.is_ready():
                return JSONResponse(status_code=503, content={"ready": False})
        except Exception:
            return JSONResponse(status_code=503, content={"ready": False})
        return {"ready": True}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(actor: ActorDependency):
        from src.infrastructure.ml_metrics import SQLMetricsReader
        actor.require("ml:operate")
        return SQLMetricsReader(components.database).render()
    return app
