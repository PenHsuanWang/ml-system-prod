"""Shared HTTP schemas; domain contracts remain in ``src.domain``."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok", "ready", "unavailable"]
