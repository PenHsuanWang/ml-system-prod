from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from bootstrap.components import ModuleCatalog, ModuleContext, ModuleDefinition, ModuleExports
from bootstrap.plugin_loader import (
    SOLUTION_KERNEL_CONTRACT,
    PluginBinding,
    load_approved_plugins,
)
from src.config.settings import Settings
from src.domain.identity.models import Principal
from src.domain.jobs.models import Payload
from src.domain.kernels.models import KernelContext, KernelSpec


class ApprovedKernel:
    def __init__(self, version: str = "1.0.0") -> None:
        self.spec = KernelSpec(version, "custom")

    async def execute(self, payload: Payload, context: KernelContext) -> Payload:
        return payload


def test_no_explicit_bindings_registers_no_plugins() -> None:
    assert load_approved_plugins(()) == {}


def test_explicit_bindings_preserve_objects_and_expose_read_only_registry() -> None:
    selected = ApprovedKernel("2026.10-rc_2")
    another = ApprovedKernel("v1")
    registered = load_approved_plugins(
        (
            PluginBinding("alpha.predict", selected),
            PluginBinding("beta.evaluate", another, SOLUTION_KERNEL_CONTRACT),
        )
    )
    assert dict(registered) == {"alpha.predict": selected, "beta.evaluate": another}
    assert registered["alpha.predict"].spec.version == "2026.10-rc_2"
    with pytest.raises(TypeError):
        registered["alpha.predict"] = another
    with pytest.raises(TypeError):
        del registered["beta.evaluate"]


def test_duplicate_identifiers_fail_instead_of_replacing_an_approved_implementation() -> None:
    with pytest.raises(ValueError, match="Duplicate plugin: alpha.predict"):
        load_approved_plugins(
            (
                PluginBinding("alpha.predict", ApprovedKernel("1")),
                PluginBinding("alpha.predict", ApprovedKernel("2")),
            )
        )


def test_incompatible_contract_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="Incompatible plugin contract: alpha.predict"):
        load_approved_plugins(
            (PluginBinding("alpha.predict", ApprovedKernel(), "solution-kernel/v2"),)
        )


@pytest.mark.parametrize(
    "name", ["predict", "alpha.predict.extra", "Alpha.predict", "alpha.bad-name"]
)
def test_plugin_identifiers_require_one_module_and_one_local_name(name: str) -> None:
    with pytest.raises(ValueError, match="Plugin names must use module_name.local_name"):
        load_approved_plugins((PluginBinding(name, ApprovedKernel()),))


@pytest.mark.parametrize("version", ["", "has whitespace", "x" * 65])
def test_missing_or_invalid_implementation_version_is_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="explicit valid version: alpha.predict"):
        load_approved_plugins((PluginBinding("alpha.predict", ApprovedKernel(version)),))


def test_kernel_spec_must_be_the_domain_contract() -> None:
    invalid = SimpleNamespace(
        spec={"version": "1", "kind": "custom"}, execute=ApprovedKernel().execute
    )
    with pytest.raises(TypeError, match="Plugin must expose KernelSpec: alpha.predict"):
        load_approved_plugins((PluginBinding("alpha.predict", invalid),))


def test_unrecognized_kernel_kind_is_rejected() -> None:
    invalid = ApprovedKernel()
    invalid.spec = KernelSpec("1", "unknown")
    with pytest.raises(ValueError, match="Invalid plugin kind: alpha.predict"):
        load_approved_plugins((PluginBinding("alpha.predict", invalid),))


@pytest.mark.parametrize("execute", [None, "execute"])
def test_non_callable_execute_is_rejected(execute: object) -> None:
    invalid = SimpleNamespace(spec=KernelSpec("1", "custom"), execute=execute)
    with pytest.raises(TypeError, match="Plugin execute must be .*: alpha.predict"):
        load_approved_plugins((PluginBinding("alpha.predict", invalid),))


def test_synchronous_execute_fails_at_startup_without_running_the_implementation() -> None:
    called = False

    def execute(payload: Payload, context: KernelContext) -> Payload:
        nonlocal called
        called = True
        return payload

    invalid = SimpleNamespace(spec=KernelSpec("1", "custom"), execute=execute)
    with pytest.raises(TypeError, match="async"):
        load_approved_plugins((PluginBinding("alpha.predict", invalid),))
    assert called is False


async def test_async_callable_execution_object_is_a_supported_implementation() -> None:
    class AsyncExecution:
        async def __call__(self, payload: Payload, context: KernelContext) -> Payload:
            return payload

    selected = SimpleNamespace(spec=KernelSpec("1", "custom"), execute=AsyncExecution())
    registered = load_approved_plugins((PluginBinding("alpha.predict", selected),))
    context = KernelContext(Principal("user", "tenant", "name", frozenset()), "request", 1)
    assert await registered["alpha.predict"].execute({"value": 42}, context) == {"value": 42}


@pytest.mark.parametrize("role", ["api", "worker"])
async def test_invalid_plugin_aborts_catalog_startup_and_unwinds_all_opened_modules(
    role: str,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def base_lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        events.append("open:base")
        try:
            yield ModuleExports(services=object())
        finally:
            events.append("close:base")

    @asynccontextmanager
    async def solution_lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        assert "base" in context.dependencies
        events.append("open:solution")
        try:
            yield ModuleExports(kernels={"predict": ApprovedKernel("")})
        finally:
            events.append("close:solution")

    catalog = ModuleCatalog(
        (
            ModuleDefinition("solution", requires=("base",), lifespan=solution_lifespan),
            ModuleDefinition("base", lifespan=base_lifespan),
        )
    )
    settings = Settings(_env_file=None, environment="test", task_backend="memory")
    async with AsyncClient() as client:
        with pytest.raises(ValueError, match="explicit valid version: solution.predict"):
            async with AsyncExitStack() as stack:
                await catalog.start(stack, settings=settings, http_client=client, role=role)
    assert events == ["open:base", "open:solution", "close:solution", "close:base"]
