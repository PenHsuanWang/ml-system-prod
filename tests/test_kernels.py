import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

import server
from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from src.api.auth import IdentityServices, current_principal
from src.api.dependencies import get_runtime
from src.api.kernels.routers import router as kernel_router
from src.application.kernels.service import KernelService
from src.application.orchestration import KernelJobHandler
from src.config.settings import Settings
from src.domain.execution import ExecutionBusy, ResourceLease, ResourceLeaseLost
from src.domain.identity.errors import AuthenticationFailed, AuthorizationDenied
from src.domain.identity.models import Principal
from src.domain.kernels.models import (
    InvalidKernelInput,
    KernelDeadlineExceeded,
    KernelExecutionFailed,
    KernelNotFound,
    KernelSpec,
)
from src.infrastructure.locking.resources import ResourceCoordinator

OWNER = Principal(
    "owner", "tenant_a", "owner", frozenset({"kernel.read", "kernel.execute", "jobs.read"})
)


class ProbeKernel:
    spec = KernelSpec("1.0.0", "custom")

    def __init__(self) -> None:
        self.contexts = []
        self.active = 0
        self.peak = 0

    async def execute(self, payload, context):
        self.contexts.append(context)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.005)
            return payload
        finally:
            self.active -= 1


def settings(**overrides):
    return Settings(_env_file=None, environment="test", task_backend="memory", **overrides)


@pytest.fixture
async def kernel_service():
    coordinator = ResourceCoordinator(settings())
    await coordinator.start()
    kernel = ProbeKernel()
    service = KernelService(
        {"probe.echo": kernel}, coordinator, timeout_seconds=1, resource_limits={"compute": 1}
    )
    try:
        yield service, kernel, coordinator
    finally:
        await service.aclose()
        await coordinator.aclose()


async def test_empty_registry_has_no_default_algorithm():
    coordinator = ResourceCoordinator(settings())
    await coordinator.start()
    try:
        service = KernelService({}, coordinator, timeout_seconds=1, resource_limits={"compute": 1})
        assert service.list_kernels(OWNER) == []
        with pytest.raises(KernelNotFound):
            await service.execute("probe.echo", "1", {}, OWNER, "request")
    finally:
        await coordinator.aclose()


async def test_kernel_versions_authorization_context_and_capacity(kernel_service):
    service, kernel, _ = kernel_service
    assert service.list_kernels(OWNER)[0]["version"] == "1.0.0"
    with pytest.raises(KernelNotFound):
        await service.execute("probe.echo", "2.0.0", {}, OWNER, "request")
    with pytest.raises(AuthorizationDenied):
        await service.execute(
            "probe.echo", "1.0.0", {}, replace(OWNER, permissions=frozenset()), "request"
        )
    responses = await asyncio.gather(
        *(
            service.execute("probe.echo", "1.0.0", {"value": index}, OWNER, f"request-{index}")
            for index in range(3)
        )
    )
    assert kernel.peak == 1
    assert [result["output"] for result in responses] == [{"value": index} for index in range(3)]
    assert all(context.principal == OWNER for context in kernel.contexts)
    assert len({context.fencing_token for context in kernel.contexts}) == 3


@pytest.mark.parametrize("version,group", [("", "compute"), ("1", "unknown")])
async def test_invalid_kernel_metadata_fails_at_composition(version, group):
    coordinator = ResourceCoordinator(settings())
    kernel = ProbeKernel()
    kernel.spec = KernelSpec(version, "custom", group)
    with pytest.raises(ValueError):
        KernelService(
            {"probe.echo": kernel}, coordinator, timeout_seconds=1, resource_limits={"compute": 1}
        )


async def test_input_size_and_strict_json_are_checked_before_execution(kernel_service):
    service, kernel, _ = kernel_service
    with pytest.raises(InvalidKernelInput):
        await service.execute("probe.echo", "1.0.0", {"value": float("nan")}, OWNER, "request")
    with pytest.raises(InvalidKernelInput):
        await service.execute("probe.echo", "1.0.0", {"value": "x" * 1_048_576}, OWNER, "request")
    assert kernel.contexts == []


async def test_deadline_cancels_kernel_and_releases_capacity(kernel_service):
    _, kernel, coordinator = kernel_service
    cancelled = asyncio.Event()

    async def slow(payload, context):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    kernel.execute = slow
    service = KernelService(
        {"probe.echo": kernel}, coordinator, timeout_seconds=0.01, resource_limits={"compute": 1}
    )
    with pytest.raises(KernelDeadlineExceeded):
        await service.execute("probe.echo", "1.0.0", {}, OWNER, "request")
    assert cancelled.is_set()
    async with coordinator.lease("compute", wait_seconds=0.1):
        pass


async def test_lease_loss_cancels_work_and_fails_closed():
    cancelled = asyncio.Event()

    class LostCoordinator:
        @asynccontextmanager
        async def lease(self, name, *, wait_seconds):
            lease = ResourceLease(name, 123)
            lease.lost.set()
            yield lease

    kernel = ProbeKernel()

    async def execute(payload, context):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    kernel.execute = execute
    service = KernelService(
        {"probe.echo": kernel}, LostCoordinator(), timeout_seconds=1, resource_limits={"compute": 1}
    )
    with pytest.raises(ResourceLeaseLost):
        await service.execute("probe.echo", "1.0.0", {}, OWNER, "request")
    assert cancelled.is_set()


@pytest.mark.parametrize("output", [{"value": {1}}, {"value": float("nan")}, [1]])
async def test_non_json_results_are_sanitized(kernel_service, output):
    _, kernel, coordinator = kernel_service
    kernel.execute = AsyncMock(return_value=output)
    service = KernelService(
        {"probe.echo": kernel}, coordinator, timeout_seconds=1, resource_limits={"compute": 1}
    )
    with pytest.raises(KernelExecutionFailed):
        await service.execute("probe.echo", "1.0.0", {}, OWNER, "request")


async def test_kernel_http_execution_overload_returns_service_unavailable(kernel_service):
    service, kernel, _ = kernel_service
    kernel.execute = AsyncMock(side_effect=ExecutionBusy("Thread execution capacity is full"))
    app = server.create_app(settings(), modules=())
    app.include_router(kernel_router, prefix="/api/v1")
    app.dependency_overrides[get_runtime] = lambda: SimpleNamespace(kernels=service)
    app.dependency_overrides[current_principal] = lambda: OWNER
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/kernels/probe.echo/execute",
            json={"version": "1.0.0", "input": {}},
        )
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "execution_unavailable",
        "message": "Thread execution capacity is full",
    }


async def test_close_cancels_active_execution_before_module_resources_close(kernel_service):
    service, kernel, _ = kernel_service
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def execute(payload, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    kernel.execute = execute
    task = asyncio.create_task(service.execute("probe.echo", "1.0.0", {}, OWNER, "request"))
    await started.wait()
    await service.aclose()
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(KernelExecutionFailed, match="shutting down"):
        await service.execute("probe.echo", "1.0.0", {}, OWNER, "request")


async def test_background_job_reads_current_identity_instead_of_embedded_permissions(
    kernel_service,
):
    service, kernel, _ = kernel_service
    resolver = AsyncMock(return_value=replace(OWNER, permissions=frozenset()))
    handler = KernelJobHandler(service, resolver)
    with pytest.raises(AuthorizationDenied):
        await handler(
            {
                "tenant_id": OWNER.tenant_id,
                "user_id": OWNER.user_id,
                "kernel": "probe.echo",
                "version": "1.0.0",
                "input": {},
                "request_id": "request",
            }
        )
    resolver.assert_awaited_once_with(OWNER.tenant_id, OWNER.user_id)
    assert kernel.contexts == []


async def test_kernel_http_queue_and_job_ownership(monkeypatch):
    principals = {
        "owner": OWNER,
        "other": replace(OWNER, user_id="other"),
        "foreign": replace(
            OWNER, tenant_id="tenant_b", permissions=OWNER.permissions | {"jobs.read_all"}
        ),
        "admin": replace(OWNER, user_id="admin", permissions=OWNER.permissions | {"jobs.read_all"}),
    }

    async def authenticate(token):
        if token not in principals:
            raise AuthenticationFailed("Invalid credentials")
        return principals[token]

    services = IdentityServices(
        SimpleNamespace(authenticate=authenticate, resolve_principal=AsyncMock(return_value=OWNER))
    )

    @asynccontextmanager
    async def identity_lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        yield ModuleExports(services=services)

    @asynccontextmanager
    async def probe_lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        yield ModuleExports(kernels={"echo": ProbeKernel()})

    original_open = server.open_resources

    async def local_resources(stack, config):
        return await original_open(stack, settings())

    monkeypatch.setattr(server, "open_resources", local_resources)
    router = APIRouter()

    @router.get("/status")
    async def status():
        return {"status": "ok"}

    app = server.create_app(
        settings(
            identity_enabled=True, mongo_enabled=True, jwt_secret="x" * 32, kernel_api_enabled=True
        ),
        modules=(
            ModuleDefinition("identity", lifespan=identity_lifespan, kind="platform"),
            ModuleDefinition("probe", lifespan=probe_lifespan, routers=(router,)),
            ModuleDefinition("anonymous_probe", allow_anonymous=True, routers=(router,)),
        ),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/kernels")).status_code == 401
        assert (await client.get("/api/v1/probe/status")).status_code == 401
        assert (await client.get("/api/v1/anonymous_probe/status")).status_code == 200
        headers = {"Authorization": "Bearer owner"}
        assert (await client.get("/api/v1/probe/status", headers=headers)).status_code == 200
        response = await client.post(
            "/api/v1/kernels/probe.echo/execute",
            headers=headers,
            json={"version": "1.0.0", "input": {"value": 42}},
        )
        assert response.status_code == 200
        assert response.json()["output"] == {"value": 42}
        response = await client.post(
            "/api/v1/kernels/probe.echo/runs",
            headers=headers,
            json={"version": "1.0.0", "input": {"value": 42}},
        )
        assert response.status_code == 202
        location = response.headers["Location"]
        async with asyncio.timeout(2):
            while True:
                snapshot = await client.get(location, headers=headers)
                assert snapshot.status_code == 200
                if snapshot.json()["status"] == "succeeded":
                    break
                await asyncio.sleep(0.005)
        assert snapshot.json()["tenant_id"] == OWNER.tenant_id
        assert snapshot.json()["owner_id"] == OWNER.user_id
        assert (await client.get(location)).status_code == 401
        for token in ("other", "foreign"):
            assert (
                await client.get(location, headers={"Authorization": f"Bearer {token}"})
            ).status_code == 404
        assert (
            await client.get(location, headers={"Authorization": "Bearer admin"})
        ).status_code == 200
