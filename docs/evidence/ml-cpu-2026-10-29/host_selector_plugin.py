"""Host-only workaround for blocked asyncio self-pipe writes; no application patch."""
import asyncio
import selectors
import pytest


class PollingSelector(selectors.DefaultSelector):
    def select(self, timeout=None):
        return super().select(0.02 if timeout is None else min(timeout, 0.02))


class PollingPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        return asyncio.SelectorEventLoop(PollingSelector())


@pytest.fixture(scope='session')
def event_loop_policy():
    return PollingPolicy()
