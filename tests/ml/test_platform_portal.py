"""Exercise the public first-use links, permission catalog and bounded audit reads."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert

from bootstrap.ml_api import create_app as management
from bootstrap.ml_gateway import create_app as gateway
from src.config.settings import Settings
from src.domain.ml import Actor
from src.infrastructure.ml_identity import AuthenticationRequired
from src.infrastructure.persistence.ml_database import audit


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class ScopedIdentity:
    async def resolve(self, token: str) -> Actor:
        if token in {"expired", "revoked"}:
            raise AuthenticationRequired("Invalid credentials")
        grants = {
            "builder": {"ml.dataset.manage"},
            "catalog": {"ml.catalog.read", "ml.ingest", "ml.source.manage"},
            "operator": {"ml.operate"},
            "foreign": {"ml.operate"},
            "predictor": {"ml.predict"},
        }
        return Actor.from_identity(
            "another-tenant" if token == "foreign" else "synthetic-tenant",
            f"synthetic-{token}",
            frozenset(grants[token]),
        )


@pytest.mark.parametrize("role", ["management", "prediction-gateway"])
@pytest.mark.parametrize("prefix", ["", "/application"])
def test_documentation_routes_use_packaged_assets_and_trusted_origin_paths(
    components, role, prefix
):
    app = (
        management(services=components.services, identity=ScopedIdentity())
        if role == "management"
        else gateway()
    )
    client = TestClient(app, root_path=prefix)
    response = client.get(
        prefix + "/api/v1/platform/info",
        headers={"Host": "untrusted.example", "X-Forwarded-Prefix": "/untrusted"},
    )
    assert response.status_code == 200
    info = response.json()
    assert info["service"] == role and info["identity"] is None
    for key, path in (
        ("swagger_url", "/docs"),
        ("redoc_url", "/redoc"),
        ("openapi_url", "/openapi.json"),
        ("capabilities_url", "/api/v1/platform/capabilities"),
    ):
        assert info[key] == prefix + path
    spec = client.get(info["openapi_url"]).json()
    assert "/api/v1/platform/capabilities" in spec["paths"]
    swagger = client.get(info["swagger_url"]).text
    redoc = client.get(info["redoc_url"]).text
    for html in [swagger, redoc]:
        assert info["openapi_url"] in html
        assert "cdn.jsdelivr.net" not in html and "fonts.googleapis.com" not in html
    for name, body, media_type in (
        ("swagger-ui-bundle.js", swagger, "text/javascript"),
        ("swagger-ui.css", swagger, "text/css"),
        ("redoc.standalone.js", redoc, "text/javascript"),
    ):
        path = f"{prefix}/docs/assets/{name}"
        assert path in body
        asset = client.get(path)
        assert asset.status_code == 200 and asset.content
        assert asset.headers["content-type"].startswith(media_type)
        assert asset.headers["x-content-type-options"] == "nosniff"
    assert client.get(prefix + "/docs/assets/swagger-license.txt").status_code == 404


def test_real_management_factory_links_to_existing_identity_without_showing_admin(
    components, monkeypatch
):
    config = Settings(
        _env_file=None,
        task_backend="disabled",
        mongo_enabled=True,
        identity_enabled=True,
        jwt_secret="synthetic-secret-for-isolated-platform-tests",
    )
    monkeypatch.setattr("bootstrap.config.load_settings", lambda: config)
    monkeypatch.setattr("bootstrap.ml_api.build_components", lambda _: components)
    monkeypatch.setattr("bootstrap.ml_api.MLSettings.from_env", lambda: components.settings)
    monkeypatch.setattr("bootstrap.ingestion_wiring.ingress_enabled", lambda: False)
    monkeypatch.setattr("bootstrap.catalog_wiring.build_catalog", lambda: None)
    app = management()
    client = TestClient(app, root_path="/management")
    data = client.get("/management/api/v1/platform/info").json()
    assert data["identity"] == {
        "login_url": "/management/api/v1/identity/login",
        "me_url": "/management/api/v1/identity/me",
        "logout_url": "/management/api/v1/identity/logout",
    }
    spec = client.get(data["openapi_url"]).json()
    assert data["identity"]["login_url"].removeprefix("/management") in spec["paths"]


def test_two_mounted_services_render_their_own_documentation_links(components):
    app = FastAPI()
    app.mount("/management", management(services=components.services, identity=ScopedIdentity()))
    app.mount("/inference", gateway())
    with TestClient(app) as client:
        for prefix in ("/management", "/inference"):
            info = client.get(f"{prefix}/api/v1/platform/info").json()
            assert info["swagger_url"] == f"{prefix}/docs"
            assert info["openapi_url"] == f"{prefix}/openapi.json"
            html = client.get(info["swagger_url"]).text
            assert info["openapi_url"] in html
            assert f"{prefix}/docs/assets/swagger-ui-bundle.js" in html


@pytest.mark.parametrize("configured", [False, True])
def test_management_capabilities_intersect_enabled_modules_and_current_scopes(
    components, configured
):
    app = management(
        services=components.services,
        identity=ScopedIdentity(),
        ingestion_service=SimpleNamespace() if configured else None,
        source_management=SimpleNamespace() if configured else None,
        catalog_service=SimpleNamespace() if configured else None,
    )
    with TestClient(app, root_path="/management") as client:
        path = "/management/api/v1/platform/capabilities"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=bearer("revoked")).status_code == 401
        assert client.get(path, headers=bearer("predictor")).json() == {"items": []}
        output = client.get(path, headers=bearer("catalog"))
        assert output.headers["cache-control"] == "private, no-store"
        assert {item["id"] for item in output.json()["items"]} == (
            {"catalog", "source-management", "ingestion-submit", "snapshot-read"}
            if configured
            else set()
        )
        build = client.get(path, headers=bearer("builder")).json()["items"]
        assert len(build) == 1
        assert build[0]["url"] == "/management/api/v1/datasets/builders"
        assert client.get(build[0]["url"], headers=bearer("builder")).status_code == 200

    bare = management(
        services=replace(components.services, dataset_builds=None, governance=None),
        identity=ScopedIdentity(),
    )
    with TestClient(bare) as client:
        assert client.get("/api/v1/platform/capabilities", headers=bearer("builder")).json() == {
            "items": []
        }


@pytest.mark.parametrize("catalog_enabled", [False, True])
def test_gateway_exposes_only_prediction_capabilities(components, monkeypatch, catalog_enabled):
    monkeypatch.delenv("APP_JWT_SECRET", raising=False)
    monkeypatch.delenv("ML_GATEWAY_IDENTITY_PREFIX", raising=False)
    monkeypatch.setattr("bootstrap.ml_gateway.MLSettings.from_env", lambda: components.settings)
    app = gateway(
        components=components,
        identity=ScopedIdentity(),
        resolver=SimpleNamespace(),
        endpoint_discovery=object() if catalog_enabled else None,
    )
    with TestClient(app, root_path="/inference") as client:
        path = "/inference/api/v1/platform/capabilities"
        assert client.get(path, headers=bearer("expired")).status_code == 401
        assert client.get(path, headers=bearer("operator")).json() == {"items": []}
        response = client.get(path, headers=bearer("predictor"))
        links = [(item["method"], item["url"]) for item in response.json()["items"]]
        predict = [("POST", "/inference/api/v1/prediction/predict")]
        assert links == (
            [("GET", "/inference/api/v1/prediction/endpoints")] + predict
            if catalog_enabled
            else predict
        )
        assert client.get("/inference/api/v1/identity/me").status_code == 404
        assert client.get("/inference/api/v1/platform/audit-events").status_code == 404


def test_gateway_advertises_only_trusted_configured_identity_links(monkeypatch):
    monkeypatch.setenv("ML_GATEWAY_IDENTITY_PREFIX", "/inference/identity")
    client = TestClient(gateway(), root_path="/inference")
    response = client.get("/inference/api/v1/platform/info")
    assert response.json()["identity"] == {
        "login_url": "/inference/identity/login",
        "me_url": "/inference/identity/me",
        "logout_url": "/inference/identity/logout",
    }
    for unsafe in ("https://untrusted.test", "//untrusted.test", "/path/../other", "/path?x=1"):
        monkeypatch.setenv("ML_GATEWAY_IDENTITY_PREFIX", unsafe)
        with pytest.raises(ValueError, match="origin-relative"):
            gateway()


def test_audit_is_subject_tenant_time_and_permission_scoped(components):
    with components.database.transaction() as connection:
        connection.execute(
            insert(audit),
            [
                {
                    "id": event_id,
                    "tenant_id": tenant,
                    "actor_id": "synthetic-actor",
                    "action": "training.submit",
                    "subject_id": subject,
                    "created_at": stamp,
                }
                for event_id, tenant, subject, stamp in (
                    ("a", "synthetic-tenant", "synthetic-job", 20),
                    ("b", "synthetic-tenant", "synthetic-job", 20),
                    ("older", "synthetic-tenant", "synthetic-job", 10),
                    ("other-subject", "synthetic-tenant", "different-job", 25),
                    ("other-tenant", "another-tenant", "synthetic-job", 25),
                )
            ],
        )
    app = management(services=components.services, identity=ScopedIdentity())
    path = "/api/v1/platform/audit-events"
    query = {"subject_id": "synthetic-job", "created_after": 10, "created_before": 30, "limit": 1}
    with TestClient(app) as client:
        assert client.get(path, params=query).status_code == 401
        assert client.get(path, params=query, headers=bearer("builder")).status_code == 403
        owner = bearer("operator")
        page = client.get(path, params=query, headers=owner)
        assert page.headers["cache-control"] == "private, no-store"
        assert page.json()["next_offset"] == 1
        assert page.json()["items"][0] == {
            "event_id": "b",
            "actor_id": "synthetic-actor",
            "action": "training.submit",
            "subject_id": "synthetic-job",
            "created_at": 20.0,
        }
        following = client.get(path, params={**query, "offset": 1}, headers=owner).json()
        assert following["items"][0]["event_id"] == "a" and following["next_offset"] is None
        foreign = client.get(path, params=query, headers=bearer("foreign")).json()
        assert [event["event_id"] for event in foreign["items"]] == ["other-tenant"]
        for invalid in (
            {"created_before": "inf"},
            {"created_before": 800_000},
            {"tenant_id": "private-value"},
            {"limit": 101},
        ):
            request_id = "wp7-correlation-id"
            error = client.get(
                path, params={**query, **invalid}, headers={**owner, "X-Request-ID": request_id}
            )
            assert error.status_code == 422 and "private-value" not in error.text
            assert error.json()["request_id"] == error.headers["x-request-id"] == request_id
        missing = client.get(path, params={"subject_id": "synthetic-job"}, headers=owner)
        assert missing.status_code == 422
