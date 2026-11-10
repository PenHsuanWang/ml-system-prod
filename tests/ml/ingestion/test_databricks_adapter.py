"""Approved Databricks batches use the real shared stager and immutable publisher."""

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from src.domain.ingestion.errors import IngestionCancelled, IngestionError, SourceRejected
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    IngestionExecution,
    SourceDefinition,
)
from src.domain.ml import canonical_json
from src.infrastructure.ingestion.adapters.databricks_contract import (
    SERIALIZATION_VERSION,
    Column,
    ConnectionConfig,
    Template,
)
from src.infrastructure.ingestion.adapters.databricks_sql import DatabricksSQLBatchAdapter
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from tests.ml.ingestion.test_file_adapter import Control


class FakeStreamClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    def stream(self, *args):
        self.calls.append(args)
        try:
            yield b'{"id":1}\n'
            yield b'{"id":2}\n'
        finally:
            self.closed = True


@pytest.fixture
def adapter_env(tmp_path):
    connection = ConnectionConfig(
        "approved.cloud.databricks.com",
        "/sql/1.0/warehouses/abcdef",
        "principal",
        Path("/run/secrets/approved-client"),
    )
    template = Template("warehouse", ("main", "approved", "events"), 42, (Column("id", "integer"),))
    source = SourceDefinition(
        "events",
        "v1",
        "tenant-a",
        "databricks-sql",
        "1",
        canonical_json(
            {
                "template_ref": "events-v1",
                "template_digest": template.digest(connection),
                "serialization_version": SERIALIZATION_VERSION,
            }
        ),
        limits=ExtractionLimits(max_bytes=4096, max_records=20, max_line_bytes=1024),
    )
    request = BatchExtraction(
        source,
        IngestionExecution("tenant-a", "job-a", "attempt-a", "snapshot-a", time.time()),
        source.limits,
    )
    client = FakeStreamClient()
    store = LocalRawSnapshotStore(tmp_path / "raw")
    adapter = DatabricksSQLBatchAdapter(
        {"warehouse": connection}, {"events-v1": template}, client, store
    )
    return adapter, client, store, request, template, connection


def test_approved_batch_is_verifiable_raw_data_with_fixed_delta_provenance(adapter_env):
    adapter, client, store, request, template, connection = adapter_env
    staged = adapter.acquire(request, Control())
    assert staged.record_count == 2 and client.closed
    assert staged.source_revision == "delta:" + template.digest(connection) + ":42"
    published = store.publish(store.prepare(request, staged))
    assert store.verify(published) == published
    assert (
        Path(published.snapshot_ref).parent / "data.jsonl"
    ).read_bytes() == b'{"id":1}\n{"id":2}\n'
    manifest = Path(published.snapshot_ref).read_text()
    assert "/run/secrets" not in manifest and "principal" not in manifest
    assert "approved.cloud.databricks.com" not in manifest


@pytest.mark.parametrize(
    "case", ["digest", "version", "sql", "endpoint", "serialization", "tenant", "budget"]
)
def test_unapproved_requests_are_rejected_before_client_access(adapter_env, case):
    adapter, client, _, request, template, _ = adapter_env
    config = json.loads(request.source.configuration_json)
    if case == "digest":
        config["template_digest"] = "0" * 64
    elif case == "version":
        adapter = DatabricksSQLBatchAdapter(
            adapter.connections,
            {"events-v1": replace(template, delta_version=43)},
            client,
            adapter.stager,
        )
    elif case in {"sql", "endpoint"}:
        config[case] = "unapproved-input"
    elif case == "serialization":
        config["serialization_version"] = "unknown"
    elif case == "tenant":
        request = replace(request, execution=replace(request.execution, tenant_id="another"))
    else:
        request = replace(request, limits=replace(request.limits, max_bytes=8192))
    request = replace(
        request, source=replace(request.source, configuration_json=canonical_json(config))
    )
    with pytest.raises(SourceRejected):
        adapter.acquire(request, Control())
    assert client.calls == []


@pytest.mark.parametrize("bound", ["max_bytes", "max_records", "max_line_bytes"])
def test_overflow_rejects_whole_batch_and_closes_stream_without_publishing(adapter_env, bound):
    adapter, client, store, request, _, _ = adapter_env
    limits = replace(request.limits, **{bound: 1})
    request = replace(request, limits=limits)
    with pytest.raises(IngestionError):
        adapter.acquire(request, Control())
    assert client.closed
    assert not list((store.root / ".staging").rglob("data.jsonl"))
    assert not list((store.root / "published").rglob("manifest.json"))


def test_cancellation_closes_stream_and_removes_partial_staging(adapter_env):
    adapter, client, store, request, _, _ = adapter_env

    class Cancel:
        calls = 0

        def check(self):
            self.calls += 1
            if self.calls >= 4:
                raise IngestionCancelled("Safe cancellation")

    with pytest.raises(IngestionCancelled):
        adapter.acquire(request, Cancel())
    assert client.closed
    assert not list((store.root / ".staging").rglob("data.jsonl"))
