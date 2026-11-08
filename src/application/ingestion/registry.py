"""Immutable approved adapter registry, independent of models and global state."""

from collections.abc import Iterable
from types import MappingProxyType

from src.application.ingestion.ports import AdapterRegistration, BatchSourceAdapter
from src.domain.ingestion.errors import SourceRejected
from src.domain.ingestion.models import SourceDefinition


class ApprovedSourceAdapterRegistry:
    def __init__(self, registrations: Iterable[AdapterRegistration]) -> None:
        adapters = {}
        for descriptor, adapter, validator in registrations:
            key = (descriptor.adapter_type, descriptor.version)
            if key in adapters:
                raise SourceRejected("Duplicate approved source adapter registration")
            adapters[key] = (descriptor, adapter, validator)
        self._adapters = MappingProxyType(adapters)

    def resolve(
        self, adapter_type: str, version: str, capability: str = "batch_extract"
    ) -> BatchSourceAdapter:
        item = self._adapters.get((adapter_type, version))
        if item is None or capability not in item[0].capabilities:
            raise SourceRejected("Unknown source adapter or unsupported capability")
        return item[1]

    def validate_source(self, source: SourceDefinition) -> None:
        self.resolve(source.adapter_type, source.adapter_version)
        self._adapters[(source.adapter_type, source.adapter_version)][2](source)
