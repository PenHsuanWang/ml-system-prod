"""The only place assembling SQL, approved plugins and runtime adapters."""

from dataclasses import dataclass
from functools import partial

from bootstrap.ml_config import MLSettings
from src.application.deployments.service import DeploymentService
from src.application.datasets.service import DatasetService
from src.application.ml_services import MLServices
from src.application.model_registry.service import ModelRegistryService
from src.application.training.service import TrainingService
from src.application.training.supervisor import TrainingSupervisor
from src.infrastructure.datasets.ml_recipes import ApprovedRecipeResolver
from src.infrastructure.datasets.snapshots import FileDatasetSource
from src.infrastructure.execution.local_process import LocalProcessExecutor
from src.infrastructure.mlflow.tracking import LocalExperimentTracker, MLflowExperimentTracker
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.persistence.ml_deployments import SQLDeploymentRepository
from src.infrastructure.persistence.ml_registry import SQLRegistryRepository
from src.infrastructure.persistence.ml_training import SQLTrainingRepository
from src.infrastructure.serving.evaluation import BundleEvaluator
from src.infrastructure.serving.materializer import BundleMaterializer
from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
from src.plugins.models.mlp import build_mlp


@dataclass(frozen=True)
class Components:
    settings: MLSettings
    database: Database
    training: SQLTrainingRepository
    registry: SQLRegistryRepository
    deployments: SQLDeploymentRepository
    services: MLServices

    def supervisor(self) -> TrainingSupervisor:
        tracker = (LocalExperimentTracker(self.settings.artifact_root)
                   if self.settings.tracker_backend == "local" else
                   MLflowExperimentTracker(self.settings.tracking_uri))
        materializer = BundleMaterializer(self.settings.artifact_root, self.settings.tracking_uri)
        evaluator = BundleEvaluator(materializer, partial(PyTorchRuntime, model_factory=build_mlp,
                                                          expected_code_digest=self.settings.code_revision), FileDatasetSource(self.settings.dataset_root))
        executor = LocalProcessExecutor(self.settings.execution_root, timeout=self.settings.training_timeout,
                                        grace=self.settings.cancel_grace, dataset_root=self.settings.dataset_root)
        supervisor = TrainingSupervisor(self.training, executor, self.registry, tracker, evaluator,
                                        timeout=self.settings.training_timeout, heartbeat=self.settings.heartbeat_seconds,
                                        runtime_code=self.settings.code_revision, runtime_image=self.settings.image_digest)
        return supervisor


def build_components(settings: MLSettings) -> Components:
    settings.validate()
    database = Database(settings.database_url)
    training = SQLTrainingRepository(database, retention=settings.retention_seconds,
                                     max_pending=settings.max_pending, queue_timeout=settings.queue_timeout,
                                     training_timeout=settings.training_timeout)
    registry = SQLRegistryRepository(database)
    deployments = SQLDeploymentRepository(database, serving_image=settings.image_digest)
    catalog = SQLDatasetCatalog(database, settings.dataset_root)
    recipes = ApprovedRecipeResolver(
        code_revision=settings.code_revision, image_digest=settings.image_digest, catalog=catalog)
    services = MLServices(TrainingService(training, recipes), ModelRegistryService(registry),
                          DeploymentService(deployments, settings.environment), DatasetService(catalog))
    return Components(settings, database, training, registry, deployments, services)
