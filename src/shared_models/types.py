"""Neutral JSON and lifecycle contracts; no transport or domain dependencies."""

from typing import Any, Protocol, TypeAlias

Payload: TypeAlias = dict[str, Any]
JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class ThreadDrainer(Protocol):
    async def drain(self) -> None: ...
