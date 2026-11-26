"""Authorize scope before and after bounded external metadata operations."""

from src.application.catalog.ports import DiscoveryPolicy, MetadataProvider
from src.domain.catalog.models import MetadataPage, MetadataQuery
from src.domain.ingestion.models import identifier
from src.domain.ml import Actor, Forbidden


class CatalogService:
    def __init__(self, policy: DiscoveryPolicy, provider: MetadataProvider) -> None:
        self.policy, self.provider = policy, provider

    def connections(self, actor: Actor) -> list[str]:
        actor.require("ml:catalog:read")
        return self.policy.connections(actor)

    def browse(self, actor: Actor, connection_ref: str, query: MetadataQuery) -> MetadataPage:
        actor.require("ml:catalog:read")
        identifier(connection_ref)
        scopes = self.policy.scopes(actor, connection_ref, query)
        items = self.provider.fetch(connection_ref, query, scopes)
        if self.policy.scopes(actor, connection_ref, query) != scopes:
            raise Forbidden("Discovery access changed; repeat the request")
        # Defense in depth: the provider must apply these same scopes in its query.
        visible = []
        for item in items[: query.limit]:
            path = query.path + (item.name,) if query.kind != "columns" else query.path
            if any(
                path[: min(len(path), len(s.table_scope))]
                == s.table_scope[: min(len(path), len(s.table_scope))]
                and (query.kind != "columns" or item.name in s.columns)
                for s in scopes
            ):
                visible.append(item)
        # Raw bounded page progress must survive post-query filtering.
        more = len(items) > query.limit
        return MetadataPage(
            tuple(visible[: query.limit]),
            query.limit,
            query.offset + query.limit if more else None,
            more,
        )
