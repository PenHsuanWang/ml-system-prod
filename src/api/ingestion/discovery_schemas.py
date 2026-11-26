"""Safe ingestion job and published snapshot collection metadata."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.api.collection_schemas import CollectionQuery
from src.domain.ingestion.discovery import IngestionCollectionQuery

IngestionState = Literal[
    "QUEUED",
    "ACQUIRING",
    "VALIDATING",
    "PUBLISHING",
    "PUBLISHED",
    "RETRY_WAIT",
    "NEEDS_RECONCILIATION",
    "FAILED",
    "CANCELLED",
]
SourceKey = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]


class SnapshotCollectionQuery(CollectionQuery):
    source_id: SourceKey | None = None
    source_version: SourceKey | None = None

    def query(self) -> IngestionCollectionQuery:
        return IngestionCollectionQuery(self.window(), self.source_id, self.source_version)


class JobCollectionQuery(SnapshotCollectionQuery):
    state: IngestionState | None = None

    def query(self) -> IngestionCollectionQuery:
        return IngestionCollectionQuery(
            self.window(), self.source_id, self.source_version, self.state
        )


class IngestionJobMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str
    state: IngestionState
    revision: int
    source_id: str
    source_version: str
    attempt_id: str | None
    attempt_count: int
    snapshot_id: str
    created_at: float = Field(description="UTC Unix time when the job was created.")
    deadline: float = Field(description="Absolute execution deadline, UTC Unix seconds.")
    cancel_requested: bool
    error_code: str | None
    status_url: str
    result_url: str


class RawSnapshotMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot_id: str
    job_id: str
    source_id: str
    source_version: str
    source_configuration_digest: str
    content_digest: str
    manifest_digest: str
    data_format: str
    schema_version: str
    size_bytes: int = Field(description="Published raw payload size in bytes.")
    record_count: int = Field(description="Number of validated raw records.")
    created_at: float = Field(description="UTC Unix snapshot creation time.")
    source_revision: str
