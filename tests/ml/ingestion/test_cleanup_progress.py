"""Maintenance advances across protected work/history and separate invocations."""

import json
import os
import time

import pytest

from src.domain.ingestion.errors import SnapshotInvalid
from src.domain.ml import digest
from src.infrastructure.ingestion.storage.raw_snapshot_writer import LocalRawSnapshotStore


def test_cleanup_resumes_past_5000_protected_jobs_after_store_restart(tmp_path):
    store = LocalRawSnapshotStore(tmp_path / "raw")
    tenant = store.root / ".staging" / digest("tenant-a")
    tenant.mkdir()
    for index in range(5100):
        (tenant / f"job-{index:05}").mkdir()
    # Directory enumeration is filesystem-specific. Pick the actual last entry
    # without assuming a name or insertion order.
    jobs = list(tenant.iterdir())
    parent = jobs[-1]
    orphan = parent / ("attempt-" + "a" * 32)
    orphan.mkdir()
    (orphan / "data.jsonl").write_bytes(b"{}\n")
    old = time.time() - 1000
    os.utime(orphan, (old, old))
    protected = frozenset(job.name for job in jobs[:-1])
    cutoff = time.time() - 10

    assert store.cleanup_abandoned(protected, cutoff) == 0
    assert orphan.exists()
    # A fresh CLI/store instance must continue from the persisted cursor.
    restarted = LocalRawSnapshotStore(store.root)
    assert restarted.cleanup_abandoned(protected, cutoff) == 1
    assert not orphan.exists()
    assert all(job.exists() for job in jobs[:-1])


def test_publication_cleanup_advances_past_final_snapshots_and_protected_copies(tmp_path):
    store = LocalRawSnapshotStore(tmp_path / "raw")
    tenant = store.root / "published" / digest("tenant-a")
    tenant.mkdir()
    finals = []
    protected = set()
    for index in range(5100):
        final = tenant / f"snapshot-{index:05}"
        final.mkdir()
        finals.append(final)
        job = f"job-{index:05}"
        (tenant / (f".publish-{job}-" + "a" * 32)).mkdir()
        protected.add(job)
    # There are >5000 private copies in addition to immutable final history,
    # so at least one private copy is beyond any 5000-entry prefix, in any order.
    copies = [path for path in tenant.iterdir() if path.name.startswith(".publish-")]
    orphan = copies[-1]
    job_id = orphan.name[len(".publish-") : -33]
    protected.remove(job_id)
    (orphan / "data.jsonl").write_bytes(b"{}\n")
    old = time.time() - 1000
    os.utime(orphan, (old, old))
    cutoff = time.time() - 10
    counts = []
    for _ in range(4):
        counts.append(
            LocalRawSnapshotStore(store.root).cleanup_abandoned(frozenset(protected), cutoff)
        )
    assert counts[0] == 0
    assert sum(counts) == 1
    assert not orphan.exists()
    assert all(final.is_dir() for final in finals)
    assert all(path.is_dir() for path in copies[:-1])


def test_cleanup_cursor_refuses_symlink_and_corrupt_path(tmp_path):
    store = LocalRawSnapshotStore(tmp_path / "raw")
    outside = tmp_path / "outside"
    outside.write_text("preserve-me")
    cursor = store.root / ".cleanup-staging.json"
    cursor.symlink_to(outside)
    with pytest.raises(SnapshotInvalid):
        store.cleanup_abandoned(frozenset(), time.time() - 10)
    assert outside.read_text() == "preserve-me"
    cursor.unlink()
    cursor.write_text(
        json.dumps(
            {
                "version": 1,
                "frames": [{"parts": [".."], "cookie": 0, "device": 0, "inode": 0}],
            }
        )
    )
    with pytest.raises(SnapshotInvalid):
        store.cleanup_abandoned(frozenset(), time.time() - 10)
    assert outside.read_text() == "preserve-me"


def test_replaced_namespace_directory_restarts_its_cookie_safely(tmp_path, monkeypatch):
    from src.infrastructure.ingestion.storage import raw_snapshot_cleanup

    monkeypatch.setattr(raw_snapshot_cleanup, "_BUDGET", 4)
    store = LocalRawSnapshotStore(tmp_path / "raw")
    namespace = store.root / ".staging"
    for index in range(10):
        (namespace / digest(f"tenant-{index}")).mkdir()
    store.cleanup_abandoned(frozenset(), time.time() - 10)
    renamed = store.root / "previous-staging"
    namespace.rename(renamed)
    namespace.mkdir()
    tenant = namespace / digest("new-tenant")
    tenant.mkdir()
    job = tenant / "orphan-job"
    job.mkdir()
    attempt = job / ("attempt-" + "c" * 32)
    attempt.mkdir()
    (attempt / "data.jsonl").write_bytes(b"{}\n")
    old = time.time() - 1000
    os.utime(attempt, (old, old))
    removed = 0
    for _ in range(10):
        removed += LocalRawSnapshotStore(store.root).cleanup_abandoned(
            frozenset(), time.time() - 10
        )
    assert removed == 1
    assert not attempt.exists()
