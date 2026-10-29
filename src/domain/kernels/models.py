from dataclasses import dataclass
from typing import Literal

from src.domain.identity.models import Principal


@dataclass(frozen=True, slots=True)
class KernelSpec:
    version: str
    kind: Literal["ai", "ml", "rule", "custom"]
    resource_group: str = "compute"
    artifact_digest: str | None = None


@dataclass(frozen=True, slots=True)
class KernelContext:
    principal: Principal
    request_id: str
    fencing_token: int


class KernelNotFound(Exception):
    pass


class KernelExecutionFailed(Exception):
    pass


class InvalidKernelInput(Exception):
    pass


class KernelDeadlineExceeded(KernelExecutionFailed):
    pass
