"""Client requests identify approved sources and may only narrow their budgets."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ingestion.models import IngestionRequest

SourceIdentifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class IngestionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    source_id: SourceIdentifier
    source_version: SourceIdentifier
    max_bytes: int | None = Field(default=None, ge=1, le=128 * 1024 * 1024)
    max_records: int | None = Field(default=None, ge=1, le=1_000_000)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)

    def to_domain(self) -> IngestionRequest:
        return IngestionRequest(
            self.source_id,
            self.source_version,
            self.max_bytes,
            self.max_records,
            self.timeout_seconds,
        )
