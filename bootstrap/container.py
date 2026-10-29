from collections.abc import Mapping
from dataclasses import dataclass, field

from httpx import AsyncClient

from bootstrap.components import ModuleBindings
from src.application.exports.service import ExportService
from src.application.identity.facade import IdentityServices
from src.application.jobs.service import JobService
from src.application.kernels.service import KernelService
from src.application.orchestration import KernelJobHandler
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.ports import JobDispatcher
from src.infrastructure.locking.resources import ResourceCoordinator


@dataclass(frozen=True, slots=True)
class Runtime:
    """Process-scoped dependencies owned by API or worker bootstrap lifespan."""

    settings: Settings
    registry: TaskRegistry
    dispatcher: JobDispatcher
    jobs: JobService
    http_client: AsyncClient
    modules: Mapping[str, object | None] = field(default_factory=dict)
    resources: Mapping[str, object] = field(default_factory=dict)
    kernels: KernelService | None = None
    exports: ExportService | None = None


def build_platform_services(
    settings: Settings, bindings: ModuleBindings, resources: Mapping[str, object]
) -> tuple[KernelService, ExportService]:
    coordinator = resources["leases"]
    if not isinstance(coordinator, ResourceCoordinator):
        raise TypeError("A resource coordinator is required")
    kernels = KernelService(
        bindings.kernels,
        coordinator,
        timeout_seconds=settings.kernel_timeout_seconds,
        resource_limits=settings.resource_limits,
    )
    exports = ExportService(bindings.datasets)
    identity = bindings.services.get("identity")
    if isinstance(identity, IdentityServices) and settings.task_backend != "disabled":
        bindings.registry.register(
            "platform.kernel.execute",
            KernelJobHandler(kernels, identity.resolve_principal),
        )
    return kernels, exports
