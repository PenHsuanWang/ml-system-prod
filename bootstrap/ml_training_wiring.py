"""Select execution adapters without introducing Kubernetes into application code."""

from bootstrap.ml_config import MLSettings
from src.infrastructure.execution.local_process import LocalProcessExecutor


def training_executor(settings: MLSettings):
    if settings.training_backend == "pod":
        from src.infrastructure.deployment.kubernetes_serving import KubernetesAPI
        from src.infrastructure.execution.pod_process import PodProcessExecutor

        return PodProcessExecutor(
            settings.execution_root,
            timeout=settings.training_timeout,
            grace=settings.cancel_grace,
            dataset_root=settings.dataset_root,
            api=KubernetesAPI.in_cluster(),
            namespace=settings.training_namespace,
            pod_name=settings.training_pod_name,
            pod_uid=settings.training_pod_uid,
            container=settings.training_container,
            worker=settings.role == "supervisor",
        )
    return LocalProcessExecutor(
        settings.execution_root,
        timeout=settings.training_timeout,
        grace=settings.cancel_grace,
        dataset_root=settings.dataset_root,
    )
