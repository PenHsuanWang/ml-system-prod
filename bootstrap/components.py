"""Explicit cross-layer feature initialization without discovery or business defaults."""

import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from fastapi import APIRouter
from httpx import AsyncClient

from bootstrap.plugin_loader import PluginBinding, load_approved_plugins
from src.application.jobs.service import JobService
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.exports.ports import ExportSource
from src.domain.jobs.ports import TaskHandler
from src.domain.strategy import SolutionKernel

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_NAMES = frozenset({"jobs", "identity", "kernels", "exports"})
RuntimeRole = Literal["api", "worker"]


@dataclass(frozen=True, slots=True)
class ModuleContext:
    """Process-scoped resources and only the explicitly required module facades."""

    settings: Settings
    http_client: AsyncClient
    role: RuntimeRole
    dependencies: Mapping[str, object | None]
    resources: Mapping[str, object] = field(default_factory=dict)
    jobs: JobService | None = None


@dataclass(frozen=True, slots=True)
class ModuleExports:
    """A module-defined public facade and local task names, without global state."""

    services: object | None = None
    tasks: Mapping[str, TaskHandler] = field(default_factory=dict)
    kernels: Mapping[str, SolutionKernel] = field(default_factory=dict)
    datasets: Mapping[str, ExportSource] = field(default_factory=dict)


ModuleLifespan = Callable[[ModuleContext], AbstractAsyncContextManager[ModuleExports]]


@asynccontextmanager
async def empty_lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
    yield ModuleExports()


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    """Cross-layer feature initializer explicitly selected by bootstrap/wiring.py.

    Building a manifest must not perform I/O. All resource creation belongs in
    lifespan, which runs independently in every API and task-worker process.
    """

    name: str
    requires: tuple[str, ...] = ()
    routers: tuple[APIRouter, ...] = ()
    lifespan: ModuleLifespan = empty_lifespan
    kind: Literal["application", "platform"] = "application"
    allow_anonymous: bool = False


@dataclass(frozen=True, slots=True)
class ModuleBindings:
    registry: TaskRegistry
    services: Mapping[str, object | None]
    kernels: Mapping[str, SolutionKernel] = field(default_factory=dict)
    datasets: Mapping[str, ExportSource] = field(default_factory=dict)


class ModuleCatalog:
    """Validate dependency order, then start modules under the owner's exit stack."""

    def __init__(self, modules: Sequence[ModuleDefinition] = ()) -> None:
        definitions: dict[str, ModuleDefinition] = {}
        for definition in modules:
            if not _NAME.fullmatch(definition.name) or (
                definition.name in _RESERVED_NAMES and definition.kind != "platform"
            ):
                raise ValueError(f"Invalid or reserved module name: {definition.name}")
            if definition.name in definitions:
                raise ValueError(f"Duplicate module: {definition.name}")
            if len(set(definition.requires)) != len(definition.requires):
                raise ValueError(f"Duplicate dependencies for module: {definition.name}")
            definitions[definition.name] = definition

        ordered: list[ModuleDefinition] = []
        active: set[str] = set()
        complete: set[str] = set()

        def visit(name: str) -> None:
            if name in complete:
                return
            if name in active:
                raise ValueError(f"Cyclic module dependency involving: {name}")
            if name not in definitions:
                raise ValueError(f"Required module is not registered: {name}")
            active.add(name)
            definition = definitions[name]
            for dependency in definition.requires:
                visit(dependency)
            active.remove(name)
            complete.add(name)
            ordered.append(definition)

        for name in definitions:
            visit(name)
        self.definitions = tuple(ordered)

    async def start(
        self,
        stack: AsyncExitStack,
        *,
        settings: Settings,
        http_client: AsyncClient,
        role: RuntimeRole,
        resources: Mapping[str, object] | None = None,
        registry: TaskRegistry | None = None,
        jobs: JobService | None = None,
    ) -> ModuleBindings:
        registry = registry if registry is not None else TaskRegistry()
        services: dict[str, object | None] = {}
        kernels: dict[str, SolutionKernel] = {}
        datasets: dict[str, ExportSource] = {}
        for definition in self.definitions:
            context = ModuleContext(
                settings=settings,
                http_client=http_client,
                role=role,
                dependencies=MappingProxyType(
                    {name: services[name] for name in definition.requires}
                ),
                resources=resources if resources is not None else MappingProxyType({}),
                jobs=jobs if settings.task_backend != "disabled" else None,
            )
            exports = await stack.enter_async_context(definition.lifespan(context))
            if not isinstance(exports, ModuleExports):
                raise TypeError(f"Module {definition.name} must yield ModuleExports")
            if exports.tasks and settings.task_backend == "disabled":
                raise ValueError(
                    f"Module {definition.name} exports tasks but the task backend is disabled"
                )
            for local_name, handler in exports.tasks.items():
                if not _NAME.fullmatch(local_name):
                    raise ValueError(f"Invalid task name in module {definition.name}: {local_name}")
                if not callable(handler):
                    raise TypeError(f"Task handler is not callable: {definition.name}.{local_name}")
                registry.register(f"{definition.name}.{local_name}", handler)
            for local_name, kernel in exports.kernels.items():
                if not _NAME.fullmatch(local_name):
                    raise ValueError(
                        f"Invalid kernel name in module {definition.name}: {local_name}"
                    )
                kernels[f"{definition.name}.{local_name}"] = kernel
            for local_name, source in exports.datasets.items():
                if not _NAME.fullmatch(local_name):
                    raise ValueError(
                        f"Invalid dataset name in module {definition.name}: {local_name}"
                    )
                datasets[f"{definition.name}.{local_name}"] = source
            services[definition.name] = exports.services
        return ModuleBindings(
            registry,
            MappingProxyType(services),
            load_approved_plugins(PluginBinding(name, kernel) for name, kernel in kernels.items()),
            MappingProxyType(datasets),
        )
