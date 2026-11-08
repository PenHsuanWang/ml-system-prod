"""New batch implementations use registration and the same unmodified application workflow."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from bootstrap.ingestion_wiring import build_ingestion
from src.application.ingestion.service import IngestionService
from src.domain.ingestion.errors import IngestionCancelled, SourceRejected
from src.domain.ingestion.models import (
    AdapterDescriptor,
    BatchExtraction,
    IngestionRequest,
)
from src.infrastructure.ingestion.persistence.schema import raw_snapshots
from src.infrastructure.persistence.ml_database import datasets
from tests.ml.ingestion.test_service import PAYLOAD, count_rows, ingress_environment


class MemoryBatchAdapter:
    """An independent test source; its payload never comes from a source path."""

    def __init__(self, stager):
        self.stager = stager
        self.calls = []

    def validate_source(self, source):
        if (
            source.adapter_type != "test-memory"
            or source.adapter_version != "1"
            or source.configuration_json != "{}"
        ):
            raise SourceRejected("Invalid approved in-memory test source")

    def acquire(self, extraction, control):
        self.validate_source(extraction.source)
        self.calls.append(extraction.execution)
        chunks = (PAYLOAD[:7], PAYLOAD[7:20], PAYLOAD[20:])
        return self.stager.stage(extraction, chunks, control)


class GeneratedBatchAdapter:
    """A third source streams independently generated records without subclassing File/Memory."""

    def __init__(self, stager):
        self.stager = stager
        self.calls = []

    def validate_source(self, source):
        if (
            source.adapter_type != "test-generated"
            or source.adapter_version != "1"
            or source.configuration_json != "{}"
        ):
            raise SourceRejected("Invalid approved generated test source")

    def acquire(self, extraction, control):
        self.validate_source(extraction.source)
        self.calls.append(extraction.execution)

        def records():
            yield b'{"event_id":1,"value":"first"}\n'
            yield b'{"event_id":2,"value":"second"}\n'

        return self.stager.stage(extraction, records(), control)


@pytest.fixture(params=["file", "test-memory", "test-generated"])
def registered_source(request, components, actor, tmp_path):
    base, settings, operator, file_source, path = ingress_environment(components, actor, tmp_path)
    memory = MemoryBatchAdapter(base.store)
    generated = GeneratedBatchAdapter(base.store)
    submit_code = IngestionService.submit.__code__
    extended = build_ingestion(
        settings,
        database=base.database,
        additional_registrations=(
            (AdapterDescriptor("test-memory", "1"), memory, memory.validate_source),
            (AdapterDescriptor("test-generated", "1"), generated, generated.validate_source),
        ),
    )
    assert extended.service.__class__ is base.service.__class__ is IngestionService
    assert IngestionService.submit.__code__ is submit_code
    selected = file_source
    adapter_type = request.param
    if adapter_type != "file":
        selected = replace(
            file_source, source_id=adapter_type, adapter_type=adapter_type, configuration_json="{}"
        )
        extended.registry.validate_source(selected)
        extended.sources.register(operator, selected)
    try:
        yield extended, operator, selected, path, memory, generated
    finally:
        extended.close()
        base.close()


class CooperativeControl:
    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.checks = 0

    def check(self):
        self.checks += 1
        if self.cancelled:
            raise IngestionCancelled("Test cancellation")


def test_shared_batch_adapter_contract_returns_bounded_private_verifiable_staging(
    registered_source,
):
    ingress, actor, source, _, _, _ = registered_source
    job = ingress.service.submit(
        actor, "adapter-contract", IngestionRequest(source.source_id, "v1")
    )
    claim = ingress.jobs.claim_next(job_id=job.job_id)
    extraction = BatchExtraction(source, claim.execution, source.limits)
    control = CooperativeControl()
    adapter = ingress.registry.resolve(source.adapter_type, source.adapter_version)
    staged = adapter.acquire(extraction, control)
    assert staged.tenant_id == actor.tenant_id and staged.attempt_id == claim.job.attempt_id
    assert staged.job_id == job.job_id
    assert staged.content_digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert staged.size_bytes == len(PAYLOAD) and staged.record_count == 2
    assert staged.data_format == "jsonl" and staged.schema_version == "raw-json-object-v1"
    assert control.checks >= 2
    intent = ingress.store.prepare(extraction, staged)
    assert intent.staged == staged
    assert ingress.store.lookup(actor.tenant_id, job.snapshot_id, intent.manifest_digest) is None
    assert count_rows(ingress.database, raw_snapshots) == 0
    ingress.store.discard(staged)
    assert not Path(staged.staging_ref).exists()


def test_shared_batch_adapter_contract_observes_cancellation_before_returning_evidence(
    registered_source,
):
    ingress, actor, source, _, _, _ = registered_source
    job = ingress.service.submit(actor, "adapter-cancel", IngestionRequest(source.source_id, "v1"))
    claim = ingress.jobs.claim_next(job_id=job.job_id)
    extraction = BatchExtraction(source, claim.execution, source.limits)
    with pytest.raises(IngestionCancelled):
        ingress.registry.resolve(source.adapter_type, source.adapter_version).acquire(
            extraction, CooperativeControl(cancelled=True)
        )
    assert count_rows(ingress.database, raw_snapshots) == 0


def test_same_ingestion_service_and_state_machine_accept_file_memory_and_generated_sources(
    registered_source,
):
    ingress, actor, source, input_path, memory, generated = registered_source
    datasets_before = count_rows(ingress.database, datasets)
    # Only the File Adapter needs the approved external file. Other registered
    # implementations must succeed even when no such filesystem source exists.
    if source.adapter_type != "file":
        input_path.unlink()
    job = ingress.service.submit(actor, "ocp-workflow", IngestionRequest(source.source_id, "v1"))
    completed = ingress.supervisor().run_once(job_id=job.job_id)
    assert completed.state == "PUBLISHED"
    snapshot = ingress.service.result(actor, job.job_id)
    assert snapshot.content_digest == hashlib.sha256(PAYLOAD).hexdigest()
    assert snapshot.source_id == source.source_id and snapshot.record_count == 2
    assert (Path(snapshot.snapshot_ref).parent / "data.jsonl").read_bytes() == PAYLOAD
    assert count_rows(ingress.database, datasets) == datasets_before
    assert len(memory.calls) == (1 if source.adapter_type == "test-memory" else 0)
    assert len(generated.calls) == (1 if source.adapter_type == "test-generated" else 0)
    with pytest.raises(SourceRejected):
        ingress.registry.resolve(source.adapter_type, "1", "stream_consume")


def test_unknown_or_unsupported_adapter_never_falls_back_to_file(components, actor, tmp_path):
    ingress, _, operator, source, _ = ingress_environment(components, actor, tmp_path)
    try:
        unapproved = replace(source, source_id="unknown", adapter_type="not-registered")
        ingress.sources.register(operator, unapproved)
        with pytest.raises(SourceRejected):
            ingress.service.submit(operator, "unknown", IngestionRequest("unknown", "v1"))
        with pytest.raises(SourceRejected):
            ingress.registry.resolve("file", "1", "stream_consume")
        assert count_rows(ingress.database, raw_snapshots) == 0
    finally:
        ingress.close()
