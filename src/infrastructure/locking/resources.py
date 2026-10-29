"""Process-local or Redis-coordinated capacity leases with owner-safe renewal."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from hashlib import sha256
from uuid import uuid4

from redis.asyncio import Redis

from src.config.settings import Settings
from src.domain.execution import ResourceBusy, ResourceLease, ResourceUnavailable

_CLAIM = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
local fencing = redis.call('INCR', KEYS[2])
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2])*2)
return fencing
"""
_RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2])*2)
return 1
"""


class ResourceCoordinator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphores = {
            name: asyncio.Semaphore(capacity) for name, capacity in settings.resource_limits.items()
        }
        self._client: Redis | None = None
        self._counter = 0
        self._closed = False
        self._started = False

    async def start(self) -> None:
        if self._closed:
            raise ResourceUnavailable("Resource coordination has been closed")
        if self._started:
            return
        if self._settings.resource_backend == "redis":
            client: Redis | None = None
            try:
                client = Redis.from_url(
                    self._settings.redis_url.get_secret_value(),
                    socket_connect_timeout=3,
                    socket_timeout=3,
                )
                await client.ping()
            except BaseException as exc:
                if client is not None:
                    with suppress(Exception):
                        await client.aclose()
                if not isinstance(exc, Exception):
                    raise
                raise ResourceUnavailable("Resource coordination is unavailable") from None
            self._client = client
        self._started = True

    async def is_ready(self) -> bool:
        if self._closed or not self._started:
            return False
        if self._settings.resource_backend == "memory":
            return True
        try:
            return self._client is not None and bool(await self._client.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        self._closed = True
        self._started = False
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _keys(self, name: str) -> tuple[str, str]:
        digest = sha256(f"{self._settings.queue_name}:{name}".encode()).hexdigest()
        prefix = f"backend:resources:{{{digest}}}"
        return f"{prefix}:leases", f"{prefix}:fence"

    @asynccontextmanager
    async def lease(self, name: str, *, wait_seconds: float) -> AsyncIterator[ResourceLease]:
        if self._closed or not self._started or name not in self._semaphores:
            raise ResourceUnavailable("Resource group is unavailable")
        if self._settings.resource_backend == "memory":
            semaphore = self._semaphores[name]
            try:
                async with asyncio.timeout(wait_seconds):
                    await semaphore.acquire()
            except TimeoutError:
                raise ResourceBusy("Resource capacity is exhausted") from None
            self._counter += 1
            try:
                yield ResourceLease(name, self._counter)
            finally:
                semaphore.release()
            return

        client = self._client
        if client is None:
            raise ResourceUnavailable("Resource coordination has not started")
        key, fence_key = self._keys(name)
        owner = uuid4().hex
        ttl_ms = int(self._settings.resource_lease_seconds * 1000)
        try:
            async with asyncio.timeout(wait_seconds):
                while True:
                    try:
                        fencing = int(
                            await client.eval(
                                _CLAIM,
                                2,
                                key,
                                fence_key,
                                self._settings.resource_limits[name],
                                ttl_ms,
                                owner,
                            )
                        )
                    except Exception:
                        raise ResourceUnavailable("Resource coordination is unavailable") from None
                    if fencing:
                        break
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise ResourceBusy("Resource capacity is exhausted") from None

        lease = ResourceLease(name, fencing)

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._settings.resource_lease_seconds / 3)
                try:
                    alive = await client.eval(_RENEW, 1, key, owner, ttl_ms)
                except Exception:
                    alive = False
                if not alive:
                    lease.lost.set()
                    return

        heartbeat = asyncio.create_task(renew(), name=f"resource-lease-{name}")
        try:
            yield lease
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(Exception):
                await client.zrem(key, owner)
