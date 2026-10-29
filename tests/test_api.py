import asyncio
from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import server
from src.api.dependencies import get_job_service
from src.config.settings import Settings
from src.domain.jobs.errors import DispatcherUnavailable, QueueFull, UnknownTask
from tests.fixtures.neutral_module import module as probe_module


def test_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "environment": "test",
            "task_backend": "memory",
            "workers": 1,
            "reload": False,
            **overrides,
        },
    )


# A settings helper, not a pytest test.
test_settings.__test__ = False


async def test_factory_is_lazy_and_lifespan_releases_resources() -> None:
    app = server.create_app(test_settings())
    assert app.state.runtime is None
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        assert await runtime.dispatcher.is_ready()
        assert not runtime.http_client.is_closed
    assert app.state.runtime is None
    assert runtime.http_client.is_closed
    assert not await runtime.dispatcher.is_ready()


async def test_default_application_has_no_modules_tasks_or_job_api() -> None:
    app = server.create_app(test_settings())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        assert app.state.runtime.modules == {}
        with pytest.raises(UnknownTask):
            app.state.runtime.registry.resolve("probe.echo")
        assert set(app.openapi()["paths"]) == {"/health/live", "/health/ready"}
        assert (await client.post("/api/v1/probe/tasks", json={"value": 1})).status_code == 404
        assert (await client.get(f"/api/v1/jobs/{'0' * 32}")).status_code == 404


async def test_explicit_module_can_submit_and_poll_technical_task() -> None:
    app = server.create_app(test_settings(expose_job_api=True), modules=(probe_module,))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/live")).json() == {"status": "ok"}
        assert (await client.get("/health/ready")).json() == {"status": "ready"}
        assert app.state.runtime.modules == {"probe": {"role": "api"}}
        response = await client.post(
            "/api/v1/probe/tasks", json={"value": 42}, headers={"X-Request-ID": "test-123"}
        )
        assert response.status_code == 202
        assert response.headers["x-request-id"] == "test-123"
        body = response.json()
        assert body["name"] == "probe.echo"
        assert body["status"] == "queued"
        assert response.headers["location"] == f"/api/v1/jobs/{body['job_id']}"
        async with asyncio.timeout(2):
            while True:
                response = await client.get(
                    response.headers.get("location", f"/api/v1/jobs/{body['job_id']}")
                )
                result = response.json()
                if result["status"] == "succeeded":
                    break
                await asyncio.sleep(0.001)
        assert result["result"] == {"value": 42}
        assert result["attempts"] == 1
        assert result["finished_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"value": []},
        {"value": True},
        {"value": "1"},
        {"value": 1.5},
        {"value": 1, "extra": "private"},
        {},
    ],
)
async def test_validation_is_consistent_and_does_not_echo_input(payload: object) -> None:
    app = server.create_app(test_settings(), modules=(probe_module,))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/probe/tasks", json=payload)
        assert response.status_code == 422
        assert response.json()["error"] == {
            "code": "validation_error",
            "message": "Request validation failed",
        }
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "private" not in response.text


async def test_missing_job_and_invalid_request_id() -> None:
    app = server.create_app(test_settings(expose_job_api=True))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.get(f"/api/v1/jobs/{'0' * 32}", headers={"X-Request-ID": "x" * 65})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"
        assert len(response.headers["x-request-id"]) == 32
        assert (await client.get("/api/v1/jobs/bad-id")).status_code == 422


async def test_backpressure_and_dependency_errors_have_http_mapping() -> None:
    class FailedService:
        async def submit(self, *_: object) -> None:
            raise QueueFull("The local job queue is full")

        async def get(self, *_: object) -> None:
            raise DispatcherUnavailable("Job state could not be read")

    app = server.create_app(test_settings(expose_job_api=True), modules=(probe_module,))
    app.dependency_overrides[get_job_service] = FailedService
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/probe/tasks", json={"value": 1})
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json()["error"]["code"] == "queue_full"
        response = await client.get(f"/api/v1/jobs/{'0' * 32}")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dispatcher_unavailable"


async def test_unready_dependency_does_not_break_liveness() -> None:
    class UnreadyDispatcher:
        async def is_ready(self) -> bool:
            return False

    app = server.create_app(test_settings())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        app.state.runtime = replace(app.state.runtime, dispatcher=UnreadyDispatcher())
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


async def test_partial_startup_rolls_back_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenDispatcher:
        closed = False

        async def start(self) -> None:
            raise DispatcherUnavailable("Startup failed")

        async def aclose(self) -> None:
            self.closed = True

    dispatcher = BrokenDispatcher()
    clients = []

    def make_client(**kwargs: object) -> AsyncClient:
        client = AsyncClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(server, "AsyncClient", make_client)
    monkeypatch.setattr(server, "build_dispatcher", lambda *_: dispatcher)
    app = server.create_app(test_settings())
    with pytest.raises(DispatcherUnavailable):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup failure must not yield a running application")
    assert dispatcher.closed
    assert len(clients) == 1 and clients[0].is_closed
    assert app.state.runtime is None


async def test_dispatcher_drains_before_shared_http_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dispatcher:
        async def start(self) -> None:
            pass

        async def aclose(self) -> None:
            assert not runtime.http_client.is_closed

    monkeypatch.setattr(server, "build_dispatcher", lambda *_: Dispatcher())
    app = server.create_app(test_settings())
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
    assert runtime.http_client.is_closed


def test_cli_uses_factory_and_propagates_worker_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_TASK_BACKEND", "distributed")
    monkeypatch.setenv("APP_RELOAD", "false")
    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    called = {}
    monkeypatch.setattr(
        server.uvicorn, "run", lambda app, **kwargs: called.update(app=app, **kwargs)
    )
    # Restore CLI-written env vars after this test too.
    monkeypatch.setenv("APP_WORKERS", "1")
    monkeypatch.setenv("APP_PORT", "8000")
    server.main(["--workers", "2", "--port", "9000"])
    assert called["app"] == "server:create_app"
    assert called["factory"] is True
    assert called["workers"] == 2
    assert Settings().workers == 2
    assert Settings().port == 9000


@pytest.mark.parametrize(
    "overrides",
    [
        {"workers": 2},
        {"workers": 2, "reload": True, "task_backend": "distributed"},
        {"environment": "production"},
    ],
)
def test_invalid_runtime_combinations_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        test_settings(**overrides)


def test_invalid_identity_settings_do_not_echo_secret_values() -> None:
    secret = "sensitive-short-secret"
    with pytest.raises(ValidationError) as caught:
        test_settings(identity_enabled=True, mongo_enabled=True, jwt_secret=secret)
    assert secret not in str(caught.value)


def test_production_application_routers_require_identity_unless_explicitly_anonymous() -> None:
    from fastapi import APIRouter

    from bootstrap.components import ModuleDefinition

    router = APIRouter()

    @router.get("/status")
    async def status() -> dict[str, str]:
        return {"status": "ok"}

    config = test_settings(environment="production", task_backend="distributed")
    with pytest.raises(ValueError, match="require identity"):
        server.create_app(config, modules=(ModuleDefinition("probe", routers=(router,)),))
    app = server.create_app(
        config, modules=(ModuleDefinition("probe", routers=(router,), allow_anonymous=True),)
    )
    assert "/api/v1/probe/status" in app.openapi()["paths"]
