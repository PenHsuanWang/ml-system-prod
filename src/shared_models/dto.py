"""Neutral data transfer structures; business entities remain in domain."""

from typing import TypedDict

from src.shared_models.schemas import ErrorDetail


class ErrorEnvelope(TypedDict):
    error: ErrorDetail
    request_id: str
