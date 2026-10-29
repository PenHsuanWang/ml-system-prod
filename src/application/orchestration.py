"""Transport-independent background orchestration over shared application use cases."""

from collections.abc import Awaitable, Callable

from src.application.kernels.service import KernelService
from src.domain.identity.models import Principal
from src.domain.jobs.models import Payload


class KernelJobHandler:
    def __init__(
        self,
        service: KernelService,
        resolve_principal: Callable[[str, str], Awaitable[Principal]],
    ) -> None:
        self._service = service
        self._resolve_principal = resolve_principal

    async def __call__(self, payload: Payload) -> Payload:
        # Re-authorize on the worker using current persisted permissions, and
        # never place a reusable JWT or password into a broker message.
        principal = await self._resolve_principal(payload["tenant_id"], payload["user_id"])
        return await self._service.execute(
            payload["kernel"],
            payload["version"],
            payload["input"],
            principal,
            payload["request_id"],
        )
