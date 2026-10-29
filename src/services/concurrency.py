"""Bound blocking SDK calls without treating cancellation as thread termination."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import Any, TypeVar

from src.config.settings import Settings
from src.domain.execution import ExecutionBusy

T = TypeVar("T")


class BoundedThreadExecutor:
    def __init__(self, settings: Settings) -> None:
        self._workers = settings.thread_workers
        self._capacity = self._workers + settings.thread_queue_size
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: set[asyncio.Future[Any]] = set()
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Thread executor has been closed")
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="backend")

    async def run(self, function: Callable[..., T], /, *args: Any) -> T:
        if self._closed or self._pool is None:
            raise ExecutionBusy("Blocking execution is unavailable")
        if len(self._inflight) >= self._capacity:
            raise ExecutionBusy("Blocking execution capacity is full")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, copy_context().run, partial(function, *args))
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A Python thread cannot be killed safely. Keep the owning coroutine
            # (and any resource lease) alive until the function actually returns.
            # Blocking adapters must also set their own SDK/network timeouts.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise
        finally:
            # Completion callbacks may run after the awaiting caller resumes.
            # Release already-finished work synchronously, including exceptions.
            if future.done():
                self._inflight.discard(future)

    async def drain(self) -> None:
        cancelled = False
        while self._inflight:
            pending = asyncio.gather(*tuple(self._inflight), return_exceptions=True)
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            # A done callback removes the actual completed futures.
            completed = {future for future in self._inflight if future.done()}
            self._inflight.difference_update(completed)
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        self._closed = True
        try:
            await self.drain()
        finally:
            pool, self._pool = self._pool, None
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
