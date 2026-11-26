"""Narrow, separately authorized external metadata boundaries."""

from typing import Protocol

from src.domain.catalog.models import DiscoveryScope, MetadataItem, MetadataQuery
from src.domain.ml import Actor


class DiscoveryPolicy(Protocol):
    def connections(self, actor: Actor) -> list[str]: ...
    def scopes(
        self, actor: Actor, connection_ref: str, query: MetadataQuery
    ) -> tuple[DiscoveryScope, ...]: ...


class MetadataProvider(Protocol):
    def fetch(
        self, connection_ref: str, query: MetadataQuery, scopes: tuple[DiscoveryScope, ...]
    ) -> tuple[MetadataItem, ...]: ...
