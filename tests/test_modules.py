from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from taskiq import TaskiqEvents, TaskiqState

import server
from bootstrap import worker as taskiq_worker
from bootstrap.components import ModuleCatalog, ModuleContext, ModuleDefinition, ModuleExports
from src.application.jobs.service import JobService
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.exports.models import ExportColumn
from src.domain.jobs.errors import UnknownTask
from src.domain.kernels.models import KernelSpec


def settings():
    return Settings(_env_file=None, environment="test", task_backend="memory")


async def pure_echo(payload):
    return payload


class PureKernel:
    spec = KernelSpec(version="test", kind="custom")

    async def execute(self, payload, context):
        return payload


class PureDataset:
    columns = (ExportColumn("value"),)

    async def rows(self, principal, *, limit):
        if limit:
            yield {"value": None}


async def test_empty_catalog_exports_no_services_tasks_kernels_or_datasets():
    catalog = ModuleCatalog()
    assert catalog.definitions == ()
    async with AsyncExitStack() as stack, AsyncClient() as client:
        bindings = await catalog.start(stack, settings=settings(), http_client=client, role="api")
        assert bindings.services == bindings.kernels == bindings.datasets == {}
        with pytest.raises(UnknownTask):
            bindings.registry.resolve("alpha.echo")


@pytest.mark.parametrize(
    "name",
    ["", "Alpha", "bad-name", "bad.name", "0alpha", "jobs", "identity", "kernels", "exports"],
)
def test_invalid_and_reserved_module_names_are_rejected(name):
    with pytest.raises(ValueError, match="Invalid or reserved module name"):
        ModuleCatalog((ModuleDefinition(name),))


@pytest.mark.parametrize("name", ["jobs", "identity", "kernels", "exports"])
def test_reserved_names_require_an_explicit_platform_manifest(name):
    definition = ModuleDefinition(name, kind="platform")
    assert ModuleCatalog((definition,)).definitions == (definition,)


def test_duplicate_names_and_duplicate_dependency_declarations_are_rejected():
    with pytest.raises(ValueError, match="Duplicate module"):
        ModuleCatalog((ModuleDefinition("alpha"), ModuleDefinition("alpha")))
    with pytest.raises(ValueError, match="Duplicate dependencies"):
        ModuleCatalog(
            (ModuleDefinition("alpha"), ModuleDefinition("beta", requires=("alpha", "alpha")))
        )


def test_missing_dependency_is_rejected_before_startup():
    with pytest.raises(ValueError, match="Required module is not registered: beta"):
        ModuleCatalog((ModuleDefinition("alpha", requires=("beta",)),))


@pytest.mark.parametrize(
    "definitions",
    [
        (ModuleDefinition("alpha", requires=("alpha",)),),
        (
            ModuleDefinition("alpha", requires=("beta",)),
            ModuleDefinition("beta", requires=("gamma",)),
            ModuleDefinition("gamma", requires=("alpha",)),
        ),
    ],
)
def test_dependency_cycles_are_rejected(definitions):
    with pytest.raises(ValueError, match="Cyclic module dependency"):
        ModuleCatalog(definitions)


@pytest.mark.parametrize("role", ["api", "worker"])
async def test_topological_start_order_dependency_visibility_and_reverse_cleanup(role):
    events, contexts = [], {}
    alpha, beta, gamma = object(), object(), object()
    resources = MappingProxyType({"technical_resource": object()})

    def lifespan_for(name, facade):
        @asynccontextmanager
        async def lifespan(context):
            contexts[name] = context
            events.append(f"{name}.open")
            assert not context.http_client.is_closed
            try:
                yield ModuleExports(services=facade)
            finally:
                assert not context.http_client.is_closed
                events.append(f"{name}.close")

        return lifespan

    catalog = ModuleCatalog(
        (
            ModuleDefinition("beta", requires=("alpha",), lifespan=lifespan_for("beta", beta)),
            ModuleDefinition("gamma", lifespan=lifespan_for("gamma", gamma)),
            ModuleDefinition("alpha", lifespan=lifespan_for("alpha", alpha)),
        )
    )
    assert [definition.name for definition in catalog.definitions] == ["alpha", "beta", "gamma"]
    async with AsyncClient() as client:
        async with AsyncExitStack() as stack:
            bindings = await catalog.start(
                stack,
                settings=settings(),
                http_client=client,
                role=role,
                resources=resources,
            )
            assert contexts["alpha"].dependencies == {}
            assert contexts["gamma"].dependencies == {}
            assert contexts["beta"].dependencies == {"alpha": alpha}
            assert contexts["beta"].role == role
            assert contexts["beta"].resources is resources
            assert bindings.services == {"alpha": alpha, "beta": beta, "gamma": gamma}
            with pytest.raises(TypeError):
                contexts["beta"].dependencies["gamma"] = gamma
            with pytest.raises(TypeError):
                bindings.services["delta"] = object()
    assert events == [
        "alpha.open",
        "beta.open",
        "gamma.open",
        "gamma.close",
        "beta.close",
        "alpha.close",
    ]


async def test_module_exports_are_namespaced_and_share_the_explicit_job_registry():
    kernel, dataset, facade = PureKernel(), PureDataset(), object()
    registry = TaskRegistry()
    dispatcher = SimpleNamespace(dispatch=AsyncMock())
    jobs = JobService(dispatcher, registry)
    contexts = []

    @asynccontextmanager
    async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        contexts.append(context)
        # Modules can retain jobs for later requests; no work runs during startup.
        yield ModuleExports(
            services=facade,
            tasks={"echo": pure_echo},
            kernels={"echo": kernel},
            datasets={"echo": dataset},
        )

    catalog = ModuleCatalog(
        (
            ModuleDefinition("alpha", lifespan=lifespan),
            ModuleDefinition("beta", lifespan=lifespan),
        )
    )
    async with AsyncExitStack() as stack, AsyncClient() as client:
        bindings = await catalog.start(
            stack,
            settings=settings(),
            http_client=client,
            role="api",
            registry=registry,
            jobs=jobs,
        )
        assert bindings.registry is registry
        assert all(context.jobs is jobs for context in contexts)
        dispatcher.dispatch.assert_not_awaited()
        for prefix in ("alpha", "beta"):
            assert bindings.services[prefix] is facade
            assert bindings.registry.resolve(f"{prefix}.echo") is pure_echo
            assert bindings.kernels[f"{prefix}.echo"] is kernel
            assert bindings.datasets[f"{prefix}.echo"] is dataset
        for exports in (bindings.kernels, bindings.datasets):
            with pytest.raises(TypeError):
                exports["gamma.echo"] = object()
        with pytest.raises(UnknownTask):
            bindings.registry.resolve("echo")
        await jobs.submit("beta.echo", {"value": None})
        assert dispatcher.dispatch.await_args.args[0].name == "beta.echo"


@pytest.mark.parametrize("export_kind", ["tasks", "kernels", "datasets"])
@pytest.mark.parametrize("local_name", ["", "Bad", "bad-name", "alpha.echo"])
async def test_invalid_local_export_names_roll_back_module_lifespan(export_kind, local_name):
    events = []
    value = {"tasks": pure_echo, "kernels": PureKernel(), "datasets": PureDataset()}[export_kind]

    @asynccontextmanager
    async def lifespan(context):
        events.append("open")
        try:
            yield ModuleExports(**{export_kind: {local_name: value}})
        finally:
            events.append("close")

    catalog = ModuleCatalog((ModuleDefinition("alpha", lifespan=lifespan),))
    async with AsyncClient() as client:
        with pytest.raises(ValueError, match="Invalid .* name in module alpha"):
            async with AsyncExitStack() as stack:
                await catalog.start(stack, settings=settings(), http_client=client, role="api")
    assert events == ["open", "close"]


@pytest.mark.parametrize("bad_exports", [object(), ModuleExports(tasks={"echo": object()})])
async def test_invalid_exports_and_noncallable_tasks_are_rejected_and_cleaned_up(bad_exports):
    events = []

    @asynccontextmanager
    async def lifespan(context):
        try:
            yield bad_exports
        finally:
            events.append("close")

    catalog = ModuleCatalog((ModuleDefinition("alpha", lifespan=lifespan),))
    async with AsyncClient() as client:
        with pytest.raises(TypeError):
            async with AsyncExitStack() as stack:
                await catalog.start(stack, settings=settings(), http_client=client, role="api")
    assert events == ["close"]


@pytest.mark.parametrize("role", ["api", "worker"])
async def test_partial_module_startup_rolls_back_only_started_modules_in_reverse_order(role):
    events = []

    @asynccontextmanager
    async def alpha(context):
        events.append("alpha.open")
        try:
            yield ModuleExports()
        finally:
            events.append("alpha.close")

    @asynccontextmanager
    async def beta(context):
        events.append("beta.open")
        try:
            raise RuntimeError("technical startup failure")
            yield ModuleExports()
        finally:
            events.append("beta.close")

    @asynccontextmanager
    async def gamma(context):
        events.append("gamma.open")
        yield ModuleExports()

    catalog = ModuleCatalog(
        (
            ModuleDefinition("alpha", lifespan=alpha),
            ModuleDefinition("beta", requires=("alpha",), lifespan=beta),
            ModuleDefinition("gamma", lifespan=gamma),
        )
    )
    async with AsyncClient() as client:
        with pytest.raises(RuntimeError, match="technical startup failure"):
            async with AsyncExitStack() as stack:
                await catalog.start(stack, settings=settings(), http_client=client, role=role)
    assert events == ["alpha.open", "beta.open", "beta.close", "alpha.close"]


@pytest.mark.parametrize("runtime_role", ["api", "worker"])
async def test_api_and_worker_rollback_shared_resources_when_module_startup_fails(
    monkeypatch, runtime_role
):
    events, clients, roles = [], [], []

    @asynccontextmanager
    async def alpha(context):
        roles.append(context.role)
        events.append("alpha.open")
        try:
            yield ModuleExports()
        finally:
            assert not context.http_client.is_closed
            events.append("alpha.close")

    @asynccontextmanager
    async def beta(context):
        raise RuntimeError("technical startup failure")
        yield ModuleExports()

    async def open_resources(stack, config):
        events.append("resources.open")

        async def close():
            events.append("resources.close")

        stack.push_async_callback(close)
        return MappingProxyType({})

    def make_client(**kwargs):
        client = AsyncClient(**kwargs)
        clients.append(client)
        return client

    definitions = (
        ModuleDefinition("alpha", lifespan=alpha),
        ModuleDefinition("beta", requires=("alpha",), lifespan=beta),
    )
    if runtime_role == "api":
        monkeypatch.setattr(server, "AsyncClient", make_client)
        monkeypatch.setattr(server, "open_resources", open_resources)
        app = server.create_app(settings(), modules=definitions)
        with pytest.raises(RuntimeError, match="technical startup failure"):
            async with app.router.lifespan_context(app):
                pytest.fail("Failed module startup cannot yield a running application")
        assert app.state.runtime is None
    else:
        store = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
        monkeypatch.setattr(taskiq_worker, "RedisJobStore", lambda *_: store)
        monkeypatch.setattr(taskiq_worker, "AsyncClient", make_client)
        monkeypatch.setattr(taskiq_worker, "open_resources", open_resources)
        worker_settings = Settings(_env_file=None, environment="test", task_backend="distributed")
        broker = taskiq_worker.build_worker_broker(worker_settings, modules=definitions)
        state = TaskiqState()
        with pytest.raises(RuntimeError, match="technical startup failure"):
            await broker.event_handlers[TaskiqEvents.WORKER_STARTUP][0](state)
        store.start.assert_awaited_once()
        store.aclose.assert_awaited_once()
        # A later shutdown hook is safe even after startup rolled back its stack.
        await broker.event_handlers[TaskiqEvents.WORKER_SHUTDOWN][0](state)
        store.aclose.assert_awaited_once()
    assert roles == [runtime_role]
    assert events == ["resources.open", "alpha.open", "alpha.close", "resources.close"]
    assert len(clients) == 1 and clients[0].is_closed
