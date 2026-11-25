"""Strict runtime source transport contracts. Credentials are never accepted."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.ingestion.schemas import SourceIdentifier


class StrictSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ColumnRequest(StrictSourceModel):
    name: str = Field(min_length=1, max_length=128)
    type: Literal["boolean", "integer", "float", "decimal", "string", "date", "timestamp", "binary"]


class PredicateRequest(StrictSourceModel):
    column: str = Field(min_length=1, max_length=128)
    operator: Literal["eq", "ge", "gt", "le", "lt", "is_null"]
    value: str | bool | int | float | None = None


class LimitsRequest(StrictSourceModel):
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    max_records: int = Field(default=100000, ge=1, le=1000000)
    max_line_bytes: int = Field(default=1024 * 1024, ge=1, le=4 * 1024 * 1024)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class SourceVersionRequest(StrictSourceModel):
    version: SourceIdentifier
    adapter_type: Literal["databricks-sql"] = "databricks-sql"
    connection_ref: SourceIdentifier
    table: list[str] = Field(min_length=3, max_length=3)
    delta_version: int = Field(ge=0, lt=2**63)
    projection: list[ColumnRequest] = Field(min_length=1, max_length=128)
    predicates: list[PredicateRequest] = Field(default_factory=list, max_length=32)
    limits: LimitsRequest = Field(default_factory=LimitsRequest)
    allowed_actor_ids: list[SourceIdentifier] = Field(min_length=1, max_length=256)

    def configuration(self) -> dict[str, object]:
        return self.model_dump(
            include={"connection_ref", "table", "delta_version", "projection", "predicates"}
        )


class SourceRegisterRequest(SourceVersionRequest):
    source_id: SourceIdentifier


class SourceResponse(StrictSourceModel):
    source_id: str
    version: str
    adapter_type: str
    configuration_digest: str
    configuration: dict[str, object]
    limits: LimitsRequest
    allowed_actor_ids: list[str]
    enabled: bool
    external_qualification: Literal["not_verified"] = "not_verified"


class SourcePage(StrictSourceModel):
    items: list[SourceResponse]
    limit: int
    offset: int
    next_offset: int


class ConnectionResponse(StrictSourceModel):
    connection_ref: str
