"""Composition root: concrete classes are selected only at application boundaries."""

from bootstrap.components import ModuleDefinition
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.ports import JobDispatcher


def build_modules(settings: Settings | None = None) -> tuple[ModuleDefinition, ...]:
    """Explicit feature initializers; each wires collaborators across the src layers."""
    config = settings or Settings()
    modules: list[ModuleDefinition] = []
    if config.identity_enabled:
        from bootstrap.identity import build_identity_module

        modules.append(build_identity_module(config))
    # Add approved cross-layer application initializers here.
    return tuple(modules)


def build_dispatcher(settings: Settings, registry: TaskRegistry) -> JobDispatcher:
    if settings.task_backend == "disabled":
        from src.infrastructure.messaging.disabled import DisabledJobDispatcher

        return DisabledJobDispatcher()
    if settings.task_backend == "distributed":
        from src.infrastructure.messaging.distributed import DistributedJobDispatcher

        return DistributedJobDispatcher(registry, settings)

    from src.infrastructure.messaging.memory import MemoryJobDispatcher

    return MemoryJobDispatcher(
        registry,
        concurrency=settings.task_concurrency,
        queue_size=settings.task_queue_size,
        retention_seconds=settings.job_retention_seconds,
        max_records=settings.job_max_records,
        job_timeout_seconds=settings.job_timeout_seconds,
        shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
    )
