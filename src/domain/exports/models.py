import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from src.domain.exports.errors import InvalidExportRequest
from src.shared_models.types import JSONValue as JSONValue

ExportRow: TypeAlias = Mapping[str, JSONValue]
ExportFormat: TypeAlias = Literal["csv", "jsonl"]
MAX_EXPORT_ROWS = 10_000
SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
COLUMN_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


def is_protected_field(name: str) -> bool:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return name.startswith("_") or any(
        marker in normalized
        for marker in (
            "password",
            "passwd",
            "secret",
            "token",
            "session",
            "credential",
            "authorization",
            "privatekey",
            "apikey",
        )
    )


def validate_source_name(name: str) -> None:
    if len(name) > 129 or SOURCE_NAME.fullmatch(name) is None:
        raise InvalidExportRequest("Invalid export source name")


@dataclass(frozen=True, slots=True)
class ExportColumn:
    name: str
    permission: str | None = None

    def __post_init__(self) -> None:
        if COLUMN_NAME.fullmatch(self.name) is None or is_protected_field(self.name):
            raise InvalidExportRequest("Invalid or protected export column")
        if self.permission is not None and (
            not self.permission.strip() or len(self.permission) > 128
        ):
            raise InvalidExportRequest("Invalid export column permission")


@dataclass(frozen=True, slots=True)
class ExportRequest:
    source_name: str
    format: ExportFormat = "csv"
    limit: int = 1000

    def __post_init__(self) -> None:
        validate_source_name(self.source_name)
        if self.format not in {"csv", "jsonl"}:
            raise InvalidExportRequest("Unsupported export format")
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= MAX_EXPORT_ROWS
        ):
            raise InvalidExportRequest(f"Export limit must be between 1 and {MAX_EXPORT_ROWS}")


@dataclass(frozen=True, slots=True)
class ExportDescriptor:
    source_name: str
    columns: tuple[ExportColumn, ...]


@dataclass(frozen=True, slots=True)
class PreparedExport:
    columns: tuple[ExportColumn, ...]
    format: ExportFormat
    filename: str
    media_type: str
    stream: AsyncIterator[bytes]
