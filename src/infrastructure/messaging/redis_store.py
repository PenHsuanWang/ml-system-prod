"""JSON job snapshots shared by API processes and dedicated task workers."""

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from math import ceil
from typing import Any

from redis.asyncio import Redis

from src.domain.jobs.errors import DispatcherUnavailable
from src.domain.jobs.models import JobSnapshot, JobStatus
from src.infrastructure.messaging.envelope import decode_request as decode_request
from src.infrastructure.messaging.envelope import encode_request as encode_request


def encode_snapshot(snapshot: JobSnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    data["status"] = snapshot.status.value
    for name in ("submitted_at", "started_at", "finished_at"):
        value = data[name]
        data[name] = value.isoformat() if value is not None else None
    return data


def decode_snapshot(data: dict[str, Any]) -> JobSnapshot:
    values = dict(data)
    values["status"] = JobStatus(values["status"])
    for name in ("submitted_at", "started_at", "finished_at"):
        value = values.get(name)
        values[name] = datetime.fromisoformat(value) if value is not None else None
    return JobSnapshot(**values)


class RedisJobStore:
    """Own one process-local client; constructing the store never opens a socket."""

    def __init__(self, url: str, retention_seconds: float, prefix: str) -> None:
        self._url = url
        self._ttl_ms = max(1, ceil(retention_seconds * 1000))
        self._prefix = prefix
        self._client: Redis | None = None

    async def start(self) -> None:
        if self._client is not None:
            return
        client: Redis | None = None
        try:
            client = Redis.from_url(
                self._url,
                decode_responses=False,
                socket_connect_timeout=5,
                socket_timeout=5,
                health_check_interval=30,
            )
            await client.ping()
        except BaseException as exc:
            if client is not None:
                with suppress(Exception):
                    await client.aclose()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise DispatcherUnavailable("Job storage is unavailable") from None
        self._client = client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                raise DispatcherUnavailable("Job storage could not be closed") from None

    async def is_ready(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    def _require_client(self) -> Redis:
        if self._client is None:
            raise DispatcherUnavailable("Job storage has not started")
        return self._client

    async def save(self, snapshot: JobSnapshot) -> None:
        try:
            data = json.dumps(encode_snapshot(snapshot), allow_nan=False)
            await self._require_client().set(
                f"{self._prefix}:job:{snapshot.job_id}", data, px=self._ttl_ms
            )
        except Exception:
            raise DispatcherUnavailable("Job state could not be stored") from None

    async def get(self, job_id: str) -> JobSnapshot | None:
        try:
            data = await self._require_client().get(f"{self._prefix}:job:{job_id}")
            if data is None:
                return None
            return decode_snapshot(json.loads(data))
        except Exception:
            raise DispatcherUnavailable("Job state could not be read") from None
