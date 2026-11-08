"""Exercise approved provider registration through the ordinary SQL job workflow."""

import hashlib
import json
from dataclasses import replace

import pytest

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ingestion.errors import SourceRejected
from src.domain.ingestion.models import IngestionRequest, SourceDefinition
from src.domain.ml import canonical_json


def configuration(tmp_path):
    connections = tmp_path / "connections.json"
    templates = tmp_path / "templates.json"
    connections.write_text(
        json.dumps(
            {
                "warehouse": {
                    "server_hostname": "dbc-approved.cloud.databricks.com",
                    "http_path": "/sql/1.0/warehouses/123abc",
                    "client_id": "approved-principal",
                    "client_secret_file": str(tmp_path / "not-mounted-in-management"),
                }
            }
        )
    )
    templates.write_text(
        json.dumps(
            {
                "events": {
                    "connection_ref": "warehouse",
                    "table": ["main", "approved", "events"],
                    "delta_version": 7,
                    "projection": [{"name": "value", "type": "integer"}],
                    "predicates": [],
                }
            }
        )
    )
    return connections, templates


def test_databricks_disabled_never_loads_optional_configuration(tmp_path):
    ingress = build_ingestion(
        IngestionSettings(
            database_url="sqlite://",
            raw_root=tmp_path / "raw",
            databricks_connections_file=tmp_path / "missing.json",
            databricks_sources_file=tmp_path / "missing-templates.json",
        )
    )
    try:
        assert not ingress.approved_template_digests
        with pytest.raises(SourceRejected):
            ingress.registry.resolve("databricks-sql", "1")
    finally:
        ingress.close()


def test_enabled_databricks_uses_same_ingestion_and_publication_workflow(
    components, actor, tmp_path, monkeypatch
):
    from src.infrastructure.ingestion.adapters import databricks_client

    payload = b'{"value":1}\n{"value":2}\n'
    calls = []

    class ApprovedClient:
        def stream(self, connection, template, limits, control):
            control.check()
            calls.append((template.delta_version, template.table))
            yield payload

    monkeypatch.setattr(databricks_client, "DatabricksSQLClient", ApprovedClient)
    connections, templates = configuration(tmp_path)
    settings = IngestionSettings(
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw",
        databricks_enabled=True,
        databricks_connections_file=connections,
        databricks_sources_file=templates,
    )
    ingress = build_ingestion(settings, database=components.database)
    operator = replace(actor, permissions=actor.permissions | {"ml:ingest", "ml:source:manage"})
    source = SourceDefinition(
        "dbx-events",
        "v1",
        actor.tenant_id,
        "databricks-sql",
        "1",
        canonical_json(
            {
                "template_ref": "events",
                "template_digest": dict(ingress.approved_template_digests)["events"],
                "serialization_version": "dbx-jsonl-v1",
            }
        ),
    )
    ingress.sources.register(operator, source)
    job = ingress.service.submit(operator, "dbx-first", IngestionRequest(source.source_id, "v1"))
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    snapshot = ingress.service.result(operator, job.job_id)
    assert snapshot.content_digest == hashlib.sha256(payload).hexdigest()
    assert snapshot.record_count == 2 and snapshot.source_revision.endswith(":7")
    assert ingress.store.verify(snapshot) == snapshot
    assert calls == [(7, ("main", "approved", "events"))]
    queued = ingress.service.submit(operator, "dbx-next", IngestionRequest(source.source_id, "v1"))
    changed = json.loads(templates.read_text())
    changed["events"]["delta_version"] = 8
    templates.write_text(json.dumps(changed))
    upgraded = build_ingestion(settings, database=components.database)
    assert upgraded.supervisor().run_once(job_id=queued.job_id).state == "FAILED"
    assert len(calls) == 1, "Changed deployment cannot silently change an approved source version"


@pytest.mark.parametrize(
    "field,value",
    [
        ("databricks_enabled", 1),
        ("databricks_connections_file", "relative.json"),
    ],
)
def test_databricks_startup_settings_fail_closed(field, value, tmp_path):
    with pytest.raises(ValueError):
        IngestionSettings(raw_root=tmp_path / "raw", **{field: value})


def test_enabled_provider_requires_configs_before_creating_storage(tmp_path):
    with pytest.raises(ValueError):
        build_ingestion(IngestionSettings(raw_root=tmp_path / "raw", databricks_enabled=True))
    assert not (tmp_path / "raw").exists()


def test_approved_configuration_rejects_duplicate_keys(tmp_path):
    connections, templates = configuration(tmp_path)
    connections.write_text('{"warehouse":{},"warehouse":{}}')
    with pytest.raises(ValueError):
        build_ingestion(
            IngestionSettings(
                raw_root=tmp_path / "raw",
                databricks_enabled=True,
                databricks_connections_file=connections,
                databricks_sources_file=templates,
            )
        )
    assert not (tmp_path / "raw").exists()
