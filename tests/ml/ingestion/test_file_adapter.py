"""Approved local acquisition: bounded records, race-resistant paths and cleanup."""

import errno
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from src.domain.ingestion.errors import (
    IngestionCancelled,
    IngestionError,
    SourceChanged,
    SourceRejected,
)
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    IngestionExecution,
    SourceDefinition,
)
from src.infrastructure.ingestion.adapters.file_batch import FileBatchAdapter
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore


class Control:
    def check(self):
        pass


def extraction(
    *,
    source_type="file",
    config=None,
    tenant="tenant-a",
    job="job-a",
    attempt="attempt-a",
    snapshot="snapshot-a",
    limits=None,
):
    approved = limits or ExtractionLimits()
    source = SourceDefinition(
        "source-a",
        "v1",
        tenant,
        source_type,
        "1",
        json.dumps(config or {"root_key": "input", "relative_path": "records.jsonl"}),
        approved,
    )
    return BatchExtraction(
        source, IngestionExecution(tenant, job, attempt, snapshot, 1793404800.0), approved
    )


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    path = root / "records.jsonl"
    path.write_bytes(b'{"lot":"LOT-001","measure":12.5}\n{"lot":"LOT-002","measure":13}\n')
    store = LocalRawSnapshotStore(tmp_path / "raw")
    return root, path, store, FileBatchAdapter({"input": root}, store)


def test_real_jsonl_acquisition_preserves_exact_bytes_and_provenance(setup):
    _, path, store, adapter = setup
    result = adapter.acquire(extraction(), Control())
    assert result.content_digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.source_revision == "sha256:" + result.content_digest
    assert result.record_count == 2
    assert result.size_bytes == path.stat().st_size
    assert (Path(result.staging_ref) / "data.jsonl").read_bytes() == path.read_bytes()
    assert not any((store.root / "published").iterdir())
    store.discard(result)
    assert not Path(result.staging_ref).exists()


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"\n",
        b"[]\n",
        b"{}\n\n",
        b'{"x":NaN}\n',
        b'{"x":1e999}\n',
        b'{"x":1,"x":2}\n',
        b'{"x":\xff}\n',
        b'{"x":}\n',
        b"pickle\x80\x04",
    ],
)
def test_empty_invalid_or_ambiguous_jsonl_never_leaves_staging(setup, value):
    _, path, store, adapter = setup
    path.write_bytes(value)
    with pytest.raises(IngestionError):
        adapter.acquire(extraction(), Control())
    assert not list((store.root / ".staging").rglob("data.jsonl"))


@pytest.mark.parametrize("case", ["file", "line", "rows"])
def test_approved_byte_line_and_record_limits_are_enforced(setup, case):
    _, path, store, adapter = setup
    limits = (
        ExtractionLimits(max_bytes=10)
        if case == "file"
        else (
            ExtractionLimits(max_line_bytes=10)
            if case == "line"
            else ExtractionLimits(max_records=1)
        )
    )
    with pytest.raises(IngestionError):
        adapter.acquire(extraction(limits=limits), Control())
    assert not list((store.root / ".staging").rglob("data.jsonl"))
    assert path.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "/etc/passwd",
        "../outside.jsonl",
        "x/../../outside",
        "x//data.jsonl",
        "./records.jsonl",
        "x\\records.jsonl",
        "records.jsonl\x00",
    ],
)
def test_invalid_relative_locations_rejected_without_access(setup, relative):
    _, _, _, adapter = setup
    with pytest.raises(SourceRejected):
        adapter.validate_source(
            extraction(config={"root_key": "input", "relative_path": relative}).source
        )


@pytest.mark.parametrize("case", ["unknown_root", "extra_key", "digest", "format", "schema"])
def test_unapproved_configuration_and_formats_are_rejected(setup, case):
    _, _, _, adapter = setup
    config = {"root_key": "input", "relative_path": "records.jsonl"}
    if case == "unknown_root":
        config["root_key"] = "not-approved"
    elif case == "extra_key":
        config["uri"] = "http://unapproved"
    elif case == "digest":
        config["expected_sha256"] = "not-a-digest"
    source = extraction(config=config).source
    if case == "format":
        source = replace(source, data_format="pickle")
    if case == "schema":
        source = replace(source, schema_version="ebm-features")
    with pytest.raises(SourceRejected):
        adapter.validate_source(source)


@pytest.mark.parametrize("case", ["file", "parent", "root"])
def test_symlink_escapes_are_rejected_at_each_directory_component(setup, tmp_path, case):
    root, path, store, adapter = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "records.jsonl").write_bytes(b'{"outside":true}\n')
    if case == "file":
        path.unlink()
        path.symlink_to(outside / "records.jsonl")
        request = extraction()
    elif case == "parent":
        (root / "linked").symlink_to(outside, target_is_directory=True)
        request = extraction(config={"root_key": "input", "relative_path": "linked/records.jsonl"})
    else:
        alias = tmp_path / "root-link"
        alias.symlink_to(root, target_is_directory=True)
        adapter = FileBatchAdapter({"input": alias}, store)
        request = extraction()
    with pytest.raises(IngestionError):
        adapter.acquire(request, Control())
    assert not list((store.root / ".staging").rglob("data.jsonl"))


def test_fifo_is_rejected_without_blocking(setup):
    _, path, _, adapter = setup
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(IngestionError):
        adapter.acquire(extraction(), Control())


def test_parent_replacement_race_cannot_redirect_an_open_directory_fd(setup, tmp_path, monkeypatch):
    root, _, store, adapter = setup
    nested = root / "nested"
    nested.mkdir()
    original_content = b'{"approved":"original-directory"}\n'
    (nested / "records.jsonl").write_bytes(original_content)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "records.jsonl").write_bytes(b'{"secret":"outside-root"}\n')
    original_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "nested" and not swapped:
            swapped = True
            nested.rename(root / "old-nested")
            nested.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", swapping_open)
    result = adapter.acquire(
        extraction(config={"root_key": "input", "relative_path": "nested/records.jsonl"}), Control()
    )
    assert swapped
    assert (Path(result.staging_ref) / "data.jsonl").read_bytes() == original_content
    assert result.content_digest == hashlib.sha256(original_content).hexdigest()
    store.discard(result)


@pytest.mark.parametrize("failure", [False, True])
def test_source_file_descriptor_is_closed_after_success_and_failure(setup, monkeypatch, failure):
    _, path, _, adapter = setup
    if failure:
        path.write_bytes(b"invalid json\n")
    original_open = os.open
    source_descriptors = []

    def observing_open(name, flags, *args, **kwargs):
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == "records.jsonl":
            source_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", observing_open)
    if failure:
        with pytest.raises(IngestionError):
            adapter.acquire(extraction(), Control())
    else:
        adapter.acquire(extraction(), Control())
    assert source_descriptors
    for descriptor in source_descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def test_pinned_content_digest_mismatch_cleans_staged_result(setup):
    _, _, store, adapter = setup
    request = extraction(
        config={"root_key": "input", "relative_path": "records.jsonl", "expected_sha256": "a" * 64}
    )
    with pytest.raises(SourceChanged):
        adapter.acquire(request, Control())
    assert not list((store.root / ".staging").rglob("data.jsonl"))


@pytest.mark.parametrize("case", ["truncate", "rewrite", "grow", "interrupt", "permission"])
def test_changed_or_interrupted_source_closes_handles_and_discards_stage(setup, monkeypatch, case):
    _, path, store, adapter = setup
    original = os.read
    altered = False

    def changed_read(descriptor, size):
        nonlocal altered
        if case == "permission":
            raise PermissionError(errno.EACCES, "sensitive-path-token")
        if case == "interrupt":
            raise OSError(errno.EIO, "sensitive-path-token")
        value = original(descriptor, size)
        if value and not altered:
            altered = True
            if case == "truncate":
                path.write_bytes(b"{}\n")
            elif case == "rewrite":
                path.write_bytes(b" " * path.stat().st_size)
            else:
                with path.open("ab") as stream:
                    stream.write(b'{"new":true}\n')
        return value

    monkeypatch.setattr(os, "read", changed_read)
    with pytest.raises(IngestionError) as error:
        adapter.acquire(extraction(), Control())
    assert "sensitive-path-token" not in str(error.value)
    assert not list((store.root / ".staging").rglob("data.jsonl"))


def test_cancelled_extraction_does_not_publish_or_retain_partial_data(setup):
    _, _, store, adapter = setup

    class Cancel:
        count = 0

        def check(self):
            self.count += 1
            if self.count == 5:
                raise IngestionCancelled("Cancelled")

    with pytest.raises(IngestionCancelled):
        adapter.acquire(extraction(), Cancel())
    assert not list((store.root / ".staging").rglob("data.jsonl"))
    assert not any((store.root / "published").iterdir())
