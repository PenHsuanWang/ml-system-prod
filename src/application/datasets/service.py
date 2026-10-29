from src.application.datasets.ports import DatasetCatalog
from src.domain.datasets.models import DatasetVersion
from src.domain.ml import Actor, Forbidden


class DatasetService:
    def __init__(self, catalog: DatasetCatalog) -> None:
        self.catalog = catalog

    def get(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion:
        if not actor.permissions.intersection({"ml:train", "ml:dataset:read"}):
            raise Forbidden("Dataset read permission is required")
        return self.catalog.resolve(actor, dataset_id, version)
