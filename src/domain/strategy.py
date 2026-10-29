from typing import Protocol

from src.domain.jobs.models import Payload
from src.domain.kernels.models import KernelContext, KernelSpec


class SolutionKernel(Protocol):
    """Version-pinned solution port. Model/rule loading belongs in module lifespan.

    Implementations may delegate to a bounded thread executor, separate process,
    or remote model server. Never load a caller-supplied import or pickle file.
    """

    @property
    def spec(self) -> KernelSpec: ...

    async def execute(self, payload: Payload, context: KernelContext) -> Payload: ...
