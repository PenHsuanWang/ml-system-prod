"""Typed filters for tenant/owner-scoped ingestion metadata discovery."""

from dataclasses import dataclass, field

from src.domain.ingestion.models import identifier
from src.domain.ingestion.states import TERMINAL, TRANSITIONS
from src.domain.ml import SchemaMismatch
from src.domain.pagination import CollectionWindow


@dataclass(frozen=True)
class IngestionCollectionQuery:
    window: CollectionWindow = field(default_factory=CollectionWindow)
    source_id: str | None = None
    source_version: str | None = None
    state: str | None = None

    def __post_init__(self) -> None:
        for value in (self.source_id, self.source_version):
            if value is not None:
                identifier(value)
        if self.state is not None and self.state not in set(TRANSITIONS) | TERMINAL:
            raise SchemaMismatch("Unknown ingestion job state")
