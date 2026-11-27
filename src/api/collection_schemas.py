"""Strict query strings and typed bounded metadata pages."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.domain.pagination import MAX_COLLECTION_OFFSET, CollectionWindow

T = TypeVar("T")


class CollectionError(BaseModel):
    code: str
    message: str
    request_id: str
    retryable: bool


class CollectionAuthenticationError(BaseModel):
    detail: str


COLLECTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Stored metadata failed integrity checks.", "model": CollectionError},
    401: {
        "description": "Sign in again.",
        "model": CollectionError | CollectionAuthenticationError,
    },
    403: {"description": "Required read permission is missing.", "model": CollectionError},
    422: {
        "description": "Invalid or unknown query/path fields; correct the request.",
        "model": CollectionError,
    },
    503: {
        "description": "Catalog unavailable or not configured; retry with backoff.",
        "model": CollectionError,
    },
}


class CollectionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    limit: int = Field(default=25, ge=1, le=100, description="Maximum items, default 25.")
    offset: int = Field(
        default=0, ge=0, le=MAX_COLLECTION_OFFSET, description="Offset in deterministic ordering."
    )
    created_after: float | None = Field(
        default=None, ge=0, description="Strictly after UTC Unix time."
    )
    created_before: float | None = Field(
        default=None, ge=0, description="Strictly before UTC Unix time."
    )

    def window(self) -> CollectionWindow:
        return CollectionWindow(self.limit, self.offset, self.created_after, self.created_before)


class MetadataPage(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[T]
    limit: int
    offset: int
    next_offset: int | None = Field(
        ge=0,
        le=MAX_COLLECTION_OFFSET,
        description="Null when there are no more results or the next offset exceeds the maximum.",
    )
