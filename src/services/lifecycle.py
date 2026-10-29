"""Technical shutdown ordering; composition owns resource construction."""

from contextlib import AsyncExitStack

from src.shared_models.types import AsyncCloseable, ThreadDrainer


def register_execution_cleanup(
    stack: AsyncExitStack,
    *,
    dispatcher: AsyncCloseable,
    active_operations: AsyncCloseable,
    threads: ThreadDrainer | None = None,
) -> None:
    """Drain dispatch, cancel active work, then drain physical threads before resources."""
    if threads is not None:
        stack.push_async_callback(threads.drain)
    stack.push_async_callback(active_operations.aclose)
    stack.push_async_callback(dispatcher.aclose)
