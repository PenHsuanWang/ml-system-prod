"""Opt-in system test; services must already be reachable at APP_*_URL."""

import asyncio
import json
import os
import re
import signal
import socket
import sys
from contextlib import ExitStack, suppress
from pathlib import Path
from uuid import uuid4

import aio_pika
import httpx
import pytest
from aio_pika.exceptions import ChannelNotFoundEntity
from redis.asyncio import Redis

from src.config.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_SYSTEM_INTEGRATION") != "1",
        reason="Set RUN_SYSTEM_INTEGRATION=1 with RabbitMQ and Redis running",
    ),
]
ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def group_exists(process_id: int) -> bool:
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    return True


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + 8
    # Process-group membership has no asyncio event; include surviving children.
    while group_exists(process.pid) and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    if group_exists(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    await asyncio.wait_for(process.wait(), timeout=5)


async def delete_test_resources(settings: Settings, namespace: str) -> None:
    """Delete only the exact topology and Redis prefix created by this test."""
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    try:
        keys = [key async for key in redis.scan_iter(match=f"{namespace}:job:*")]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()
    connection = await aio_pika.connect_robust(settings.rabbitmq_url.get_secret_value(), timeout=5)
    try:
        for name in (namespace, f"{namespace}.dead_letter"):
            async with connection.channel() as channel:
                with suppress(ChannelNotFoundEntity):
                    await channel.queue_delete(name, if_unused=False, if_empty=False)
        async with connection.channel() as channel:
            with suppress(ChannelNotFoundEntity):
                await channel.exchange_delete(f"{namespace}.exchange", if_unused=False)
    finally:
        await connection.close()


def request_worker_pids(log: Path) -> set[int]:
    pids = set()
    for line in log.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("logger") == "src.api.http" and record.get("message", "").startswith(
            "GET /api/v1/jobs/"
        ):
            pids.add(int(record["process_id"]))
    return pids


async def test_two_api_workers_share_distributed_jobs(tmp_path: Path) -> None:
    namespace = f"integration.{uuid4().hex}"
    settings = Settings(
        _env_file=None,
        task_backend="distributed",
        queue_name=namespace,
        identity_enabled=False,
        mongo_enabled=False,
        kernel_api_enabled=False,
        exports_api_enabled=False,
        resource_backend="memory",
    )
    port = free_port()
    environment = {
        **os.environ,
        "APP_ENVIRONMENT": "test",
        "APP_TASK_BACKEND": "distributed",
        "APP_EXPOSE_JOB_API": "true",
        "APP_IDENTITY_ENABLED": "false",
        "APP_MONGO_ENABLED": "false",
        "APP_KERNEL_API_ENABLED": "false",
        "APP_EXPORTS_API_ENABLED": "false",
        "APP_RESOURCE_BACKEND": "memory",
        "APP_QUEUE_NAME": namespace,
        "APP_WORKERS": "2",
        "APP_RELOAD": "false",
        "APP_TASK_CONCURRENCY": "2",
        "APP_WORKER_PROCESSES": "2",
        "APP_JOB_RETENTION_SECONDS": "120",
        "APP_JOB_TIMEOUT_SECONDS": "5",
        "APP_SHUTDOWN_TIMEOUT_SECONDS": "2",
        "APP_LOG_LEVEL": "info",
        "APP_RABBITMQ_URL": settings.rabbitmq_url.get_secret_value(),
        "APP_REDIS_URL": settings.redis_url.get_secret_value(),
        "PYTHONUNBUFFERED": "1",
    }
    processes: list[asyncio.subprocess.Process] = []
    api_log = tmp_path / "api.log"
    worker_log = tmp_path / "worker.log"
    with ExitStack() as stack:
        api_output = stack.enter_context(api_log.open("wb"))
        worker_output = stack.enter_context(worker_log.open("wb"))
        try:
            async with asyncio.timeout(60):
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "taskiq",
                        "worker",
                        "tests.fixtures.worker:broker",
                        "--workers",
                        "2",
                        "--max-async-tasks",
                        "2",
                        "--ack-type",
                        "when_saved",
                        "--wait-tasks-timeout",
                        "2",
                        "--shutdown-timeout",
                        "2",
                        cwd=ROOT,
                        env=environment,
                        start_new_session=True,
                        stdout=worker_output,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                )
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "tests.fixtures.api:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--workers",
                        "2",
                        cwd=ROOT,
                        env=environment,
                        start_new_session=True,
                        stdout=api_output,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                )
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=2,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=0),
                ) as client:
                    while True:
                        assert all(process.returncode is None for process in processes), (
                            f"A child exited; inspect {api_log} and {worker_log}"
                        )
                        startup_pids = set(
                            re.findall(r"Runtime started pid=(\d+)", api_log.read_text())
                        )
                        with suppress(httpx.HTTPError):
                            response = await client.get("/health/ready")
                            if response.status_code == 200 and len(startup_pids) == 2:
                                break
                        await asyncio.sleep(0.05)

                    submissions = await asyncio.gather(
                        *(client.post("/api/v1/probe/tasks", json={"value": i}) for i in range(12))
                    )
                    pending = {}
                    for index, response in enumerate(submissions):
                        assert response.status_code == 202, response.text
                        pending[response.headers["Location"]] = index
                    locations = list(pending)
                    while pending:
                        for location in list(pending):
                            response = await client.get(location)
                            assert response.status_code == 200, response.text
                            snapshot = response.json()
                            assert snapshot["status"] not in {"failed", "cancelled"}, snapshot
                            if snapshot["status"] == "succeeded":
                                assert snapshot["result"] == {"value": pending.pop(location)}
                        await asyncio.sleep(0.05)

                    # New connections allow both accept loops to service reads.
                    # The per-request application log proves which API process
                    # actually retrieved the Redis-backed snapshots.
                    while len(request_worker_pids(api_log)) < 2:
                        responses = await asyncio.gather(
                            *(client.get(location) for location in locations * 2)
                        )
                        assert all(response.status_code == 200 for response in responses)
                        await asyncio.sleep(0.05)
                    assert {str(pid) for pid in request_worker_pids(api_log)} == startup_pids
        finally:
            await asyncio.gather(*(stop_process_group(process) for process in reversed(processes)))
            await delete_test_resources(settings, namespace)
