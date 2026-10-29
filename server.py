"""Application Factory, runtime lifespan, and the Uvicorn parent-process entry point."""

import argparse
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient, Limits, Timeout

from bootstrap.components import ModuleCatalog, ModuleDefinition
from bootstrap.config import load_settings
from bootstrap.container import Runtime, build_platform_services
from bootstrap.wiring import build_dispatcher, build_modules
from src.api.exceptions import register_error_handlers
from src.api.routers import mount_routes
from src.application.jobs.service import JobService
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.services.concurrency import BoundedThreadExecutor
from src.services.lifecycle import register_execution_cleanup
from src.services.observability import RequestContextMiddleware, configure_logging
from src.services.resource_manager import open_resources

logger = logging.getLogger("src.server")


def create_app(
    settings: Settings | None = None,
    *,
    modules: Sequence[ModuleDefinition] | None = None,
) -> FastAPI:
    config = settings or load_settings()
    catalog = ModuleCatalog(build_modules(config) if modules is None else modules)
    if config.environment == "production" and not config.identity_enabled:
        if any(
            definition.routers
            and definition.kind == "application"
            and not definition.allow_anonymous
            for definition in catalog.definitions
        ):
            raise ValueError("Protected application APIs require identity in production")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            config.log_level,
            service_name=config.app_name,
            environment=config.environment,
            role="api",
        )
        async with AsyncExitStack() as stack:
            # Shared resources outlive dispatchers that may drain work on shutdown.
            http_client = await stack.enter_async_context(
                AsyncClient(
                    timeout=Timeout(config.http_timeout_seconds),
                    limits=Limits(max_connections=100, max_keepalive_connections=20),
                )
            )
            resources = await open_resources(stack, config)
            registry = TaskRegistry()
            dispatcher = build_dispatcher(config, registry)
            jobs = JobService(dispatcher, registry)
            bindings = await catalog.start(
                stack,
                settings=config,
                http_client=http_client,
                role="api",
                resources=resources,
                registry=registry,
                jobs=jobs if config.task_backend != "disabled" else None,
            )
            kernels, exports = build_platform_services(config, bindings, resources)
            threads = resources.get("threads")
            register_execution_cleanup(
                stack,
                dispatcher=dispatcher,
                active_operations=kernels,
                threads=threads if isinstance(threads, BoundedThreadExecutor) else None,
            )
            await dispatcher.start()
            app.state.runtime = Runtime(
                config,
                registry,
                dispatcher,
                jobs,
                http_client,
                bindings.services,
                resources,
                kernels,
                exports,
            )
            logger.info("Runtime started pid=%s backend=%s", os.getpid(), config.task_backend)
            try:
                yield
            finally:
                app.state.runtime = None
                logger.info("Runtime stopping pid=%s", os.getpid())

    app = FastAPI(title=config.app_name, version="0.1.0", lifespan=lifespan)
    app.state.runtime = None
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    mount_routes(
        app,
        expose_job_api=config.expose_job_api,
        kernel_api_enabled=config.kernel_api_enabled,
        exports_api_enabled=config.exports_api_enabled,
        identity_enabled=config.identity_enabled,
        definitions=catalog.definitions,
        jobs_enabled=config.task_backend != "disabled",
    )
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--reload", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args(argv)
    overrides = {key: value for key, value in vars(args).items() if value is not None}
    config = load_settings(**overrides)
    # Spawned Uvicorn children import the Application Factory and read validated config.
    # Export only CLI overrides; keep secrets in their existing environment/.env.
    for key, value in overrides.items():
        os.environ[f"APP_{key.upper()}"] = str(value)
    uvicorn.run(
        "server:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        workers=config.workers,
        reload=config.reload,
        log_level=config.log_level,
        timeout_graceful_shutdown=int(config.shutdown_timeout_seconds) + 5,
    )


if __name__ == "__main__":
    main()
