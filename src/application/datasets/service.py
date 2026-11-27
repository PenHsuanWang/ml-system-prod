from typing import Any

from src.application.datasets.ports import DatasetCatalog, DatasetDiscoveryRepository
from src.domain.datasets.models import DatasetVersion
from src.domain.ml import Actor, Forbidden, Unavailable
from src.domain.pagination import CollectionWindow


class DatasetService:
    def __init__(
        self, catalog: DatasetCatalog, discovery: DatasetDiscoveryRepository | None = None
    ) -> None:
        self.catalog, self.discovery = catalog, discovery

    def get(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion:
        if not actor.permissions.intersection({"ml:train", "ml:dataset:read"}):
            raise Forbidden("Dataset read permission is required")
        return self.catalog.resolve(actor, dataset_id, version)

    def _reader(self, actor: Actor) -> DatasetDiscoveryRepository:
        if not actor.permissions.intersection({"ml:train", "ml:dataset:read"}):
            raise Forbidden("Dataset read permission is required")
        if self.discovery is None:
            raise Unavailable("Dataset discovery is not configured")
        return self.discovery

    @staticmethod
    def _page(rows: list[dict[str, Any]], window: CollectionWindow) -> dict[str, Any]:
        return {
            "items": rows[: window.limit],
            "limit": window.limit,
            "offset": window.offset,
            "next_offset": window.next_offset(has_more=len(rows) > window.limit),
        }

    def families(
        self, actor: Actor, window: CollectionWindow, dataset_id: str | None = None
    ) -> dict[str, Any]:
        return self._page(self._reader(actor).list_families(actor, window, dataset_id), window)

    def versions(self, actor: Actor, dataset_id: str, window: CollectionWindow) -> dict[str, Any]:
        return self._page(self._reader(actor).list_versions(actor, dataset_id, window), window)

    def metadata(self, actor: Actor, dataset_id: str, version: str) -> dict[str, Any]:
        return self._reader(actor).version_metadata(actor, dataset_id, version)

    def lineage(self, actor: Actor, dataset_id: str, version: str) -> dict[str, Any]:
        return self._reader(actor).lineage(actor, dataset_id, version)
