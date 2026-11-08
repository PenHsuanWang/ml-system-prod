"""Independent adapters share one executable, model-agnostic staging contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from src.application.ingestion.registry import ApprovedSourceAdapterRegistry
from src.domain.ingestion.models import AdapterDescriptor
from src.infrastructure.ingestion.adapters.file_batch import FileBatchAdapter
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from tests.ml.ingestion.test_file_adapter import Control, extraction


class MemoryBatchAdapter:
    """Test-only, independent producer; no file adapter inheritance or source I/O."""

    def __init__(self, store, content=b'{"sensor":"memory","value":4}\n'):
        self.store, self.content = store, content

    def acquire(self, request, control):
        control.check()
        staged = self.store.stage(request, [self.content], control)
        return replace(staged, source_revision="memory:immutable-v1")


class ThirdBatchAdapter:
    def __init__(self, store):
        self.store = store

    def acquire(self, request, control):
        return self.store.stage(request, (b'{"third":true}\n' for _ in range(2)), control)


@pytest.fixture(params=["file", "memory", "third"])
def registered_adapter(request, tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "records.jsonl").write_bytes(b'{"sensor":"file","value":3}\n')
    store = LocalRawSnapshotStore(tmp_path / "raw")
    file = FileBatchAdapter({"input": root}, store)
    adapters = [
        (AdapterDescriptor("file", "1"), file, file.validate_source),
        (AdapterDescriptor("memory", "1"), MemoryBatchAdapter(store), lambda source: None),
        (AdapterDescriptor("third", "1"), ThirdBatchAdapter(store), lambda source: None),
    ]
    registry = ApprovedSourceAdapterRegistry(adapters)
    source_request = extraction(source_type=request.param)
    registry.validate_source(source_request.source)
    return registry.resolve(request.param, "1"), source_request, store


def test_registered_batch_adapter_contract(registered_adapter):
    adapter, request, store = registered_adapter
    result = adapter.acquire(request, Control())
    assert result.tenant_id == request.execution.tenant_id
    assert result.job_id == request.execution.job_id
    assert result.attempt_id == request.execution.attempt_id
    assert result.size_bytes > 0 and result.record_count > 0
    assert result.data_format == "jsonl" and result.schema_version == "raw-json-object-v1"
    assert len(result.content_digest) == 64
    assert Path(result.staging_ref).is_dir()
    intent = store.prepare(request, result)
    snapshot = store.publish(intent)
    assert snapshot.content_digest == result.content_digest
    assert snapshot.source_revision == result.source_revision
    assert store.verify(snapshot) == snapshot
    store.discard(result)
    assert not Path(result.staging_ref).exists()
