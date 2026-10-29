import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from bootstrap.ml_config import MLSettings
from src.domain.ml import canonical_json, CapacityExceeded, MLError
from src.infrastructure.execution.local_process import LocalProcessExecutor, process_identity
from src.infrastructure.serving.artifacts import ArtifactVerifier
from src.infrastructure.serving.bounded_prediction import BoundedPredictionExecutor, PredictionDeadline


def manifest_fixture(root):
    names = ("weights.pt", "preprocessing.json", "feature_schema.json",
             "output_mapping.json", "golden_io.json")
    for name in names:
        (root/name).write_bytes(b"fixture")
    manifest = {"manifest_version": "2", "artifact": {"format": "trusted_pytorch_state_dict",
                                                      "files": [{"path": name, "sha256": hashlib.sha256(b"fixture").hexdigest(), "size_bytes": 7}
                                                                for name in names]}}
    return manifest


def write_manifest(root, manifest):
    data = canonical_json(manifest).encode()
    (root/"manifest.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("attack", ["digest", "traversal", "symlink", "missing", "size"])
def test_artifact_attacks_rejected_before_deserialization(tmp_path, attack):
    value = manifest_fixture(tmp_path)
    if attack == "traversal":
        value["artifact"]["files"][0]["path"] = "../outside.pt"
    elif attack == "missing":
        value["artifact"]["files"].pop()
    elif attack == "size":
        value["artifact"]["files"][0]["size_bytes"] = 100
    expected = write_manifest(tmp_path, value)
    if attack == "digest":
        (tmp_path/"weights.pt").write_bytes(b"tampered")
    if attack == "symlink":
        outside = tmp_path.parent/(tmp_path.name+"-outside")
        outside.write_bytes(b"fixture")
        (tmp_path/"weights.pt").unlink()
        (tmp_path/"weights.pt").symlink_to(outside)
    with pytest.raises(MLError):
        ArtifactVerifier().verify(tmp_path, expected)


def test_prediction_timeout_keeps_physical_capacity():
    started, release = threading.Event(), threading.Event()

    class BlockingRuntime:
        def predict(self, values):
            started.set()
            release.wait(timeout=5)
            return [{"class_id": 1, "label": "positive", "score": 0.8}]

        def close(self):
            pass
    executor = BoundedPredictionExecutor(BlockingRuntime(), capacity=1, timeout=0.02)
    try:
        with pytest.raises(PredictionDeadline):
            executor.predict(((1.0, 1.0),))
        assert started.is_set()
        with pytest.raises(CapacityExceeded):
            executor.predict(((1.0, 1.0),))
    finally:
        release.set()
        executor.close()


def test_missing_child_identity_never_proves_death(tmp_path):
    executor = LocalProcessExecutor(tmp_path, timeout=10, grace=1)
    handle = executor.prepare("{}", "attempt", "owner")
    assert executor.inspect(handle).state == "UNKNOWN"
    assert not executor.stop(handle)


def test_pid_reuse_does_not_signal_an_unrelated_process(tmp_path, monkeypatch):
    executor = LocalProcessExecutor(tmp_path, timeout=10, grace=1)
    handle = executor.prepare("{}", "attempt", "owner")
    identity = process_identity(os.getpid())
    identity["start_ticks"] = "wrong-generation"
    (Path(handle.directory)/"process.json").write_text(json.dumps(identity))

    def forbidden_signal(*args):
        raise AssertionError("Must never signal a recycled PID")
    monkeypatch.setattr(os, "killpg", forbidden_signal)
    assert executor.stop(handle)


def test_explicit_migration_is_idempotent(tmp_path):
    from bootstrap.ml_cli import migrate
    from src.infrastructure.persistence.ml_database import Database
    settings = MLSettings(database_url="sqlite:///"+str(tmp_path/"fresh"/"ml.db"))
    migrate(settings)
    migrate(settings)
    db = Database(settings.database_url)
    assert db.ready()
    db.close()


def test_non_dev_cannot_select_local_authority_or_unqualified_runtime():
    with pytest.raises(ValueError):
        MLSettings(environment="prod").validate()


def test_reachable_old_schema_is_not_ready(tmp_path):
    from sqlalchemy.exc import SQLAlchemyError
    from src.infrastructure.persistence.ml_database import Database
    db = Database("sqlite:///"+str(tmp_path/"old.db"))
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE training_jobs (id TEXT PRIMARY KEY)")
        with pytest.raises(SQLAlchemyError):
            db.ready()
    finally:
        db.close()
