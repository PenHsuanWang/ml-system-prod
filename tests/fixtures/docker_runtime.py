"""Test-only Docker entry points, with real platform features and a neutral kernel.

This fixture is mounted into the production image by integration tests. It is
never registered by the production composition root, and importing it opens no
connections.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from bootstrap.wiring import build_modules
from bootstrap.worker import build_worker_broker
from server import create_app as create_backend_app
from src.config.settings import Settings
from src.domain.jobs.models import Payload
from src.domain.kernels.models import KernelContext, KernelSpec


class EchoKernel:
    spec = KernelSpec(version="1", kind="custom", resource_group="compute")

    async def execute(self, payload: Payload, context: KernelContext) -> Payload:
        # Keep several deliveries in flight so both consumer processes can work.
        await asyncio.sleep(0.075)
        return {"value": payload["value"], "worker_pid": os.getpid()}


@asynccontextmanager
async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
    yield ModuleExports(kernels={"echo": EchoKernel()})


module = ModuleDefinition(name="probe", lifespan=lifespan)


def create_app() -> FastAPI:
    settings = Settings()
    return create_backend_app(settings, modules=(*build_modules(settings), module))


_worker_settings = Settings()
broker = build_worker_broker(_worker_settings, modules=(*build_modules(_worker_settings), module))
