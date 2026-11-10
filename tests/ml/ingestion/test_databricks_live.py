"""Opt-in real Warehouse + isolated PostgreSQL + independent worker qualification."""

import hashlib
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from bootstrap.ingestion_wiring import IngestionSettings, build_ingestion
from src.domain.ingestion.models import IngestionRequest, SourceDefinition, sha256
from src.domain.ml import canonical_json

REQUIRED = (
    "ML_TEST_DATABASE_URL",
    "ML_DATABRICKS_CONNECTIONS_FILE",
    "ML_DATABRICKS_SOURCES_FILE",
    "ML_TEST_DATABRICKS_TEMPLATE_REF",
    "ML_TEST_DATABRICKS_EXPECTED_RECORDS",
    "ML_TEST_DATABRICKS_EXPECTED_SHA256",
)


@pytest.mark.integration
def test_live_warehouse_postgres_and_independent_worker(components, actor, tmp_path):
    if not all(os.environ.get(key) for key in REQUIRED):
        pytest.skip("Requires approved Databricks fixture, OAuth secret files and PostgreSQL")
    if not os.environ["ML_TEST_DATABASE_URL"].startswith("postgresql"):
        pytest.fail("Live Databricks qualification requires isolated PostgreSQL")
    settings = replace(
        IngestionSettings.from_env(),
        database_url=components.settings.database_url,
        raw_root=tmp_path / "raw",
        databricks_enabled=True,
    )
    ingress = build_ingestion(settings, database=components.database)
    reference = os.environ["ML_TEST_DATABRICKS_TEMPLATE_REF"]
    expected = os.environ["ML_TEST_DATABRICKS_EXPECTED_SHA256"]
    sha256(expected)
    expected_records = int(os.environ["ML_TEST_DATABRICKS_EXPECTED_RECORDS"])
    assert expected_records > 0
    approved = replace(actor, permissions=actor.permissions | {"ml:source:manage", "ml:ingest"})
    source = SourceDefinition(
        "live-db-qualification",
        "v1",
        approved.tenant_id,
        "databricks-sql",
        "1",
        canonical_json(
            {
                "template_ref": reference,
                "template_digest": dict(ingress.approved_template_digests)[reference],
                "serialization_version": "dbx-jsonl-v1",
            }
        ),
        allowed_actor_ids=(approved.actor_id,),
    )
    ingress.registry.validate_source(source)
    ingress.sources.register(approved, source)
    job = ingress.service.submit(
        approved,
        "real-databricks-qualification",
        IngestionRequest(source.source_id, source.version),
    )
    worker = subprocess.run(
        [sys.executable, "-m", "bootstrap.ingestion_cli", "work", "--once", "--job-id", job.job_id],
        env={
            **os.environ,
            "ML_DATABASE_URL": settings.database_url,
            "ML_RAW_SNAPSHOT_ROOT": str(settings.raw_root),
            "ML_DATABRICKS_ENABLED": "true",
        },
        capture_output=True,
        text=True,
        timeout=source.limits.timeout_seconds + 30,
        check=False,
    )
    assert worker.returncode == 0, "Independent ingestion worker failed; inspect safe job metadata"
    completed = ingress.service.get(approved, job.job_id)
    assert completed.state == "PUBLISHED", completed.error_code
    snapshot = ingress.service.result(approved, job.job_id)
    assert snapshot.record_count == expected_records
    assert snapshot.content_digest == expected
    assert snapshot.source_revision.startswith("delta:")
    assert ingress.store.verify(snapshot) == snapshot
    from pathlib import Path

    assert (
        hashlib.sha256((Path(snapshot.snapshot_ref).parent / "data.jsonl").read_bytes()).hexdigest()
        == expected
    )
