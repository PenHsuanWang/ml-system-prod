"""Keep local development available; Kubernetes adapters remain at the composition root."""

from bootstrap.ml_config import MLSettings
from src.infrastructure.deployment.endpoints import (
    KubernetesEndpointResolver,
    LocalEndpointResolver,
)
from src.infrastructure.deployment.local_serving import LocalServingExecutor


def endpoint_resolver(settings: MLSettings):
    if settings.serving_backend == "kubernetes":
        return KubernetesEndpointResolver(settings.serving_namespace)
    return LocalEndpointResolver(settings.execution_root / "serving")


def serving_executor(settings: MLSettings):
    if settings.serving_backend == "kubernetes":
        from src.infrastructure.deployment.kubernetes_serving import (
            KubernetesAPI,
            KubernetesServingExecutor,
        )

        return KubernetesServingExecutor(
            KubernetesAPI.in_cluster(),
            settings.serving_namespace,
            settings.serving_image_repository,
        )
    return LocalServingExecutor(settings.execution_root / "serving")
