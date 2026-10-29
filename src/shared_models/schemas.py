"""Framework-independent schemas for neutral cross-boundary structures."""

from typing import TypedDict


class ErrorDetail(TypedDict):
    code: str
    message: str
