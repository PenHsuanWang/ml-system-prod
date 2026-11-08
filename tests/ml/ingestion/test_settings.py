"""Bootstrap and SQL reject the same invalid resource configuration early."""

from pathlib import Path

import pytest

from bootstrap.ingestion_wiring import IngestionSettings
from src.infrastructure.ingestion.persistence.ingestion_jobs import SQLIngestionJobRepository


@pytest.mark.parametrize(
    "field,invalid",
    [
        ("lease_seconds", 0),
        ("lease_seconds", -1),
        ("lease_seconds", 3600.1),
        ("retry_delay", -0.1),
        ("retry_delay", 300.1),
        ("queue_timeout", 0),
        ("queue_timeout", 86400.1),
        ("max_attempts", 0),
        ("max_attempts", 101),
        ("reconcile_limit", 0),
        ("reconcile_limit", 1001),
        ("max_pending", 0),
        ("max_pending", 100001),
        *[
            (field, value)
            for field in ("lease_seconds", "retry_delay", "queue_timeout")
            for value in (True, False, float("nan"), float("inf"), -float("inf"), "1", None)
        ],
        *[
            (field, value)
            for field in ("max_attempts", "reconcile_limit", "max_pending")
            for value in (True, False, 1.5, float("nan"), "1", None)
        ],
    ],
)
def test_settings_and_repository_share_bounds_and_reject_before_storage(field, invalid, tmp_path):
    raw_root = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        IngestionSettings(raw_root=raw_root, **{field: invalid})
    # Validation precedes access to the supplied database/resource object.
    with pytest.raises(ValueError):
        SQLIngestionJobRepository(None, **{field: invalid})
    assert not raw_root.exists()


def test_settings_and_repository_accept_identical_documented_boundary_values():
    values = {
        "lease_seconds": 3600,
        "retry_delay": 300,
        "queue_timeout": 86400,
        "max_attempts": 100,
        "reconcile_limit": 1000,
        "max_pending": 100000,
    }
    settings = IngestionSettings(**values)
    repository = SQLIngestionJobRepository(None, **values)
    for name, value in values.items():
        assert getattr(settings, name) == getattr(repository, name) == value
    assert IngestionSettings(lease_seconds=0.15).lease_seconds == 0.15


@pytest.mark.parametrize("value", [0, -1, 300.1, True, float("nan"), float("inf"), "1", None])
def test_poll_interval_is_explicitly_bounded(value):
    with pytest.raises(ValueError):
        IngestionSettings(poll_seconds=value)


@pytest.mark.parametrize(
    "roots",
    [
        [("approved", Path("/safe"))],
        (("approved", Path("/safe")), ("approved", Path("/other"))),
        (("../root", Path("/safe")),),
        (("approved", "/untyped"),),
        (("approved", Path("../escape")),),
        (("approved",),),
        ((True, Path("/safe")),),
    ],
)
def test_approved_roots_are_validated_before_adapter_construction(roots):
    with pytest.raises(ValueError):
        IngestionSettings(file_roots=roots)


def test_invalid_environment_bounds_are_rejected_at_configuration_load(monkeypatch):
    monkeypatch.setenv("ML_INGESTION_MAX_ATTEMPTS", "101")
    with pytest.raises(ValueError):
        IngestionSettings.from_env()


def test_duplicate_environment_roots_cannot_silently_replace_an_approved_path(monkeypatch):
    monkeypatch.setenv(
        "ML_INGESTION_FILE_ROOTS_JSON", '{"approved":"/safe","approved":"/replacement"}'
    )
    with pytest.raises(ValueError, match="Duplicate approved"):
        IngestionSettings.from_env()
