"""Raw snapshots are immutable artifacts, separate from any ML Dataset Version."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, get_ident

import pytest

from src.domain.ingestion.errors import (
    IngestionError,
    PublicationUncertain,
    SnapshotConflict,
    SnapshotInvalid,
)
from src.domain.ingestion.models import PublicationIntent
from src.domain.ml import canonical_json, digest
from src.infrastructure.ingestion.storage import raw_snapshot_writer
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore
from tests.ml.ingestion.test_file_adapter import Control, extraction


@pytest.fixture
def staged(tmp_path):
    store = LocalRawSnapshotStore(tmp_path / "raw")
    request = extraction()
    batch = store.stage(request, [b'{"wafer":"W1"}\n', b'{"wafer":"W2"}\n'], Control())
    return store, request, batch


def test_prepare_and_publish_exact_raw_manifest_without_model_or_credentials(staged):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    assert (
        store.lookup(
            request.execution.tenant_id, request.execution.snapshot_id, intent.manifest_digest
        )
        is None
    )
    manifest = json.loads(intent.manifest_json)
    assert manifest["manifest_version"] == "raw-snapshot-v1"
    assert manifest["source_configuration_digest"] == request.source.configuration_digest
    assert manifest["source_revision"] == batch.source_revision
    assert "relative_path" not in intent.manifest_json
    assert "root_key" not in intent.manifest_json
    assert "attempt_id" not in intent.manifest_json
    snapshot = store.publish(intent)
    assert store.verify(snapshot) == snapshot
    assert snapshot.record_count == 2
    assert snapshot.created_at == request.execution.created_at
    assert Path(snapshot.snapshot_ref).parent.name == request.execution.snapshot_id
    assert (
        hashlib.sha256(Path(snapshot.snapshot_ref).read_bytes()).hexdigest()
        == intent.manifest_digest
    )
    assert Path(batch.staging_ref).exists(), "SQL success owns the decision to discard staging"


def test_identical_republication_is_idempotent_even_after_staging_cleanup(staged):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    snapshot = store.publish(intent)
    original = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in Path(snapshot.snapshot_ref).parent.iterdir()
    }
    store.discard(batch)
    assert store.publish(intent) == snapshot
    assert (
        store.lookup(snapshot.tenant_id, snapshot.snapshot_id, snapshot.manifest_digest) == snapshot
    )
    assert original == {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in Path(snapshot.snapshot_ref).parent.iterdir()
    }


def test_same_snapshot_identity_cannot_be_overwritten_by_new_content(staged):
    store, request, batch = staged
    first = store.publish(store.prepare(request, batch))
    new_batch = store.stage(request, [b'{"replacement":true}\n'], Control())
    with pytest.raises(SnapshotConflict):
        store.publish(store.prepare(request, new_batch))
    assert store.verify(first) == first
    assert Path(batch.staging_ref).exists()
    assert Path(new_batch.staging_ref).exists()


@pytest.mark.parametrize("case", ["payload", "stage_metadata", "count", "location", "tenant"])
def test_staging_tamper_or_identity_forgery_is_rejected_before_publication(staged, tmp_path, case):
    store, request, batch = staged
    if case == "payload":
        (Path(batch.staging_ref) / "data.jsonl").write_bytes(b'{"tampered":true}\n')
    elif case == "stage_metadata":
        path = Path(batch.staging_ref) / "stage.json"
        value = json.loads(path.read_text())
        value["size_bytes"] += 1
        path.write_text(json.dumps(value))
    elif case == "count":
        batch = replace(batch, record_count=batch.record_count + 1)
    elif case == "location":
        outside = tmp_path / "outside"
        outside.mkdir()
        batch = replace(batch, staging_ref=str(outside))
    else:
        batch = replace(batch, tenant_id="other-tenant")
    with pytest.raises(IngestionError):
        store.prepare(request, batch)
    assert not any((store.root / "published").iterdir())


def test_publication_revalidates_bytes_after_prepare(staged):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    (Path(batch.staging_ref) / "data.jsonl").write_bytes(b'{"changed":true}\n')
    with pytest.raises(SnapshotInvalid):
        store.publish(intent)
    assert not any((store.root / "published").iterdir())


@pytest.mark.parametrize("case", ["tenant", "snapshot", "format", "source_revision"])
def test_publication_rejects_forged_manifest_even_with_recomputed_digest(staged, case):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    manifest = json.loads(intent.manifest_json)
    if case == "tenant":
        manifest["tenant_id"] = "other"
    elif case == "snapshot":
        manifest["snapshot_id"] = "other"
    elif case == "format":
        manifest["data_format"] = "pickle"
    else:
        manifest["source_revision"] = "secret value with spaces"
    forged = PublicationIntent(
        intent.snapshot_id,
        intent.tenant_id,
        intent.job_id,
        batch,
        canonical_json(manifest),
        digest(manifest),
    )
    with pytest.raises(SnapshotInvalid):
        store.publish(forged)


def test_published_read_rejects_tampering_and_external_paths(staged):
    store, request, batch = staged
    snapshot = store.publish(store.prepare(request, batch))
    with pytest.raises(SnapshotInvalid):
        store.verify(replace(snapshot, snapshot_ref=str(store.root.parent / "manifest.json")))
    payload = Path(snapshot.snapshot_ref).parent / "data.jsonl"
    payload.write_bytes(b'{"corrupt":true}\n')
    with pytest.raises(SnapshotInvalid):
        store.verify(snapshot)


def test_concurrent_identical_publication_is_one_immutable_snapshot(staged):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    with ThreadPoolExecutor(max_workers=6) as workers:
        snapshots = list(workers.map(lambda _: store.publish(intent), range(12)))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert (
        len(list((store.root / "published" / digest(request.execution.tenant_id)).iterdir())) == 1
    )


def test_missing_tenant_lookup_remains_a_miss_when_identical_publisher_finishes(
    staged, monkeypatch
):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    parent = store._final(intent.tenant_id, intent.snapshot_id).parent
    original = raw_snapshot_writer.directory_fd
    observed_missing, published = Event(), Event()
    reader = get_ident()
    first_lookup = True
    results = {}

    @contextmanager
    def interleaved(path, *, create=False):
        nonlocal first_lookup
        if get_ident() == reader and path == parent and not create and first_lookup:
            first_lookup = False
            try:
                with original(path, create=create) as descriptor:
                    yield descriptor
            except FileNotFoundError:
                observed_missing.set()
                assert published.wait(5), "Competing publication did not complete"
                raise
        else:
            with original(path, create=create) as descriptor:
                yield descriptor

    def publish_competitor():
        try:
            assert observed_missing.wait(5), "Lookup did not observe a missing tenant"
            results["snapshot"] = store.publish(intent)
        except BaseException as error:
            results["error"] = error
        finally:
            published.set()

    monkeypatch.setattr(raw_snapshot_writer, "directory_fd", interleaved)
    competitor = Thread(target=publish_competitor)
    competitor.start()
    try:
        result = store.publish(intent)
    finally:
        competitor.join(timeout=5)
    assert not competitor.is_alive()
    assert "error" not in results, results.get("error")
    assert result == results["snapshot"]
    assert store.verify(result) == result


def test_failure_before_atomic_rename_exposes_no_published_snapshot(staged, monkeypatch):
    store, request, batch = staged
    intent = store.prepare(request, batch)

    def interrupted(*args):
        assert not (store._final(intent.tenant_id, intent.snapshot_id) / "manifest.json").exists()
        raise OSError("sensitive-filesystem-detail")

    monkeypatch.setattr(raw_snapshot_writer, "_rename_exclusive", interrupted)
    with pytest.raises(PublicationUncertain) as error:
        store.publish(intent)
    assert "sensitive-filesystem-detail" not in str(error.value)
    assert store.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest) is None
    assert Path(batch.staging_ref).exists()
    assert not list((store.root / "published").rglob(".publish-*"))


def test_successful_rename_then_fsync_error_recovers_same_snapshot(staged, monkeypatch):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    original = raw_snapshot_writer._rename_exclusive

    def renamed_then_failed(*args):
        original(*args)
        raise OSError("simulated-after-rename-error")

    monkeypatch.setattr(raw_snapshot_writer, "_rename_exclusive", renamed_then_failed)
    with pytest.raises(PublicationUncertain):
        store.publish(intent)
    snapshot = store.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest)
    assert snapshot is not None
    assert snapshot.content_digest == batch.content_digest
    assert store.publish(intent) == snapshot


def test_atomic_no_replace_does_not_replace_existing_empty_target(staged):
    store, request, batch = staged
    intent = store.prepare(request, batch)
    target = store._final(intent.tenant_id, intent.snapshot_id)
    target.mkdir(parents=True)
    with pytest.raises(SnapshotInvalid):
        store.publish(intent)
    assert target.is_dir() and not list(target.iterdir())


def test_cleanup_only_removes_old_unprotected_private_attempts(staged, tmp_path):
    store, request, batch = staged
    preserved = store.stage(
        replace(
            request,
            execution=replace(request.execution, job_id="recovering-job", attempt_id="attempt-b"),
        ),
        [b'{"value":2}\n'],
        Control(),
    )
    snapshot = store.publish(store.prepare(request, batch))
    old = time.time() - 100
    os.utime(batch.staging_ref, (old, old))
    os.utime(preserved.staging_ref, (old, old))
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    alias = store.root / ".staging" / ("a" * 64)
    alias.symlink_to(outside, target_is_directory=True)
    assert store.cleanup_abandoned(frozenset({"recovering-job"}), time.time() - 10) == 1
    assert not Path(batch.staging_ref).exists()
    assert Path(preserved.staging_ref).exists()
    assert store.verify(snapshot) == snapshot
    assert (outside / "keep.txt").read_text() == "keep"
    assert store.cleanup_abandoned(frozenset({"recovering-job"}), time.time() - 10) == 0


@pytest.mark.parametrize("case", ["old", "young", "protected"])
def test_cleanup_crash_left_publication_copy_preserves_live_work_and_snapshots(
    staged, monkeypatch, case
):
    store, request, batch = staged
    published = store.publish(store.prepare(request, batch))
    published_bytes = {
        path.name: path.read_bytes() for path in Path(published.snapshot_ref).parent.iterdir()
    }
    crashed_request = replace(
        request,
        execution=replace(request.execution, job_id="crashed-job", snapshot_id="crashed-snapshot"),
    )
    crashed_batch = store.stage(crashed_request, [b'{"crashed":true}\n'], Control())
    intent = store.prepare(crashed_request, crashed_batch)

    def crash_before_rename(*args):
        raise OSError("simulated-process-death-before-rename")

    with monkeypatch.context() as crash:
        crash.setattr(raw_snapshot_writer, "_rename_exclusive", crash_before_rename)
        crash.setattr(store, "_discard_path", lambda *args: False)
        with pytest.raises(PublicationUncertain):
            store.publish(intent)
    parent = Path(published.snapshot_ref).parent.parent
    (temporary,) = parent.glob(".publish-crashed-job-*")
    assert (temporary / "manifest.json").is_file()
    assert store.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest) is None
    if case != "young":
        old = time.time() - 100
        os.utime(temporary, (old, old))
    protected = frozenset({"crashed-job"}) if case == "protected" else frozenset()
    removed = store.cleanup_abandoned(protected, time.time() - 10)
    assert removed == (1 if case == "old" else 0)
    assert temporary.exists() is (case != "old")
    assert store.verify(published) == published
    assert published_bytes == {
        path.name: path.read_bytes() for path in Path(published.snapshot_ref).parent.iterdir()
    }
    assert Path(crashed_batch.staging_ref).exists()


def test_publication_temp_gc_ignores_symlinks_unrecognized_names_and_final_identity(
    staged, tmp_path
):
    store, request, batch = staged
    published = store.publish(store.prepare(request, batch))
    parent = Path(published.snapshot_ref).parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.jsonl").write_bytes(b'{"keep":true}\n')
    alias = parent / (".publish-absent-job-" + "a" * 32)
    alias.symlink_to(outside, target_is_directory=True)
    unknown = parent / (".publish-" + "b" * 32)
    unknown.mkdir()
    (unknown / "data.jsonl").write_bytes(b'{"unknown":true}\n')
    old = time.time() - 100
    os.utime(unknown, (old, old))
    os.utime(Path(published.snapshot_ref).parent, (old, old))
    assert store.cleanup_abandoned(frozenset(), time.time() - 10) == 0
    assert alias.is_symlink()
    assert (outside / "data.jsonl").read_bytes() == b'{"keep":true}\n'
    assert unknown.is_dir()
    assert store.verify(published) == published
