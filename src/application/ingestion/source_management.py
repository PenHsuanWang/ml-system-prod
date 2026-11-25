"""Governed immutable registration and lifecycle operations through narrow ports."""

from src.application.ingestion.ports import SourceApproval, SourceManagementRepository
from src.domain.ingestion.errors import IngestionError
from src.domain.ingestion.models import ExtractionLimits, SourceDefinition
from src.domain.ml import Actor, Forbidden


class SourceManagementService:
    def __init__(self, repository: SourceManagementRepository, approval: SourceApproval) -> None:
        self.repository, self.approval = repository, approval

    def connections(self, actor: Actor) -> list[str]:
        actor.require("ml:source:manage")
        return self.approval.connections(actor)

    def register(
        self,
        actor: Actor,
        source_id: str,
        version: str,
        configuration: dict[str, object],
        limits: ExtractionLimits,
        allowed_actor_ids: tuple[str, ...],
    ) -> tuple[SourceDefinition, bool]:
        actor.require("ml:source:manage")
        source = self.approval.create(
            actor, source_id, version, configuration, limits, allowed_actor_ids
        )
        self.repository.register(actor, source)
        return self.get(actor, source_id, version)

    def get(self, actor: Actor, source_id: str, version: str) -> tuple[SourceDefinition, bool]:
        actor.require("ml:source:manage")
        value = self.repository.metadata(actor, source_id, version)
        self.approval.authorize(actor, value[0])
        return value

    def list(
        self, actor: Actor, source_id: str | None, limit: int, offset: int
    ) -> list[tuple[SourceDefinition, bool]]:
        actor.require("ml:source:manage")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise IngestionError("Invalid source page bounds")
        # The SQL page is bounded before authorization filtering. Clients advance by
        # limit even when policy filtering produces an empty page.
        values = self.repository.list_metadata(actor, source_id, limit, offset)
        result = []
        for value in values:
            try:
                self.approval.authorize(actor, value[0])
            except Forbidden:
                continue
            result.append(value)
        return result

    def set_enabled(
        self, actor: Actor, source_id: str, version: str, enabled: bool
    ) -> tuple[SourceDefinition, bool]:
        source, _ = self.get(actor, source_id, version)
        self.repository.set_enabled(actor, source.source_id, source.version, enabled)
        return self.get(actor, source_id, version)
