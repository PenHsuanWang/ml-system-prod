import asyncio
import io
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

import server
from bootstrap.worker import execute_request
from src.application.registry import TaskRegistry
from src.config.settings import Settings
from src.domain.jobs.models import JobSnapshot, JobStatus, TaskRequest
from src.infrastructure.messaging.memory import MemoryJobDispatcher
from src.infrastructure.messaging.redis_store import (
    RedisJobStore,
    decode_request,
    encode_request,
)
from src.services.observability import JsonFormatter, configure_logging
from src.shared_models.context import (
    bind_job_context,
    job_id_context,
    request_id_context,
)


def request(**changes: Any) -> TaskRequest:
    return replace(
        TaskRequest("job-1", "technical.echo", {"value": 1}, datetime.now(UTC)),
        **changes,
    )


def test_envelope_preserves_version_correlation_and_legacy_messages() -> None:
    original = request(tenant_id="tenant", owner_id="owner", request_id="request-1")
    encoded = encode_request(original)
    assert encoded["schema_version"] == 1
    assert encoded["request_id"] == "request-1"
    assert decode_request(json.loads(json.dumps(encoded))) == original
    legacy = dict(encoded)
    for field in ("schema_version", "request_id", "tenant_id", "owner_id"):
        legacy.pop(field)
    decoded = decode_request(legacy)
    assert decoded.schema_version == 1
    assert decoded.request_id is None
    assert decoded.tenant_id is None
    assert decoded.payload == original.payload


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"request_id": "invalid\r\nAuthorization: secret"},
        {"request_id": 1},
        {"tenant_id": "tenant"},
        {"owner_id": []},
        {"job_id": "invalid\nidentifier"},
        {"name": False},
        {"payload": []},
        {"payload": {"value": float("nan")}},
        {"submitted_at": "not-a-datetime"},
    ],
)
def test_envelope_rejects_unsupported_or_malformed_metadata(
    changes: dict[str, Any],
) -> None:
    encoded = encode_request(request())
    encoded.update(changes)
    with pytest.raises(ValueError):
        decode_request(encoded)


def test_formatter_emits_shared_metadata_and_safe_exception_type() -> None:
    formatter = JsonFormatter(service_name="backend", environment="test", role="worker")
    try:
        raise RuntimeError("mongodb://user:private-password@host/db bearer.private-token")
    except RuntimeError:
        record = logging.LogRecord(
            "src.technical",
            logging.ERROR,
            __file__,
            1,
            "Task failed",
            (),
            sys.exc_info(),
        )
    with bind_job_context("job-1", "request-1"):
        encoded = formatter.format(record)
    result = json.loads(encoded)
    assert result["service"] == "backend"
    assert result["environment"] == "test"
    assert result["role"] == "worker"
    assert result["request_id"] == "request-1"
    assert result["job_id"] == "job-1"
    assert result["exception_type"] == "RuntimeError"
    assert "private-password" not in encoded
    assert "private-token" not in encoded
    assert job_id_context.get() == "-"
    assert request_id_context.get() == "-"


def test_logging_configuration_updates_owned_handlers_without_duplication() -> None:
    roots = [logging.getLogger(name) for name in ("src", "bootstrap")]
    original = [(root.handlers[:], root.level, root.propagate) for root in roots]
    try:
        for _ in range(2):
            configure_logging("info", service_name="service", environment="test", role="worker")
        for root in roots:
            owned = [
                handler for handler in root.handlers if getattr(handler, "_backend_handler", False)
            ]
            assert len(owned) == 1
            record = logging.LogRecord(root.name, logging.INFO, __file__, 1, "Started", (), None)
            assert json.loads(owned[0].format(record))["role"] == "worker"
    finally:
        for root, (handlers, level, propagate) in zip(roots, original, strict=True):
            root.handlers[:] = handlers
            root.setLevel(level)
            root.propagate = propagate


async def test_memory_jobs_isolate_parallel_context_and_clear_legacy_context() -> None:
    observed: dict[str, tuple[str, str]] = {}
    both_started = asyncio.Event()
    both_finished = asyncio.Event()
    active = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        observed[payload["key"]] = (job_id_context.get(), request_id_context.get())
        if len(observed) == 2:
            both_finished.set()
        return {}

    registry = TaskRegistry()
    registry.register("technical.echo", handler)
    dispatcher = MemoryJobDispatcher(registry, concurrency=2)
    with bind_job_context("outer-job", "outer-request"):
        await dispatcher.start()
        try:
            await dispatcher.dispatch(
                request(job_id="a", request_id="request-a", payload={"key": "a"})
            )
            await dispatcher.dispatch(request(job_id="b", payload={"key": "b"}))
            await asyncio.wait_for(both_finished.wait(), 1)
        finally:
            await dispatcher.aclose()
        assert (job_id_context.get(), request_id_context.get()) == (
            "outer-job",
            "outer-request",
        )
    assert observed == {"a": ("a", "request-a"), "b": ("b", "-")}


async def test_worker_jobs_isolate_parallel_context_and_restore_after_failure() -> None:
    class Store:
        async def get(self, job_id: str) -> None:
            return None

        async def save(self, snapshot: JobSnapshot) -> None:
            await asyncio.sleep(0)

    observed: dict[str, tuple[str, str]] = {}

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        observed[payload["key"]] = (job_id_context.get(), request_id_context.get())
        if payload.get("fail"):
            raise RuntimeError("secret-token")
        return {}

    registry = TaskRegistry()
    registry.register("technical.echo", handler)
    store = cast(RedisJobStore, Store())
    with bind_job_context("outer-job", "outer-request"):
        first, second = await asyncio.gather(
            execute_request(
                request(job_id="a", request_id="request-a", payload={"key": "a"}),
                registry,
                store,
                1,
            ),
            execute_request(
                request(job_id="b", payload={"key": "b", "fail": True}),
                registry,
                store,
                1,
            ),
        )
        assert (job_id_context.get(), request_id_context.get()) == (
            "outer-job",
            "outer-request",
        )
    assert observed == {"a": ("a", "request-a"), "b": ("b", "-")}
    assert first["status"] == JobStatus.SUCCEEDED
    assert second["status"] == JobStatus.FAILED
    assert "secret-token" not in json.dumps(second)


async def test_api_submission_propagates_header_to_background_handler() -> None:
    app = server.create_app(Settings(_env_file=None, environment="test", task_backend="memory"))
    completed = asyncio.Event()
    observed: list[tuple[str, str]] = []

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        observed.append((job_id_context.get(), request_id_context.get()))
        completed.set()
        return {}

    @app.post("/technical-submit")
    async def submit() -> dict[str, str]:
        job = await app.state.runtime.jobs.submit("technical.echo", {"token": "never-logged"})
        return {"job_id": job.job_id}

    stream = io.StringIO()
    handler_log = logging.StreamHandler(stream)
    handler_log.setFormatter(JsonFormatter(service_name="service", environment="test", role="api"))
    application_logger = logging.getLogger("src")
    async with app.router.lifespan_context(app):
        app.state.runtime.registry.register("technical.echo", handler)
        application_logger.addHandler(handler_log)
        try:
            async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
                response = await client.post(
                    "/technical-submit", headers={"X-Request-ID": "request-http"}
                )
                await asyncio.wait_for(completed.wait(), 1)
        finally:
            application_logger.removeHandler(handler_log)
    assert response.status_code == 200
    assert observed == [(response.json()["job_id"], "request-http")]
    assert "never-logged" not in stream.getvalue()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(record["job_id"] == response.json()["job_id"] for record in records)
    assert (job_id_context.get(), request_id_context.get()) == ("-", "-")
