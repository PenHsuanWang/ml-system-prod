"""Background capability is optional without weakening production security."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import server
from bootstrap import worker as taskiq_worker
from bootstrap.components import ModuleBindings, ModuleContext, ModuleDefinition, ModuleExports
from bootstrap.container import build_platform_services
from src.api.routers import mount_routes
from src.application.identity.facade import IdentityServices
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable, UnknownTask
from src.domain.jobs.models import TaskRequest
from src.infrastructure.locking.resources import ResourceCoordinator
from src.infrastructure.messaging.disabled import DisabledJobDispatcher


def disabled_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "environment": "production",
            "task_backend": "disabled",
            "workers": 4,
            "reload": False,
            **overrides,
        },
    )


async def test_disabled_dispatcher_lifecycle_rejects_all_job_operations() -> None:
    dispatcher = DisabledJobDispatcher()
    assert not await dispatcher.is_ready()
    await dispatcher.start()
    await dispatcher.start()
    assert await dispatcher.is_ready()
    request = TaskRequest("0" * 32, "probe.echo", {}, datetime.now(UTC))
    with pytest.raises(DispatcherUnavailable, match="disabled"):
        await dispatcher.dispatch(request)
    with pytest.raises(DispatcherUnavailable, match="disabled"):
        await dispatcher.get(request.job_id)
    await dispatcher.aclose()
    await dispatcher.aclose()
    assert not await dispatcher.is_ready()
    with pytest.raises(DispatcherUnavailable, match="cannot be restarted"):
        await dispatcher.start()


async def test_production_rest_only_lifespans_need_no_network_or_job_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_connection(*args: object, **kwargs: object) -> None:
        pytest.fail("REST-only startup must not connect to a broker, Redis or MongoDB")

    monkeypatch.setattr(socket.socket, "connect", forbid_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_connection)
    contexts: list[ModuleContext] = []
    router = APIRouter()

    @router.get("/status")
    async def status() -> dict[str, bool]:
        return {"ok": True}

    @asynccontextmanager
    async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        contexts.append(context)
        assert context.jobs is None
        yield ModuleExports(services="rest-only")

    definition = ModuleDefinition(
        "probe", routers=(router,), lifespan=lifespan, allow_anonymous=True
    )
    first = server.create_app(disabled_settings(), modules=(definition,))
    second = server.create_app(disabled_settings(), modules=(definition,))
    tasks_before = asyncio.all_tasks()
    async with AsyncExitStack() as stack:
        for app in (first, second):
            await stack.enter_async_context(app.router.lifespan_context(app))
            assert isinstance(app.state.runtime.dispatcher, DisabledJobDispatcher)
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                assert (await client.get("/health/ready")).status_code == 200
                assert (await client.get("/api/v1/probe/status")).json() == {"ok": True}
                assert (await client.get(f"/api/v1/jobs/{'0' * 32}")).status_code == 404
            with pytest.raises(DispatcherUnavailable, match="disabled"):
                await app.state.runtime.jobs.get("0" * 32)
        assert not [
            task
            for task in asyncio.all_tasks() - tasks_before
            if task.get_name().startswith("memory-job-worker-")
        ]
    assert len(contexts) == 2
    assert contexts[0].http_client is not contexts[1].http_client
    assert all(context.http_client.is_closed for context in contexts)
    assert first.state.runtime is second.state.runtime is None


def test_disabled_background_does_not_bypass_production_router_security() -> None:
    router = APIRouter()

    @router.get("/status")
    async def status() -> dict[str, bool]:
        return {"ok": True}

    with pytest.raises(ValueError, match="require identity"):
        server.create_app(
            disabled_settings(), modules=(ModuleDefinition("probe", routers=(router,)),)
        )


def test_disabled_background_rejects_job_query_exposure() -> None:
    with pytest.raises(ValidationError, match="enabled task backend"):
        disabled_settings(expose_job_api=True)


@pytest.mark.parametrize("task_backend", ["disabled", "memory"])
def test_task_worker_rejects_non_distributed_configuration_before_constructing_resources(
    monkeypatch: pytest.MonkeyPatch, task_backend: str
) -> None:
    broker_factory = Mock(side_effect=AssertionError("Must validate before broker construction"))
    monkeypatch.setattr(taskiq_worker, "build_taskiq_broker", broker_factory)
    config = Settings(_env_file=None, environment="test", task_backend=task_backend)
    with pytest.raises(ValueError, match="require the distributed task backend"):
        taskiq_worker.build_worker_broker(config)
    broker_factory.assert_not_called()


async def test_disabled_task_exports_fail_startup_and_roll_back_modules_and_shared_client() -> None:
    events: list[str] = []
    clients: list[AsyncClient] = []

    @asynccontextmanager
    async def first(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        clients.append(context.http_client)
        assert context.jobs is None
        events.append("first.open")
        try:
            yield ModuleExports()
        finally:
            assert not context.http_client.is_closed
            events.append("first.close")

    async def handler(payload: dict[str, object]) -> dict[str, object]:
        return payload

    @asynccontextmanager
    async def second(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        events.append("second.open")
        try:
            yield ModuleExports(tasks={"echo": handler})
        finally:
            events.append("second.close")

    app = server.create_app(
        disabled_settings(),
        modules=(
            ModuleDefinition("first", lifespan=first),
            ModuleDefinition("second", requires=("first",), lifespan=second),
        ),
    )
    with pytest.raises(ValueError, match="exports tasks but the task backend is disabled"):
        async with app.router.lifespan_context(app):
            pytest.fail("A task-exporting module must fail before startup completes")
    assert events == ["first.open", "second.open", "second.close", "first.close"]
    assert len(clients) == 1 and clients[0].is_closed
    assert app.state.runtime is None


def test_disabled_background_keeps_synchronous_kernel_routes_and_omits_job_routes() -> None:
    app = FastAPI()
    mount_routes(
        app,
        expose_job_api=False,
        kernel_api_enabled=True,
        exports_api_enabled=False,
        identity_enabled=True,
        definitions=(),
        jobs_enabled=False,
    )
    assert set(app.openapi()["paths"]) == {
        "/health/live",
        "/health/ready",
        "/api/v1/kernels",
        "/api/v1/kernels/{name}/execute",
    }


def test_disabled_background_keeps_kernel_resource_coordination_safeguards() -> None:
    with pytest.raises(ValidationError, match="Redis resource coordination"):
        disabled_settings(
            identity_enabled=True,
            mongo_enabled=True,
            jwt_secret="x" * 32,
            kernel_api_enabled=True,
        )


def test_identity_only_composition_does_not_register_a_background_kernel_task() -> None:
    config = disabled_settings(identity_enabled=True, mongo_enabled=True, jwt_secret="x" * 32)
    registry = TaskRegistry()
    bindings = ModuleBindings(registry, {"identity": IdentityServices(Mock())})
    kernels, exports = build_platform_services(
        config, bindings, {"leases": ResourceCoordinator(config)}
    )
    assert kernels is not None and exports is not None
    with pytest.raises(UnknownTask):
        registry.resolve("platform.kernel.execute")
