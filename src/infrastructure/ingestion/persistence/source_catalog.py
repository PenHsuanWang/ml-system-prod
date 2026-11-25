"""Approved immutable source versions, with revocable execution eligibility."""

import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from src.domain.ingestion.errors import IngestionError, SourceRejected
from src.domain.ingestion.models import IngestionJob, SourceDefinition
from src.domain.ml import Actor, Conflict, NotFound, canonical_json
from src.infrastructure.ingestion.persistence.schema import source_definitions
from src.infrastructure.persistence.ml_database import Database
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit


def read_source(
    connection: Connection,
    tenant_id: str,
    source_id: str,
    version: str,
    actor_id: str,
    *,
    expected_digest: str | None = None,
) -> SourceDefinition:
    row = (
        connection.execute(
            select(source_definitions).where(
                source_definitions.c.tenant_id == tenant_id,
                source_definitions.c.source_id == source_id,
                source_definitions.c.version == version,
            )
        )
        .mappings()
        .first()
    )
    if row is None or not row["enabled"]:
        raise SourceRejected("Approved source is unavailable")
    definition = source_from_row(row)
    if (
        definition.tenant_id != tenant_id
        or expected_digest is not None
        and definition.configuration_digest != expected_digest
        or definition.configuration_digest != row["configuration_digest"]
        or definition.allowed_actor_ids
        and actor_id not in definition.allowed_actor_ids
    ):
        raise SourceRejected("Source is not approved for this execution")
    return definition


def source_from_row(row: Mapping[str, Any]) -> SourceDefinition:
    return SourceDefinition.parse(json.loads(row["definition_json"]))


class SQLSourceCatalog:
    def __init__(
        self,
        database: Database,
        authorize_execution: Callable[[SourceDefinition, str], None] | None = None,
        authorize_registration: Callable[[Actor, SourceDefinition], None] | None = None,
    ) -> None:
        self.db = database
        self.authorize_execution = authorize_execution
        self.authorize_registration = authorize_registration

    def metadata(self, actor: Actor, source_id: str, version: str) -> tuple[SourceDefinition, bool]:
        actor.require("ml:source:manage")
        with self.db.transaction() as connection:
            row = (
                connection.execute(
                    select(source_definitions).where(
                        source_definitions.c.tenant_id == actor.tenant_id,
                        source_definitions.c.source_id == source_id,
                        source_definitions.c.version == version,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("Source not found")
            return self._metadata(row)

    @staticmethod
    def _metadata(row: Mapping[str, Any]) -> tuple[SourceDefinition, bool]:
        source = source_from_row(row)
        if source.configuration_digest != row["configuration_digest"]:
            raise SourceRejected("Source integrity verification failed")
        return source, bool(row["enabled"])

    def list_metadata(
        self, actor: Actor, source_id: str | None, limit: int, offset: int
    ) -> list[tuple[SourceDefinition, bool]]:
        actor.require("ml:source:manage")
        query = select(source_definitions).where(source_definitions.c.tenant_id == actor.tenant_id)
        if source_id is not None:
            query = query.where(source_definitions.c.source_id == source_id)
        with self.db.transaction() as connection:
            rows = connection.execute(
                query.order_by(source_definitions.c.source_id, source_definitions.c.version)
                .limit(limit)
                .offset(offset)
            ).mappings()
            return [self._metadata(row) for row in rows]

    def register(self, actor: Actor, definition: SourceDefinition) -> SourceDefinition:
        actor.require("ml:source:manage")
        if definition.tenant_id != actor.tenant_id:
            raise NotFound("Source not found")
        if self.authorize_registration is not None:
            self.authorize_registration(actor, definition)
        frozen = canonical_json(definition.to_dict())
        with self.db.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            existing = (
                connection.execute(
                    select(source_definitions).where(
                        source_definitions.c.tenant_id == actor.tenant_id,
                        source_definitions.c.source_id == definition.source_id,
                        source_definitions.c.version == definition.version,
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if (
                    existing["configuration_digest"] != definition.configuration_digest
                    or existing["definition_json"] != frozen
                ):
                    raise Conflict("Approved source versions are immutable")
                return source_from_row(existing)
            connection.execute(
                insert(source_definitions).values(
                    id=str(uuid4()),
                    tenant_id=actor.tenant_id,
                    source_id=definition.source_id,
                    version=definition.version,
                    definition_json=frozen,
                    configuration_digest=definition.configuration_digest,
                    enabled=True,
                    created_at=time.time(),
                )
            )
            record_audit(
                connection,
                actor,
                "ingestion.source.register",
                definition.configuration_digest,
            )
        return definition

    def resolve(self, actor: Actor, source_id: str, version: str) -> SourceDefinition:
        with self.db.transaction() as connection:
            source = read_source(connection, actor.tenant_id, source_id, version, actor.actor_id)
            if self.authorize_execution is not None:
                self.authorize_execution(source, actor.actor_id)
            return source

    def for_execution(self, job: IngestionJob) -> SourceDefinition:
        with self.db.transaction() as connection:
            source = read_source(
                connection,
                job.tenant_id,
                job.source_id,
                job.source_version,
                job.owner_id,
                expected_digest=job.source_digest,
            )
            if self.authorize_execution is not None:
                self.authorize_execution(source, job.owner_id)
            return source

    def set_enabled(self, actor: Actor, source_id: str, version: str, enabled: bool) -> None:
        actor.require("ml:source:manage")
        if type(enabled) is not bool:
            raise IngestionError("Source enabled flag must be boolean")
        with self.db.transaction() as connection:
            lock_tenant(connection, actor.tenant_id)
            row = (
                connection.execute(
                    select(source_definitions).where(
                        source_definitions.c.tenant_id == actor.tenant_id,
                        source_definitions.c.source_id == source_id,
                        source_definitions.c.version == version,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("Source not found")
            source, _ = self._metadata(row)
            if self.authorize_registration is not None:
                self.authorize_registration(actor, source)
            changed = connection.execute(
                update(source_definitions)
                .where(
                    source_definitions.c.tenant_id == actor.tenant_id,
                    source_definitions.c.source_id == source_id,
                    source_definitions.c.version == version,
                )
                .values(enabled=enabled)
            )
            if changed.rowcount != 1:
                raise NotFound("Source not found")
            record_audit(
                connection,
                actor,
                "ingestion.source.enable" if enabled else "ingestion.source.disable",
                source.configuration_digest,
            )
