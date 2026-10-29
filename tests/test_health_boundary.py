import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.routers import mount_routes
from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.ports import JobDispatcher
from src.infrastructure.locking.resources import ResourceCoordinator
from src.infrastructure.persistence.mongo import MongoResource
from src.services.health import check_readiness


@pytest.mark.parametrize(
    ("dispatcher_ready", "mongo_ready", "leases_ready", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
async def test_readiness_checks_all_managed_resources(
    monkeypatch: pytest.MonkeyPatch,
    dispatcher_ready: bool,
    mongo_ready: bool,
    leases_ready: bool,
    expected: bool,
) -> None:
    dispatcher = AsyncMock(spec=JobDispatcher)
    dispatcher.is_ready.return_value = dispatcher_ready
    mongodb = MongoResource(Settings())
    leases = ResourceCoordinator(Settings())
    mongo_check = AsyncMock(return_value=mongo_ready)
    leases_check = AsyncMock(return_value=leases_ready)
    monkeypatch.setattr(mongodb, "is_ready", mongo_check)
    monkeypatch.setattr(leases, "is_ready", leases_check)
    unrelated_resource = AsyncMock()

    assert (
        await check_readiness(
            dispatcher, {"mongodb": mongodb, "leases": leases, "other": unrelated_resource}
        )
        is expected
    )
    dispatcher.is_ready.assert_awaited_once()
    mongo_check.assert_awaited_once()
    leases_check.assert_awaited_once()
    unrelated_resource.is_ready.assert_not_awaited()


@pytest.mark.parametrize("error", [DispatcherUnavailable("offline"), OSError(), TimeoutError()])
async def test_readiness_turns_dependency_failures_into_unavailable(error: Exception) -> None:
    dispatcher = AsyncMock(spec=JobDispatcher)
    dispatcher.is_ready.side_effect = error

    assert not await check_readiness(dispatcher, {})


async def test_readiness_has_a_three_second_total_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatcher = AsyncMock(spec=JobDispatcher)
    requested_timeouts: list[float] = []

    @asynccontextmanager
    async def timed_out(delay: float) -> AsyncIterator[None]:
        requested_timeouts.append(delay)
        yield
        raise TimeoutError

    monkeypatch.setattr("src.services.health.asyncio.timeout", timed_out)

    assert not await check_readiness(dispatcher, {})
    assert requested_timeouts == [3]


async def test_readiness_propagates_process_cancellation() -> None:
    dispatcher = AsyncMock(spec=JobDispatcher)
    dispatcher.is_ready.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await check_readiness(dispatcher, {})


@dataclass(frozen=True)
class StubRouteGroup:
    name: str
    routers: tuple[APIRouter, ...]
    kind: Literal["application", "platform"] = "application"
    allow_anonymous: bool = False


@pytest.mark.parametrize(
    ("expose_job_api", "kernel_api_enabled", "exports_api_enabled", "expected_paths"),
    [
        (False, False, False, set()),
        (True, False, False, {"/api/v1/jobs/{job_id}"}),
        (
            False,
            True,
            True,
            {
                "/api/v1/jobs/{job_id}",
                "/api/v1/kernels",
                "/api/v1/kernels/{name}/execute",
                "/api/v1/kernels/{name}/runs",
                "/api/v1/exports/sources",
                "/api/v1/exports/{source_name}",
            },
        ),
    ],
)
def test_route_mounting_preserves_optional_platform_endpoints(
    expose_job_api: bool,
    kernel_api_enabled: bool,
    exports_api_enabled: bool,
    expected_paths: set[str],
) -> None:
    app = FastAPI()
    mount_routes(
        app,
        expose_job_api=expose_job_api,
        kernel_api_enabled=kernel_api_enabled,
        exports_api_enabled=exports_api_enabled,
        identity_enabled=False,
        definitions=(),
    )

    assert set(app.openapi()["paths"]) == expected_paths | {"/health/live", "/health/ready"}


@pytest.mark.parametrize("identity_enabled", [False, True])
async def test_route_mounting_protects_only_nonanonymous_application_groups(
    identity_enabled: bool,
) -> None:
    router = APIRouter()

    @router.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    mount_routes(
        app,
        expose_job_api=False,
        kernel_api_enabled=False,
        exports_api_enabled=False,
        identity_enabled=identity_enabled,
        definitions=(
            StubRouteGroup("protected", (router,)),
            StubRouteGroup("public", (router,), allow_anonymous=True),
            StubRouteGroup("platform", (router,), kind="platform"),
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        protected = await client.get("/api/v1/protected/probe")
        public = await client.get("/api/v1/public/probe")
        platform = await client.get("/api/v1/platform/probe")

    assert protected.status_code == (401 if identity_enabled else 200)
    assert public.status_code == platform.status_code == 200
    if identity_enabled:
        assert protected.headers["WWW-Authenticate"] == "Bearer"
