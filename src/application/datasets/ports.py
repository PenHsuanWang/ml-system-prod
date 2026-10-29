from typing import Protocol
from src.domain.datasets.models import DatasetVersion
from src.domain.ml import Actor


class DatasetCatalog(Protocol):
    def resolve(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion: ...
