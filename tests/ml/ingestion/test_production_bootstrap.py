"""Exercise the production factory and lifespan; only external identity I/O is replaced."""

import sys

import httpx
import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        sys.version_info < (3, 11), reason="The production template requires Python >=3.11"
    ),
]


@pytest.mark.parametrize("ingestion_enabled", [False, True])
@pytest.mark.parametrize("dependency_state", ["ready", "mongo-unready", "sql-unready"])
async def test_production_factory_registers_health_and_opt_in_ingestion(
    components, tmp_path, monkeypatch, ingestion_enabled, dependency_state
):
    # Import the actual production template only on its supported Python runtime.
    import bootstrap.ml_api as factory
    import bootstrap.wiring as wiring
    import src.services.resource_manager as resources
    from bootstrap.components import ModuleDefinition

    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    monkeypatch.setenv("APP_TASK_BACKEND", "disabled")
    monkeypatch.setenv("APP_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("APP_MONGO_ENABLED", "true")
    monkeypatch.setenv("APP_KERNEL_API_ENABLED", "false")
    monkeypatch.setenv("APP_EXPORTS_API_ENABLED", "false")
    monkeypatch.setenv("APP_EXPOSE_JOB_API", "false")
    monkeypatch.setenv("APP_JWT_SECRET", "bootstrap-test-key-with-at-least-32-bytes")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("ML_DATABASE_URL", components.settings.database_url)
    monkeypatch.setenv("ML_RAW_SNAPSHOT_ROOT", str(tmp_path / "raw"))
    monkeypatch.setenv("ML_INGESTION_FILE_ROOTS_JSON", "{}")
    monkeypatch.setenv("ML_INGESTION_ENABLED", str(ingestion_enabled).lower())
    mongo_lifecycle = []

    class MongoResource:
        def __init__(self, config):
            assert config.identity_enabled and config.mongo_enabled

        async def start(self):
            mongo_lifecycle.append("start")

        async def aclose(self):
            mongo_lifecycle.append("close")

        async def is_ready(self):
            return dependency_state != "mongo-unready"

    # Keep the actual template factory, module catalog, dispatcher, resource
    # coordinator and thread lifecycle. Do not connect a test to an external IdP.
    monkeypatch.setattr(resources, "MongoResource", MongoResource)
    monkeypatch.setattr(
        wiring,
        "build_modules",
        lambda config: (ModuleDefinition("identity", kind="platform"),),
    )
    built = []

    def build_components(settings):
        assert settings.database_url == components.settings.database_url
        built.append(settings)
        return components

    monkeypatch.setattr(factory, "build_components", build_components)
    if dependency_state == "sql-unready":

        def storage_unavailable():
            raise RuntimeError("private-storage-error-do-not-expose")

        monkeypatch.setattr(components.database, "ready", storage_unavailable)

    app = factory.create_app()
    # The production process requires an already-migrated database. Register the
    # optional schema before creating this test fixture's missing tables.
    from src.infrastructure.persistence.ml_database import metadata

    metadata.create_all(components.database.engine)
    assert len(built) == 1
    assert sum(getattr(route, "path", None) == "/health/ready" for route in app.routes) == 1
    assert (
        bool(
            any(getattr(route, "path", "").startswith("/api/v1/ingestion") for route in app.routes)
        )
        == ingestion_enabled
    )
    assert "/health/ready" in app.openapi()["paths"]
    async with app.router.lifespan_context(app):
        assert app.state.runtime is not None
        assert app.state.ml_services is components.services
        assert hasattr(app.state, "ingestion_service") == ingestion_enabled
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            response = await client.get("/health/ready")
            ready = dependency_state == "ready"
            assert response.status_code == (200 if ready else 503)
            assert response.json() == {"ready": ready}
            assert "private-storage-error" not in response.text
            assert (await client.get("/api/v1/training/jobs/no-such-job")).status_code == 401
    assert app.state.runtime is None
    assert mongo_lifecycle == ["start", "close"]
