"""Strict, minimal metadata-only HTTP contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ConnectionReference = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class SchemaQuery(PageQuery):
    catalog: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class TableQuery(SchemaQuery):
    schema_name: str = Field(
        alias="schema", min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )


class ColumnQuery(TableQuery):
    table: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class MetadataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    table_type: str | None = None
    native_type: str | None = None
    nullable: bool | None = None
    ingestion_type: str | None = None
    supported: bool | None = None


class MetadataPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[MetadataResponse]
    limit: int
    next_offset: int | None
    has_more: bool
    version_discovery: Literal["unavailable"] = "unavailable"


class ConnectionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[str]
    limit: int
    next_offset: int | None
    has_more: bool
