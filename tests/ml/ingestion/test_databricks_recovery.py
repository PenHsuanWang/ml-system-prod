"""Real SDK-boundary processes and SQL publication recovery with synthetic rows.

These are provider workflow regressions, not real Warehouse qualification.
"""

import multiprocessing
import time
from dataclasses import replace

import pytest

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ingestion.models import IngestionRequest, SourceDefinition
from src.domain.ml import Unavailable, canonical_json
from src.infrastructure.ingestion.adapters import databricks_client
from src.infrastructure.ingestion.adapters.databricks_sql import DatabricksSQLBatchAdapter
from tests.ml.ingestion.test_databricks_wiring import configuration


def _synthetic_rows(connection, template, limits):
    yield b'{"value":1}\n'
    yield b'{"value":2}\n'


def _blocked_rows(connection, template, limits):
    time.sleep(60)
    yield b'{"value":1}\n'


@pytest.fixture
def environment(components, actor, tmp_path, monkeypatch):
    connection_file, template_file = configuration(tmp_path)
    original_client = databricks_client.DatabricksSQLClient
    monkeypatch.setattr(
        databricks_client, "DatabricksSQLClient", lambda: original_client(producer=_synthetic_rows)
    )
    ingress = build_ingestion(
        IngestionSettings(
            database_url=components.settings.database_url,
            raw_root=tmp_path / "raw",
            databricks_enabled=True,
            databricks_connections_file=connection_file,
            databricks_sources_file=template_file,
            retry_delay=0,
        ),
        database=components.database,
    )
    operator = replace(actor, permissions=actor.permissions | {"ml:ingest", "ml:source:manage"})
    source = SourceDefinition(
        "process-events",
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
    return ingress, operator, source, original_client


def test_spawned_provider_rows_reach_existing_raw_snapshot_catalog(environment):
    ingress, actor, source, _ = environment
    job = ingress.service.submit(actor, "process", IngestionRequest(source.source_id, "v1"))
    assert ingress.supervisor().run_once(job_id=job.job_id).state == "PUBLISHED"
    result = ingress.service.result(actor, job.job_id)
    assert result.record_count == 2 and result.source_revision.endswith(":7")
    assert ingress.store.verify(result) == result
    assert not multiprocessing.active_children()


def test_completed_provider_publication_recovers_without_reacquisition(environment, monkeypatch):
    ingress, actor, source, _ = environment
    job = ingress.service.submit(actor, "recover", IngestionRequest(source.source_id, "v1"))
    original_finish = ingress.jobs.finish

    def unavailable(*args):
        raise Unavailable("Injected SQL terminal write failure")

    monkeypatch.setattr(ingress.jobs, "finish", unavailable)
    uncertain = ingress.supervisor().run_once(job_id=job.job_id)
    assert uncertain.state == "NEEDS_RECONCILIATION"
    original_intent = uncertain.publication_intent_json
    ingress.sources.set_enabled(actor, source.source_id, source.version, False)

    def forbid_acquire(*args):
        raise AssertionError("Reconciliation must not read the provider again")

    monkeypatch.setattr(DatabricksSQLBatchAdapter, "acquire", forbid_acquire)
    monkeypatch.setattr(ingress.jobs, "finish", original_finish)
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "PUBLISHED" and completed.snapshot_id == job.snapshot_id
    assert completed.publication_intent_json == original_intent
    assert ingress.store.verify(ingress.service.result(actor, job.job_id)).record_count == 2


def test_blocked_provider_deadline_fails_without_publishing_or_orphaning(environment):
    ingress, actor, source, client_class = environment
    adapter = ingress.registry.resolve("databricks-sql", "1")
    adapter.client = client_class(producer=_blocked_rows)
    ingress.jobs.max_attempts = 1
    job = ingress.service.submit(
        actor, "deadline", IngestionRequest(source.source_id, "v1", timeout_seconds=0.4)
    )
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "FAILED" and completed.error_code == "ACQUISITION_DEADLINE"
    assert not list(ingress.store.root.glob("published/*/*/manifest.json"))
    assert not multiprocessing.active_children()
