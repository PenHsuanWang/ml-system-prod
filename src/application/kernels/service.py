import asyncio
import json
import logging
import re
from collections.abc import Mapping
from time import perf_counter

from src.application.identity.policy import require_permission
from src.domain.execution import (
    CapacityCoordinator,
    ExecutionBusy,
    ResourceBusy,
    ResourceLeaseLost,
    ResourceUnavailable,
)
from src.domain.identity.models import Principal
from src.domain.jobs.models import Payload
from src.domain.kernels.models import (
    InvalidKernelInput,
    KernelContext,
    KernelDeadlineExceeded,
    KernelExecutionFailed,
    KernelNotFound,
)
from src.domain.strategy import SolutionKernel

logger = logging.getLogger(__name__)


class KernelService:
    def __init__(
        self,
        kernels: Mapping[str, SolutionKernel],
        coordinator: CapacityCoordinator,
        *,
        timeout_seconds: float,
        resource_limits: Mapping[str, int],
        max_payload_bytes: int = 1_048_576,
    ) -> None:
        self._kernels = dict(kernels)
        self._coordinator = coordinator
        self._timeout = timeout_seconds
        self._max_bytes = max_payload_bytes
        self._active: set[asyncio.Task[object]] = set()
        self._closed = False
        for kernel in self._kernels.values():
            if kernel.spec.kind not in {"ai", "ml", "rule", "custom"}:
                raise ValueError("Invalid kernel kind")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", kernel.spec.version):
                raise ValueError("Kernels require an explicit, valid version")
            if kernel.spec.resource_group not in resource_limits:
                raise ValueError("A kernel references an unconfigured resource group")

    def list_kernels(self, principal: Principal) -> list[Payload]:
        require_permission(principal, "kernel.read")
        return [
            {
                "name": name,
                "version": kernel.spec.version,
                "kind": kernel.spec.kind,
                "artifact_digest": kernel.spec.artifact_digest,
            }
            for name, kernel in sorted(self._kernels.items())
        ]

    def _resolve(self, name: str, version: str) -> SolutionKernel:
        kernel = self._kernels.get(name)
        if kernel is None or kernel.spec.version != version:
            raise KernelNotFound("Kernel or requested version is not registered")
        return kernel

    def validate_request(
        self, name: str, version: str, payload: Payload, principal: Principal
    ) -> None:
        require_permission(principal, "kernel.execute")
        if self._closed:
            raise KernelExecutionFailed("Kernel execution is shutting down")
        self._resolve(name, version)
        try:
            encoded = json.dumps(payload, allow_nan=False).encode()
        except (TypeError, ValueError):
            raise InvalidKernelInput("Kernel input must be a JSON object") from None
        if len(encoded) > self._max_bytes:
            raise InvalidKernelInput("Kernel input exceeds the configured size limit")

    async def execute(
        self, name: str, version: str, payload: Payload, principal: Principal, request_id: str
    ) -> Payload:
        self.validate_request(name, version, payload, principal)
        kernel = self._resolve(name, version)
        started = perf_counter()
        outcome = "failed"
        current = asyncio.current_task()
        if current is not None:
            self._active.add(current)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._coordinator.lease(
                    kernel.spec.resource_group, wait_seconds=self._timeout
                ) as lease:
                    context = KernelContext(principal, request_id, lease.fencing_token)
                    execution = asyncio.create_task(kernel.execute(payload, context))
                    lost = asyncio.create_task(lease.lost.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {execution, lost}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if lost in done:
                            raise ResourceLeaseLost("Resource ownership was lost during execution")
                        output = await execution
                    finally:
                        execution.cancel()
                        lost.cancel()
                        cleanup = asyncio.gather(execution, lost, return_exceptions=True)
                        cancelled = False
                        while not cleanup.done():
                            try:
                                await asyncio.shield(cleanup)
                            except asyncio.CancelledError:
                                cancelled = True
                        if cancelled:
                            raise asyncio.CancelledError
                    if not isinstance(output, dict):
                        raise KernelExecutionFailed("Kernel output must be a JSON object")
                    if len(json.dumps(output, allow_nan=False).encode()) > self._max_bytes:
                        raise KernelExecutionFailed(
                            "Kernel output exceeds the configured size limit"
                        )
            outcome = "succeeded"
            return {"kernel": name, "version": version, "request_id": request_id, "output": output}
        except (
            KernelExecutionFailed,
            ExecutionBusy,
            ResourceLeaseLost,
            ResourceBusy,
            ResourceUnavailable,
        ):
            raise
        except TimeoutError:
            raise KernelDeadlineExceeded("Kernel execution exceeded its deadline") from None
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            raise KernelExecutionFailed("Kernel execution failed") from None
        finally:
            if current is not None:
                self._active.discard(current)
            # Do not log model inputs, secrets, or production data.
            logger.info(
                "Kernel name=%s version=%s tenant=%s actor=%s outcome=%s elapsed_ms=%.2f",
                name,
                version,
                principal.tenant_id,
                principal.user_id,
                outcome,
                (perf_counter() - started) * 1000,
            )

    async def aclose(self) -> None:
        self._closed = True
        pending_tasks = tuple(task for task in self._active if task is not asyncio.current_task())
        for task in pending_tasks:
            task.cancel()
        pending = asyncio.gather(*pending_tasks, return_exceptions=True)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
