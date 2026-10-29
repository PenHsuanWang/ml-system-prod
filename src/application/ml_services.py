"""Typed facades exported by the ML composition root."""

from dataclasses import dataclass
from typing import Protocol

from src.application.deployments.service import DeploymentService
from src.application.datasets.service import DatasetService
from src.application.model_registry.service import ModelRegistryService
from src.application.training.service import TrainingService
from src.domain.ml import Actor


class ActorResolver(Protocol):
    async def resolve(self, token: str) -> Actor: ...


@dataclass(frozen=True)
class MLServices:
    training: TrainingService
    registry: ModelRegistryService
    deployments: DeploymentService
    datasets: DatasetService
