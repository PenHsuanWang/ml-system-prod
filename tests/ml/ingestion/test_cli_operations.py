"""Real operator processes reject unsafe definitions and expose safe readiness."""

import json
import os
import subprocess
import sys

import pytest


def cli(components, tmp_path, *arguments, database_url=None):
    return subprocess.run(
        [sys.executable, "-m", "bootstrap.ingestion_cli", *arguments],
        env={
            **os.environ,
            "ML_DATABASE_URL": database_url or components.settings.database_url,
            "ML_RAW_SNAPSHOT_ROOT": str(tmp_path / "raw"),
            "ML_DATABRICKS_ENABLED": "false",
            "ML_INGESTION_FILE_ROOTS_JSON": "{}",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("kind", ["fifo", "symlink", "oversize", "malformed"])
def test_operator_definition_is_regular_bounded_and_errors_are_sanitized(
    components, tmp_path, kind
):
    definition = tmp_path / "definition.json"
    if kind == "fifo":
        os.mkfifo(definition)
    elif kind == "symlink":
        other = tmp_path / "private.json"
        other.write_text("SECRET-MARKER")
        definition.symlink_to(other)
    elif kind == "oversize":
        definition.write_bytes(b" " * 65537)
    else:
        definition.write_text('{"SECRET-MARKER":')
    result = cli(
        components,
        tmp_path,
        "register-source",
        "--definition-path",
        str(definition),
        "--tenant-id",
        "tenant-one",
    )
    assert result.returncode == 1
    assert json.loads(result.stderr) == {"error": "INGESTION_COMMAND_FAILED"}
    assert "SECRET-MARKER" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_worker_health_requires_migrated_schema_and_never_runs_provider(components, tmp_path):
    from src.infrastructure.ingestion.persistence.schema import metadata

    metadata.create_all(components.database.engine)
    healthy = cli(components, tmp_path, "health")
    assert healthy.returncode == 0
    assert json.loads(healthy.stdout) == {"ready": True, "databricks_enabled": False}
    missing = cli(
        components, tmp_path, "health", database_url="sqlite:///" + str(tmp_path / "unmigrated.db")
    )
    assert missing.returncode == 1
    assert json.loads(missing.stderr) == {"error": "INGESTION_COMMAND_FAILED"}
