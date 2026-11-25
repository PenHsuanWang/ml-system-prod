"""Read-only discovery contracts. No source registration or external credentials."""

import re
from dataclasses import dataclass
from typing import Literal

from src.domain.ml import MLError, SchemaMismatch

MetadataKind = Literal["catalogs", "schemas", "tables", "columns"]


def metadata_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise SchemaMismatch("Invalid metadata identifier")
    return value


@dataclass(frozen=True)
class DiscoveryScope:
    table_scope: tuple[str, ...]
    columns: tuple[str, ...]


@dataclass(frozen=True)
class MetadataQuery:
    kind: MetadataKind
    path: tuple[str, ...] = ()
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        lengths = {"catalogs": 0, "schemas": 1, "tables": 2, "columns": 3}
        if self.kind not in lengths or len(self.path) != lengths[self.kind]:
            raise SchemaMismatch("Invalid metadata operation")
        for part in self.path:
            metadata_name(part)
        if (
            type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or type(self.offset) is not int
            or not 0 <= self.offset <= 1_000_000
        ):
            raise SchemaMismatch("Invalid metadata pagination")


@dataclass(frozen=True)
class MetadataItem:
    name: str
    table_type: str | None = None
    native_type: str | None = None
    nullable: bool | None = None
    ingestion_type: str | None = None
    supported: bool | None = None


@dataclass(frozen=True)
class MetadataPage:
    items: tuple[MetadataItem, ...]
    limit: int
    next_offset: int | None
    has_more: bool
    version_discovery: Literal["unavailable"] = "unavailable"


class DiscoveryFailed(MLError):
    code, status = "CATALOG_UNAVAILABLE", 503


class DiscoveryTimeout(DiscoveryFailed):
    code, status, retryable = "CATALOG_TIMEOUT", 504, True
