import copy
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import update

from bootstrap.ml_api import create_app
from src.domain.ingestion.models import IngestionRequest, SourceDefinition
from src.domain.ml import Forbidden, MLError
from src.infrastructure.ingestion.persistence.schema import source_definitions
from tests.ml.ingestion.test_source_management import register
from tests.ml.test_api import Identity


def test_listing_uses_same_submission_validation_and_pagination(runtime):
    ingress, owner, body, settings, data = runtime
    first, _ = register(runtime)
    second_body = copy.deepcopy(body)
    second_body["source_id"] = "zztrace"
    second_body["table"][-1] = "another_table"
    second, _ = register(runtime, second_body)
    ingress.sources.set_enabled(owner, first.source_id, first.version, False)
    items, more = ingress.service.available_sources(owner, 1, 0)
    assert items == [] and more
    assert ingress.service.available_sources(owner, 1, 1) == ([second], False)
    assert ingress.service.approved_source(owner, second.source_id, second.version) == second
    job = ingress.service.submit(
        owner, "available-source", IngestionRequest(second.source_id, second.version)
    )
    assert job.source_digest == second.configuration_digest
    settings.databricks_policy_file.write_text('{"contract":"databricks-access-v1","grants":[]}')
    assert ingress.service.available_sources(owner, 100, 0) == ([], False)
    with pytest.raises(Forbidden):
        ingress.service.submit(owner, "revoked", IngestionRequest(second.source_id, second.version))


@pytest.mark.parametrize("change", ["actor", "tenant", "digest", "contract", "connection"])
def test_unusable_source_not_advertised(runtime, change):
    ingress, owner, body, _, _ = runtime
    source, _ = register(runtime)
    if change == "actor":
        owner = replace(owner, actor_id="other")
    elif change == "tenant":
        owner = replace(owner, tenant_id="other")
    elif change == "connection":
        adapter = ingress.registry.resolve("databricks-sql", "1")
        from types import MappingProxyType

        adapter.connections = MappingProxyType(
            {
                "warehouse": replace(
                    adapter.connections["warehouse"], http_path="/sql/1.0/warehouses/bbbb"
                )
            }
        )
    else:
        with ingress.database.transaction() as connection:
            values = {"configuration_digest": "0" * 64}
            if change == "contract":
                source = replace(source, adapter_version="unsupported")
                from src.domain.ml import canonical_json

                values = {
                    "definition_json": canonical_json(source.to_dict()),
                    "configuration_digest": source.configuration_digest,
                }
            connection.execute(update(source_definitions).values(**values))
    assert ingress.service.available_sources(owner, 100, 0) == ([], False)
    with pytest.raises(MLError):
        ingress.service.approved_source(owner, source.source_id, source.version)


def test_legacy_empty_actor_grants_preserved_for_file(components, actor, tmp_path):
    from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
    from src.domain.ml import canonical_json

    ingress = build_ingestion(
        IngestionSettings(file_roots=(("approved", tmp_path),), raw_root=tmp_path / "raw"),
        database=components.database,
    )
    owner = replace(actor, permissions=frozenset({"ml:source:manage", "ml:ingest"}))
    definition = SourceDefinition(
        "file",
        "v1",
        owner.tenant_id,
        "file",
        "1",
        canonical_json({"root_key": "approved", "relative_path": "missing.jsonl"}),
    )
    ingress.sources.register(owner, definition)
    other = replace(owner, actor_id="another-ingestion-user")
    assert ingress.service.available_sources(other, 50, 0) == ([definition], False)


@pytest.mark.asyncio
async def test_available_http_is_independent_of_management(runtime, components):
    ingress, owner, _, _, _ = runtime
    register(runtime)
    actor = replace(owner, permissions=frozenset({"ml:ingest"}))
    app = create_app(
        services=components.services,
        identity=Identity(actor),
        ingestion_service=ingress.service,
        source_management=ingress.source_management,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/ingestion/available-sources")).status_code == 401
        response = await client.get(
            "/api/v1/ingestion/available-sources", headers={"Authorization": "Bearer owner"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["source_id"] == "trace" and body["next_offset"] is None
        assert set(body["items"][0]) == {
            "source_id",
            "version",
            "adapter_type",
            "data_format",
            "schema_version",
            "limits",
        }
        assert "actor" not in response.text and "warehouse" not in response.text
        assert (
            await client.get("/api/v1/ingestion/sources", headers={"Authorization": "Bearer owner"})
        ).status_code == 403
        assert (
            await client.get(
                "/api/v1/ingestion/available-sources?limit=101",
                headers={"Authorization": "Bearer owner"},
            )
        ).status_code == 422
