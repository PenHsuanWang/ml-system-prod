"""Runtime contracts, tenant policy, immutable APIs and durable catalog behavior."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from bootstrap.ml_api import create_app
from src.domain.ingestion.errors import IngestionError, SourceRejected
from src.domain.ingestion.models import ExtractionLimits, IngestionRequest
from src.domain.ml import Actor, Conflict, Forbidden
from src.infrastructure.ingestion.adapters.databricks_contract import (
    build_query,
    validate_source,
)
from src.infrastructure.persistence.ml_database import audit
from tests.ml.test_api import Identity


@pytest.fixture
def runtime(components, actor, tmp_path):
    owner = replace(actor, permissions=actor.permissions | {"ml:source:manage", "ml:ingest"})
    template = {
        "connection_ref": "warehouse",
        "table": ["synthetic", "fdc", "trace"],
        "delta_version": 128,
        "projection": [{"name": "temperature", "type": "float"}],
        "predicates": [{"column": "temperature", "operator": "ge", "value": 0}],
    }
    connection = {
        "warehouse": {
            "server_hostname": "dbc-synthetic.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abcd",
            "access_token_env": "TEST_DBX_TOKEN",
        }
    }
    connections = tmp_path / "connections.json"
    connections.write_text(json.dumps(connection))
    policy = tmp_path / "policy.json"
    policy_data = {
        "contract": "databricks-access-v1",
        "grants": [
            {
                "tenant_id": owner.tenant_id,
                "manager_ids": [owner.actor_id],
                "actor_ids": [owner.actor_id],
                "template": template,
                "limits": ExtractionLimits().to_dict(),
            }
        ],
    }
    policy.write_text(json.dumps(policy_data))
    settings = IngestionSettings(
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw",
        databricks_enabled=True,
        databricks_connections_file=connections,
        databricks_policy_file=policy,
    )
    ingress = build_ingestion(settings, database=components.database)
    body = {
        "source_id": "trace",
        "version": "v1",
        **template,
        "allowed_actor_ids": [owner.actor_id],
    }
    return ingress, owner, body, settings, policy_data


def register(runtime, body=None):
    ingress, owner, default, _, _ = runtime
    request = body or default
    from src.api.ingestion.source_schemas import SourceRegisterRequest

    dto = SourceRegisterRequest(**request)
    return ingress.source_management.register(
        owner,
        dto.source_id,
        dto.version,
        dto.configuration(),
        ExtractionLimits(**dto.limits.model_dump()),
        tuple(dto.allowed_actor_ids),
    )


def test_runtime_contract_identity_limits_and_connection_drift(runtime):
    ingress, owner, _, _, _ = runtime
    source, enabled = register(runtime)
    assert enabled
    adapter = ingress.registry.resolve("databricks-sql", "1")
    template = validate_source(source, adapter.templates, adapter.connections)
    query, params = build_query(template, 10)
    assert "VERSION AS OF 128" in query and params["predicate_0"] == 0
    assert ingress.sources.resolve(owner, "trace", "v1") == source
    with pytest.raises((IngestionError, Forbidden, ValidationError)):
        ingress.service.submit(owner, "wide", IngestionRequest("trace", "v1", max_records=100001))
    changed = replace(adapter.connections["warehouse"], http_path="/sql/1.0/warehouses/bbbb")
    with pytest.raises(SourceRejected):
        validate_source(source, {}, {"warehouse": changed})
    unknown = replace(
        source,
        configuration_json=source.configuration_json.replace(
            "databricks-runtime-v1", "databricks-runtime-v99"
        ),
    )
    with pytest.raises(SourceRejected):
        validate_source(unknown, {}, adapter.connections)


@pytest.mark.parametrize(
    "change",
    [
        {"table": ["synthetic", "fdc", "trace;DROP"]},
        {"table": ["synthetic", "fdc", "private"]},
        {"connection_ref": "unknown"},
        {"projection": [{"name": "secret", "type": "string"}]},
        {"projection": [{"name": "temperature", "type": "sql_expression"}]},
        {"projection": [{"name": "x);SELECT", "type": "float"}]},
        {"delta_version": -1},
        {"delta_version": True},
        {"predicates": []},
        {"predicates": [{"column": "temperature", "operator": "execute", "value": "DROP"}]},
        {"allowed_actor_ids": ["ungranted"]},
        {"limits": {"max_records": 100001}},
    ],
)
def test_reject_invalid_or_unauthorized_configuration(runtime, change):
    request = {**runtime[2], **change}
    with pytest.raises((IngestionError, Forbidden, ValidationError)):
        register(runtime, request)


def test_policy_revocation_disable_and_queued_execution(runtime):
    ingress, owner, body, settings, policy_data = runtime
    register(runtime)
    job = ingress.service.submit(owner, "queued", IngestionRequest("trace", "v1"))
    ingress.source_management.set_enabled(owner, "trace", "v1", False)
    with pytest.raises(SourceRejected):
        ingress.service.submit(owner, "new", IngestionRequest("trace", "v1"))
    with pytest.raises(SourceRejected):
        ingress.sources.for_execution(job)
    ingress.source_management.set_enabled(owner, "trace", "v1", True)
    policy_data["grants"] = []
    settings.databricks_policy_file.write_text(json.dumps(policy_data))
    with pytest.raises(Forbidden):
        ingress.sources.for_execution(job)
    with pytest.raises(Forbidden):
        ingress.service.submit(owner, "revoked", IngestionRequest("trace", "v1"))
    # Neither registration nor policy revocation touched the connection/template files.
    assert not settings.databricks_sources_file


def test_concurrent_versions_and_restart_resolution(runtime):
    ingress, owner, body, settings, _ = runtime
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(lambda _: register(runtime)[0], range(4)))
    assert len({s.configuration_digest for s in sources}) == 1
    changed = {**body, "delta_version": 129}
    with pytest.raises(Conflict):
        register(runtime, changed)
    v2, _ = register(runtime, {**changed, "version": "v2"})
    other = build_ingestion(settings)
    assert other.sources.resolve(owner, "trace", "v1") == sources[0]
    assert other.sources.resolve(owner, "trace", "v2") == v2
    other.close()


@pytest.mark.asyncio
async def test_source_api_security_idempotency_queries_and_audit(runtime, components):
    ingress, owner, body, _, _ = runtime
    app = create_app(
        services=components.services,
        identity=Identity(owner),
        ingestion_service=ingress.service,
        source_management=ingress.source_management,
    )
    headers = {"Authorization": "Bearer owner"}
    base = "/api/v1/ingestion"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get(base + "/connections")).status_code == 401
        assert (await client.get(base + "/connections", headers=headers)).json() == [
            {"connection_ref": "warehouse"}
        ]
        first = await client.post(base + "/sources", headers=headers, json=body)
        assert first.status_code == 201, first.text
        assert first.json()["external_qualification"] == "not_verified"
        assert "hostname" not in first.text and "TEST_DBX_TOKEN" not in first.text
        assert (
            await client.post(base + "/sources", headers=headers, json=body)
        ).json() == first.json()
        conflict = await client.post(
            base + "/sources", headers=headers, json={**body, "delta_version": 129}
        )
        assert conflict.status_code == 409
        v2 = copy.deepcopy(body)
        v2.pop("source_id")
        v2["version"] = "v2"
        assert (
            await client.post(base + "/sources/trace/versions", headers=headers, json=v2)
        ).status_code == 201
        for path in ("/sources", "/sources/trace/versions"):
            page = await client.get(base + path + "?limit=1&offset=1", headers=headers)
            assert page.json()["items"][0]["version"] == "v2"
            assert page.json()["next_offset"] == 2
        for action in ("disable", "enable"):
            result = await client.post(
                base + "/sources/trace/versions/v1/" + action, headers=headers
            )
            assert result.json()["enabled"] == (action == "enable")
        for field in ("tenant_id", "server_hostname", "access_token", "client_secret_file", "sql"):
            denied = await client.post(
                base + "/sources", headers=headers, json={**body, field: "private-test"}
            )
            assert denied.status_code == 422 and "private-test" not in denied.text
        assert (await client.get(base + "/sources?limit=101", headers=headers)).status_code == 422
        cross = await client.get(
            base + "/sources/trace/versions/v1", headers={"Authorization": "Bearer other"}
        )
        assert cross.status_code in (403, 404)
    with components.database.transaction() as connection:
        events = (
            connection.execute(select(audit).where(audit.c.action.like("ingestion.source.%")))
            .mappings()
            .all()
        )
    assert len(events) == 4
    assert {row["subject_id"] for row in events} == {
        first.json()["configuration_digest"],
        ingress.sources.metadata(owner, "trace", "v2")[0].configuration_digest,
    }
    with pytest.raises(Forbidden):
        ingress.source_management.connections(Actor(owner.tenant_id, "stranger", frozenset()))


def test_concurrent_conflicting_registration_is_atomic(runtime):
    bodies = [runtime[2], {**runtime[2], "delta_version": 129}]

    def attempt(body):
        try:
            return register(runtime, body)[0]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, bodies))
    assert sum(r is not None for r in results) == 1
