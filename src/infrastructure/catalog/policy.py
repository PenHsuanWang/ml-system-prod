"""Deployment-owned discovery grants, independent of source execution grants."""

from collections.abc import Callable, Mapping
from typing import Any

from src.domain.catalog.models import DiscoveryScope, MetadataQuery, metadata_name
from src.domain.ingestion.models import identifier
from src.domain.ml import Actor, Forbidden, MLError
from src.infrastructure.ingestion.adapters.databricks_contract import ConnectionConfig, _object


class DatabricksDiscoveryPolicy:
    def __init__(
        self, connections: Mapping[str, ConnectionConfig], load: Callable[[], dict[str, Any]]
    ) -> None:
        self._connections, self.load = connections, load

    def _grants(self, actor: Actor) -> list[dict[str, Any]]:
        try:
            envelope = _object(self.load(), {"contract", "grants"})
            grants = envelope["grants"]
            if (
                envelope["contract"] != "databricks-discovery-v1"
                or not isinstance(grants, list)
                or len(grants) > 256
            ):
                raise ValueError
            result = []
            for value in grants:
                g = _object(
                    value, {"tenant_id", "actor_ids", "connection_ref", "table_scope", "columns"}
                )
                identifier(g["tenant_id"])
                identifier(g["connection_ref"])
                for key, maximum in (("actor_ids", 256), ("columns", 128), ("table_scope", 3)):
                    entries = g[key]
                    if (
                        not isinstance(entries, list)
                        or not 1 <= len(entries) <= maximum
                        or any(not isinstance(e, str) for e in entries)
                        or len(set(entries)) != len(entries)
                    ):
                        raise ValueError
                    for entry in entries:
                        (identifier if key == "actor_ids" else metadata_name)(entry)
                if (
                    g["tenant_id"] == actor.tenant_id
                    and actor.actor_id in g["actor_ids"]
                    and g["connection_ref"] in self._connections
                ):
                    result.append(g)
            return result
        except (ValueError, TypeError, KeyError, MLError):
            raise Forbidden("Discovery policy is unavailable") from None

    def connections(self, actor: Actor) -> list[str]:
        return sorted({g["connection_ref"] for g in self._grants(actor)})

    def scopes(
        self, actor: Actor, connection_ref: str, query: MetadataQuery
    ) -> tuple[DiscoveryScope, ...]:
        result = set()
        for g in self._grants(actor):
            scope = tuple(g["table_scope"])
            n = min(len(scope), len(query.path))
            if g["connection_ref"] == connection_ref and scope[:n] == query.path[:n]:
                result.add(DiscoveryScope(scope, tuple(sorted(g["columns"]))))
        if not result:
            raise Forbidden("Discovery access is not authorized")
        return tuple(sorted(result, key=lambda s: (s.table_scope, s.columns)))
