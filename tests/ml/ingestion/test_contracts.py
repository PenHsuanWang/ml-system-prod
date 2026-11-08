"""Framework-free ingress contracts and legal operation transitions."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from src.domain.ingestion.errors import IngestionError
from src.domain.ingestion.models import (
    AdapterDescriptor,
    BatchExtraction,
    ExtractionLimits,
    IngestionExecution,
    IngestionRequest,
    PublicationIntent,
    RawSnapshot,
    SourceDefinition,
    StagedBatch,
)
from src.domain.ingestion.states import TERMINAL, validate_transition
from src.domain.ml import Conflict, canonical_json, digest


def source(**overrides):
    values = {
        "source_id": "events",
        "version": "v1",
        "tenant_id": "tenant-a",
        "adapter_type": "file",
        "adapter_version": "1",
        "configuration_json": '{"root":"/operator/approved","path":"events.jsonl"}',
    }
    return SourceDefinition(**{**values, **overrides})


def staged(**overrides):
    values = {
        "staging_ref": "/private/staging/attempt/data.jsonl",
        "tenant_id": "tenant-a",
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "content_digest": "a" * 64,
        "size_bytes": 12,
        "record_count": 1,
        "data_format": "jsonl",
        "schema_version": "raw-json-object-v1",
        "source_revision": "sha256:" + "a" * 64,
    }
    return StagedBatch(**{**values, **overrides})


def test_source_definition_is_canonical_versioned_and_roundtrips():
    first = source(configuration_json='{"path":"events.jsonl", "root":"/approved"}')
    second = source(configuration_json='{"root":"/approved","path":"events.jsonl"}')
    assert first.configuration_digest == second.configuration_digest
    assert first.configuration_json == second.configuration_json
    assert SourceDefinition.parse(json.loads(canonical_json(first.to_dict()))) == first
    assert replace(first, version="v2").configuration_digest != first.configuration_digest
    assert replace(first, tenant_id="tenant-b").configuration_digest != first.configuration_digest
    assert (
        replace(first, allowed_actor_ids=("scientist",)).configuration_digest
        != first.configuration_digest
    )
    with pytest.raises(FrozenInstanceError):
        first.source_id = "other"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_id", "../source"),
        ("source_id", "/absolute"),
        ("tenant_id", ""),
        ("version", "bad version"),
        ("adapter_type", "module.arbitrary:Class"),
        ("configuration_json", "[]"),
        ("configuration_json", "{invalid-json"),
        ("configuration_json", '{"secret":"' + "x" * 17000 + '"}'),
        ("allowed_actor_ids", ("owner", "owner")),
        ("allowed_actor_ids", ["owner"]),
    ],
)
def test_source_definition_rejects_invalid_identity_and_configuration(field, value):
    with pytest.raises(IngestionError):
        source(**{field: value})


def test_optional_request_limits_only_narrow_approved_source_budget():
    approved = ExtractionLimits(
        max_bytes=4096, max_records=20, max_line_bytes=1024, timeout_seconds=10
    )
    assert IngestionRequest("events", "v1").bounded_by(approved) == approved
    narrowed = IngestionRequest("events", "v1", 1024, 10, 5).bounded_by(approved)
    assert narrowed == ExtractionLimits(1024, 10, 1024, 5)
    assert ExtractionLimits.parse(json.loads(canonical_json(narrowed.to_dict()))) == narrowed
    for values in ({"max_bytes": 4097}, {"max_records": 21}, {"timeout_seconds": 11}):
        with pytest.raises(IngestionError):
            IngestionRequest("events", "v1", **values).bounded_by(approved)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_bytes": 128 * 1024 * 1024 + 1},
        {"max_records": 0},
        {"max_records": 1_000_001},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"timeout_seconds": 301},
    ],
)
def test_extraction_request_rejects_unbounded_or_ambiguous_limits(kwargs):
    with pytest.raises(IngestionError):
        IngestionRequest("events", "v1", **kwargs)


def test_generic_extraction_and_snapshot_metadata_keep_model_data_opaque():
    approved = source()
    execution = IngestionExecution("tenant-a", "job-1", "attempt-1", "snapshot-1", 1000)
    extraction = BatchExtraction(approved, execution, ExtractionLimits())
    assert extraction.source == approved
    evidence = staged()
    assert StagedBatch.parse(json.loads(canonical_json(evidence.to_dict()))) == evidence
    manifest = {"snapshot_id": "snapshot-1", "tenant_id": "tenant-a", "job_id": "job-1"}
    intent = PublicationIntent(
        "snapshot-1", "tenant-a", "job-1", evidence, canonical_json(manifest), digest(manifest)
    )
    assert PublicationIntent.parse(json.loads(canonical_json(intent.to_dict()))) == intent
    snapshot = RawSnapshot(
        "snapshot-1",
        "tenant-a",
        "events",
        "v1",
        approved.configuration_digest,
        evidence.content_digest,
        intent.manifest_digest,
        "/private/raw/snapshot-1/manifest.json",
        "jsonl",
        "raw-json-object-v1",
        evidence.size_bytes,
        evidence.record_count,
        1000,
        "job-1",
        evidence.source_revision,
    )
    assert RawSnapshot.parse(json.loads(canonical_json(snapshot.to_dict()))) == snapshot
    assert "model" not in snapshot.to_dict() and "recipe" not in snapshot.to_dict()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content_digest": "A" * 64},
        {"content_digest": "not-a-digest"},
        {"size_bytes": 0},
        {"size_bytes": True},
        {"record_count": 0},
        {"source_revision": "revision with whitespace"},
    ],
)
def test_staged_evidence_rejects_invalid_digest_count_or_revision(kwargs):
    with pytest.raises(IngestionError):
        staged(**kwargs)


def test_publication_intent_binds_staged_ownership_and_manifest_digest():
    manifest = {"version": "raw-v1"}
    for tenant, job, sha in (
        ("other", "job-1", digest(manifest)),
        ("tenant-a", "other", digest(manifest)),
        ("tenant-a", "job-1", "b" * 64),
    ):
        with pytest.raises(IngestionError):
            PublicationIntent("snapshot-1", tenant, job, staged(), canonical_json(manifest), sha)


def test_adapter_capabilities_reject_unknown_or_duplicate_operations():
    for values in ((), ("batch_extract", "batch_extract"), ("arbitrary_operation",)):
        with pytest.raises(IngestionError):
            AdapterDescriptor("file", "1", values)


def test_adapter_capabilities_cannot_change_through_a_mutable_constructor_argument():
    values = ["batch_extract"]
    try:
        descriptor = AdapterDescriptor("file", "1", values)
    except IngestionError:
        return
    values.append("stream_consume")
    assert descriptor.capabilities == ("batch_extract",)


def test_publication_uncertainty_is_recoverable_and_terminals_never_reopen():
    for before, after in (
        ("QUEUED", "ACQUIRING"),
        ("ACQUIRING", "VALIDATING"),
        ("VALIDATING", "PUBLISHING"),
        ("PUBLISHING", "NEEDS_RECONCILIATION"),
        ("NEEDS_RECONCILIATION", "PUBLISHING"),
        ("PUBLISHING", "PUBLISHED"),
        ("ACQUIRING", "RETRY_WAIT"),
        ("RETRY_WAIT", "ACQUIRING"),
    ):
        validate_transition(before, after)
    for terminal in TERMINAL:
        for target in ("QUEUED", "ACQUIRING", "PUBLISHING", *TERMINAL):
            with pytest.raises(Conflict):
                validate_transition(terminal, target)
    with pytest.raises(Conflict):
        validate_transition("PUBLISHING", "CANCELLED")
