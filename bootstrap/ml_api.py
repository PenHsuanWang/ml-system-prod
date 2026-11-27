"""Management factory. ML is explicitly selected without changing server defaults."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from bootstrap.ml_config import MLSettings
from bootstrap.ml_telemetry import configure_telemetry
from bootstrap.ml_wiring import build_components
from src.api.ingestion.source_routers import router as source_router
from src.api.ml_docs import install_ml_docs
from src.api.ml_http import (
    ActorDependency,
    dataset_router,
    deployment_router,
    install_errors,
    mount_management,
    registry_router,
    training_router,
)
from src.api.ml_platform import (
    CATALOG,
    DATASET_BUILD,
    GOVERNANCE,
    INGESTION,
    MANAGEMENT,
    METRICS,
    SOURCE_MANAGEMENT,
    install_platform_audit,
    install_platform_discovery,
)
from src.application.catalog.service import CatalogService
from src.application.ingestion.service import IngestionService
from src.application.ingestion.source_management import SourceManagementService
from src.application.ml_services import ActorResolver, MLServices
from src.domain.ml import Actor
from src.infrastructure.ml_tracing import MLTraceMiddleware


def _install_portal(
    app: FastAPI,
    services: MLServices,
    *,
    ingestion: bool,
    source_management: bool,
    catalog: bool,
    production_factory: bool,
) -> None:
    app.description = (
        "Discover and govern data, models and deployments. Use "
        "`GET /api/v1/platform/info` for sign-in instructions and "
        "`GET /api/v1/platform/capabilities` for your workflow entry points. "
        "Use a Bearer access token from the deployment's trusted Identity URL. "
        "Use the Prediction Gateway for endpoint discovery and inference."
    )
    entries = MANAGEMENT
    if ingestion:
        entries += INGESTION
    if source_management:
        entries += (SOURCE_MANAGEMENT,)
    if catalog:
        entries += (CATALOG,)
    if services.dataset_builds is not None:
        entries += (DATASET_BUILD,)
    if services.governance is not None:
        entries += GOVERNANCE
        install_platform_audit(app)
    if production_factory:
        entries += (METRICS,)
    install_ml_docs(app)
    install_platform_discovery(app, "management", entries, local_identity=production_factory)


def create_app(
    *,
    services: MLServices | None = None,
    identity: ActorResolver | None = None,
    ingestion_service: IngestionService | None = None,
    source_management: SourceManagementService | None = None,
    catalog_service: CatalogService | None = None,
) -> FastAPI:
    if services is not None:
        if identity is None:
            raise ValueError("An explicit identity provider is required")
        app: FastAPI = FastAPI(title="ML Management", docs_url=None, redoc_url=None)
        app.state.ml_services, app.state.ml_identity = services, identity
        mount_management(app)
        if ingestion_service is not None:
            from src.api.ingestion.routers import router as ingestion_router

            app.state.ingestion_service = ingestion_service
            app.include_router(ingestion_router, prefix="/api/v1/ingestion")
        if source_management is not None:
            app.state.source_management = source_management
            app.include_router(source_router, prefix="/api/v1/ingestion")
        if catalog_service is not None:
            from src.api.catalog.routers import router as catalog_router

            app.state.catalog_service = catalog_service
            app.include_router(catalog_router, prefix="/api/v1/catalog")
        _install_portal(
            app,
            services,
            ingestion=ingestion_service is not None,
            source_management=source_management is not None,
            catalog=catalog_service is not None,
            production_factory=False,
        )
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
    from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion, ingress_enabled

    ingress = (
        build_ingestion(IngestionSettings.from_env(), database=components.database)
        if ingress_enabled()
        else None
    )
    from bootstrap.catalog_wiring import build_catalog
    from src.api.catalog.routers import router as catalog_router

    catalog = build_catalog()
    from src.api.datasets.build_jobs import router as dataset_build_router
    from src.api.datasets.routers import router as dataset_discovery_router
    from src.api.governance_discovery import deployment_router as deployment_discovery_router
    from src.api.governance_discovery import registry_router as registry_discovery_router
    from src.api.training_discovery import router as training_discovery_router

    definitions = [
        *build_modules(config),
        ModuleDefinition(
            "training", requires=("identity",), routers=(training_router, training_discovery_router)
        ),
        ModuleDefinition(
            "model_registry",
            requires=("training",),
            routers=(registry_discovery_router, registry_router),
        ),
        ModuleDefinition(
            "deployments",
            requires=("training",),
            routers=(deployment_discovery_router, deployment_router),
        ),
        ModuleDefinition(
            "datasets",
            requires=("identity",),
            routers=(dataset_discovery_router, dataset_build_router, dataset_router),
        ),
    ]
    if ingress is not None:
        from src.api.ingestion.routers import router as ingestion_router

        definitions.append(
            ModuleDefinition(
                "ingestion", requires=("identity",), routers=(ingestion_router, source_router)
            )
        )
    if catalog is not None:
        definitions.append(
            ModuleDefinition("catalog", requires=("identity",), routers=(catalog_router,))
        )
    app = template_factory(
        config, modules=definitions, include_default_readiness=False, include_default_docs=False
    )
    original_lifespan = app.router.lifespan_context

    class TemplateIdentity:
        async def resolve(self, token: str) -> Actor:
            service = app.state.runtime.modules["identity"]
            principal = await service.authenticate(token)
            return Actor.from_identity(
                principal.tenant_id, principal.user_id, principal.permissions
            )

    @asynccontextmanager
    async def lifespan(current: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(current):
            configure_telemetry("management")
            current.state.ml_services = components.services
            current.state.ml_identity = TemplateIdentity()
            if catalog is not None:
                current.state.catalog_service = catalog
            if ingress is not None:
                current.state.ingestion_service = ingress.service
                current.state.source_management = ingress.source_management
            try:
                yield
            finally:
                components.database.close()

    app.router.lifespan_context = lifespan
    install_errors(app)
    app.add_middleware(MLTraceMiddleware)

    async def ready() -> dict[str, bool] | JSONResponse:
        import asyncio

        try:
            await asyncio.to_thread(components.database.ready)
            mongo = app.state.runtime.resources.get("mongodb")
            if mongo is None or not await mongo.is_ready():
                return JSONResponse(status_code=503, content={"ready": False})
        except Exception:
            return JSONResponse(status_code=503, content={"ready": False})
        return {"ready": True}

    def metrics(actor: ActorDependency) -> str:
        from src.infrastructure.ml_metrics import SQLMetricsReader

        actor.require("ml:operate")
        rendered: str = SQLMetricsReader(components.database).render()
        if ingress is not None:
            from src.infrastructure.ingestion.persistence.metrics import SQLIngestionMetricsReader

            rendered += SQLIngestionMetricsReader(components.database).render()
        return rendered

    app.add_api_route("/health/ready", ready, methods=["GET"], response_model=None)
    app.add_api_route("/metrics", metrics, methods=["GET"], response_class=PlainTextResponse)
    _install_portal(
        app,
        components.services,
        ingestion=ingress is not None,
        source_management=ingress is not None,
        catalog=catalog is not None,
        production_factory=True,
    )
    return app
