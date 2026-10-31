"""The only place assembling SQL, approved plugins and runtime adapters."""

from dataclasses import dataclass
from functools import partial
import json

from bootstrap.ml_config import MLSettings
from src.application.deployments.service import DeploymentService
from src.application.datasets.service import DatasetService
from src.application.ml_services import MLServices
from src.application.model_registry.service import ModelRegistryService
from src.application.training.service import TrainingService
from src.application.training.supervisor import TrainingSupervisor
from src.infrastructure.datasets.ml_recipes import ApprovedRecipeResolver
from src.infrastructure.datasets.snapshots import FileDatasetSource
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.execution.local_process import LocalProcessExecutor
from src.infrastructure.mlflow.tracking import LocalExperimentTracker, MLflowExperimentTracker
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
from src.infrastructure.persistence.ml_deployments import SQLDeploymentRepository
from src.infrastructure.persistence.ml_registry import SQLRegistryRepository
from src.infrastructure.persistence.ml_training import SQLTrainingRepository
from src.infrastructure.serving.evaluation import BundleEvaluator, EBMBundleEvaluator, WorkloadBundleEvaluator
from src.infrastructure.serving.materializer import BundleMaterializer
from src.infrastructure.serving.pytorch_runtime import PyTorchRuntime
from src.plugins.models.mlp import build_mlp
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn
from src.plugins.evaluation.ebm_v1 import EBMQualityPolicy, evaluate_neural_outputs


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
        classifier = BundleEvaluator(materializer, partial(PyTorchRuntime, model_factory=build_mlp,
                                                          expected_code_digest=self.settings.code_revision), FileDatasetSource(self.settings.dataset_root))
        ebm = EBMBundleEvaluator(materializer, partial(EBMRuntime, model_factory=build_ebm_cnn,
            expected_code_digest=self.settings.code_revision), EBMFileDatasetSource(self.settings.dataset_root),
            evaluate_neural_outputs, lambda config, metrics: EBMQualityPolicy.from_dict(config).decide(metrics))
        evaluator = WorkloadBundleEvaluator(classifier, ebm)
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
    policy = None
    if settings.ebm_quality_policy_json:
        if len(settings.ebm_quality_policy_json.encode()) > 8192:
            raise ValueError("EBM quality policy configuration is too large")
        policy = EBMQualityPolicy.from_dict(json.loads(settings.ebm_quality_policy_json)).to_dict()
        if policy["policy_version"] == "ebm-unqualified-v1":
            raise ValueError("EBM quality policy uses a reserved version")
    recipes = ApprovedRecipeResolver(
        code_revision=settings.code_revision, image_digest=settings.image_digest, catalog=catalog,
        ebm_evaluation_policy=policy)
    services = MLServices(TrainingService(training, recipes), ModelRegistryService(registry),
                          DeploymentService(deployments, settings.environment), DatasetService(catalog))
    return Components(settings, database, training, registry, deployments, services)
