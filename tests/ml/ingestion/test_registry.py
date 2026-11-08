"""Approved adapter selection remains immutable and independent of model workloads."""

import pytest

from src.application.ingestion.registry import ApprovedSourceAdapterRegistry
from src.domain.ingestion.errors import SourceRejected
from src.domain.ingestion.models import AdapterDescriptor, SourceDefinition


class RecordingAdapter:
    def __init__(self):
        self.requests = []

    def acquire(self, extraction, control):
        self.requests.append(extraction)
        return "test-staging-evidence"


def source(adapter_type="file", adapter_version="1"):
    return SourceDefinition("events", "v1", "tenant", adapter_type, adapter_version, "{}")


def test_registry_resolves_only_exact_approved_type_version_and_capability():
    file, other = RecordingAdapter(), RecordingAdapter()
    validations = []
    registry = ApprovedSourceAdapterRegistry(
        [
            (AdapterDescriptor("file", "1"), file, validations.append),
            (
                AdapterDescriptor("memory", "2", ("batch_extract", "incremental_extract")),
                other,
                validations.append,
            ),
        ]
    )
    assert registry.resolve("file", "1") is file
    assert registry.resolve("memory", "2", "incremental_extract") is other
    registry.validate_source(source())
    assert validations == [source()]
    for key in (
        ("file", "2", "batch_extract"),
        ("unknown", "1", "batch_extract"),
        ("file", "1", "stream_consume"),
        ("file", "1", "incremental_extract"),
    ):
        with pytest.raises(SourceRejected):
            registry.resolve(*key)


def test_registry_rejects_duplicate_implementation_key():
    with pytest.raises(SourceRejected, match="Duplicate"):
        ApprovedSourceAdapterRegistry(
            [
                (AdapterDescriptor("file", "1"), RecordingAdapter(), lambda value: None),
                (AdapterDescriptor("file", "1"), RecordingAdapter(), lambda value: None),
            ]
        )


def test_registry_does_not_observe_later_changes_to_bootstrap_registration_list():
    approved = RecordingAdapter()
    bindings = [(AdapterDescriptor("file", "1"), approved, lambda value: None)]
    registry = ApprovedSourceAdapterRegistry(bindings)
    bindings[0] = (AdapterDescriptor("file", "1"), RecordingAdapter(), lambda value: None)
    bindings.append((AdapterDescriptor("late", "1"), RecordingAdapter(), lambda value: None))
    assert registry.resolve("file", "1") is approved
    with pytest.raises(SourceRejected):
        registry.resolve("late", "1")
    assert not hasattr(registry, "register")


def test_source_configuration_validator_is_injected_not_selected_by_service():
    approved = RecordingAdapter()

    def reject(source_definition):
        raise SourceRejected("This source configuration is not approved")

    registry = ApprovedSourceAdapterRegistry([(AdapterDescriptor("file", "1"), approved, reject)])
    with pytest.raises(SourceRejected, match="configuration"):
        registry.validate_source(source())
    assert approved.requests == []


def test_additional_adapter_is_selected_only_by_new_registration():
    first, second, third = RecordingAdapter(), RecordingAdapter(), RecordingAdapter()
    registry = ApprovedSourceAdapterRegistry(
        [
            (AdapterDescriptor("file", "1"), first, lambda value: None),
            (AdapterDescriptor("test-memory", "1"), second, lambda value: None),
            (AdapterDescriptor("test-generated", "1"), third, lambda value: None),
        ]
    )
    for name, expected in (("file", first), ("test-memory", second), ("test-generated", third)):
        assert registry.resolve(name, "1") is expected
        registry.validate_source(source(name))
