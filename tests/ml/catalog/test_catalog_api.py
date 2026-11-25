from dataclasses import replace

import httpx
import pytest

from bootstrap.catalog_wiring import build_catalog
from bootstrap.ml_api import create_app
from src.domain.catalog.models import MetadataItem
from src.domain.ml import Actor
from tests.ml.test_api import Identity


@pytest.mark.asyncio
async def test_catalog_http_contract_auth_scope_and_redaction(components, discovery):
    service, actor, _, provider, _ = discovery
    app = create_app(
        services=components.services, identity=Identity(actor), catalog_service=service
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/catalog/connections")).status_code == 401
        headers = {"Authorization": "Bearer owner"}
        response = await client.get("/api/v1/catalog/connections", headers=headers)
        assert response.json() == {
            "items": ["warehouse"],
            "limit": 50,
            "next_offset": None,
            "has_more": False,
        }
        provider.values = (
            MetadataItem(
                "temperature",
                native_type="DOUBLE",
                nullable=True,
                ingestion_type="float",
                supported=True,
            ),
        )
        response = await client.get(
            "/api/v1/catalog/warehouse/columns",
            params={"catalog": "synthetic", "schema": "fdc", "table": "trace"},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["name"] == "temperature"
        assert body["version_discovery"] == "unavailable" and body["next_offset"] is None
        assert all(
            secret not in response.text
            for secret in ("dbc-synthetic", "TOKEN", "browser", "warehouses", "client_secret")
        )
        for params in ({"limit": 101}, {"offset": -1}, {"sql": "SECRET"}):
            response = await client.get(
                "/api/v1/catalog/warehouse/catalogs", params=params, headers=headers
            )
            assert response.status_code == 422 and "SECRET" not in response.text
        assert (
            await client.get(
                "/api/v1/catalog/warehouse/tables",
                params={"catalog": "private", "schema": "fdc"},
                headers=headers,
            )
        ).status_code == 403
        assert (
            await client.get(
                "/api/v1/catalog/warehouse/columns",
                params={"catalog": "cat;DROP", "schema": "fdc", "table": "trace"},
                headers=headers,
            )
        ).status_code == 422
        assert (await client.get("/api/v1/ingestion/sources", headers=headers)).status_code == 404
    scopes = [(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")]
    assert len(scopes) == len(set(scopes))


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [{"ml:source:manage"}, {"ml:ingest"}])
async def test_no_implicit_discovery_permission(components, discovery, permissions):
    service, actor, _, provider, _ = discovery
    app = create_app(
        services=components.services,
        identity=Identity(replace(actor, permissions=frozenset(permissions))),
        catalog_service=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get(
                "/api/v1/catalog/connections", headers={"Authorization": "Bearer owner"}
            )
        ).status_code == 403
    assert provider.calls == []


def test_discovery_disabled_and_separate_connection_configuration(monkeypatch, tmp_path):
    import json

    monkeypatch.delenv("ML_CATALOG_ENABLED", raising=False)
    assert build_catalog() is None
    monkeypatch.setenv("ML_CATALOG_ENABLED", "true")
    assert build_catalog() is None
    connection = {
        "warehouse": {
            "server_hostname": "dbc-synthetic.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abcd",
            "access_token_env": "DATABRICKS_TOKEN",
        }
    }
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps(connection))
    policy = tmp_path / "policy.json"
    policy.write_text('{"contract":"databricks-discovery-v1","grants":[]}')
    monkeypatch.setenv("ML_CATALOG_CONNECTIONS_FILE", str(path))
    monkeypatch.setenv("ML_CATALOG_POLICY_FILE", str(policy))
    with pytest.raises(ValueError, match="dedicated token"):
        build_catalog()
    connection["warehouse"]["access_token_env"] = "ML_DISCOVERY_DATABRICKS_TOKEN"
    path.write_text(json.dumps(connection))
    monkeypatch.delenv("ML_DATABRICKS_CONNECTIONS_FILE", raising=False)
    assert (
        build_catalog().connections(Actor("tenant", "actor", frozenset({"ml:catalog:read"}))) == []
    )
    monkeypatch.setenv("ML_DATABRICKS_CONNECTIONS_FILE", str(path))
    with pytest.raises(ValueError, match="distinct credential"):
        build_catalog()


def test_dedicated_oauth_identity_is_supported(monkeypatch, tmp_path):
    import json

    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "warehouse": {
                    "server_hostname": "dbc-synthetic.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/abcd",
                    "client_id": "discovery-client",
                    "client_secret_file": str(tmp_path / "discovery-secret"),
                }
            }
        )
    )
    policy = tmp_path / "policy.json"
    policy.write_text('{"contract":"databricks-discovery-v1","grants":[]}')
    monkeypatch.setenv("ML_CATALOG_ENABLED", "true")
    monkeypatch.setenv("ML_CATALOG_CONNECTIONS_FILE", str(path))
    monkeypatch.setenv("ML_CATALOG_POLICY_FILE", str(policy))
    monkeypatch.delenv("ML_DATABRICKS_CONNECTIONS_FILE", raising=False)
    service = build_catalog()
    assert service.provider.connections["warehouse"].client_id == "discovery-client"
    # Registration/startup must not read the secret. The bounded child reads it on demand.
    assert not (tmp_path / "discovery-secret").exists()
