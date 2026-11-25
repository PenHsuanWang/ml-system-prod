"""Deployment-owned access grants. No network clients or credential reads."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from src.domain.ingestion.models import ExtractionLimits, SourceDefinition, identifier
from src.domain.ml import Actor, Forbidden, canonical_json
from src.infrastructure.ingestion.adapters.databricks_contract import (
    RUNTIME_CONTRACT,
    SERIALIZATION_VERSION,
    ConnectionConfig,
    Template,
    _name,
    _object,
    parse_templates,
    validate_source,
)


class DatabricksSourceApproval:
    def __init__(
        self,
        connections: Mapping[str, ConnectionConfig],
        templates: Mapping[str, Template],
        policy: Callable[[], dict[str, Any]],
    ) -> None:
        self._connections, self.templates, self.policy = connections, templates, policy

    def _grants(self) -> list[dict[str, Any]]:
        envelope = _object(self.policy(), {"contract", "grants"})
        if (
            envelope["contract"] != "databricks-access-v1"
            or not isinstance(envelope["grants"], list)
            or len(envelope["grants"]) > 256
        ):
            raise Forbidden("Source access policy is unavailable")
        grants = []
        for item in envelope["grants"]:
            grant = _object(
                item,
                {"tenant_id", "manager_ids", "actor_ids", "template", "limits"},
                {"table_scope"},
            )
            identifier(grant["tenant_id"])
            for name in ("manager_ids", "actor_ids"):
                ids = grant[name]
                if (
                    not isinstance(ids, list)
                    or not 1 <= len(ids) <= 256
                    or any(not isinstance(value, str) for value in ids)
                    or len(set(ids)) != len(ids)
                ):
                    raise Forbidden("Invalid source access policy")
                for value in ids:
                    identifier(value)
            parse_templates({"grant": grant["template"]})
            ExtractionLimits.parse(grant["limits"])
            scope = grant.get("table_scope", grant["template"]["table"])
            if not isinstance(scope, list) or not 1 <= len(scope) <= 3:
                raise Forbidden("Invalid source table scope")
            for part in scope:
                _name(part)
            if grant["template"]["table"][: len(scope)] != scope:
                raise Forbidden("Invalid source table scope")
            grants.append(grant)
        return grants

    def connections(self, actor: Actor) -> list[str]:
        return sorted(
            {
                g["template"]["connection_ref"]
                for g in self._grants()
                if g["tenant_id"] == actor.tenant_id
                and actor.actor_id in g["manager_ids"]
                and g["template"]["connection_ref"] in self._connections
            }
        )

    def create(
        self,
        actor: Actor,
        source_id: str,
        version: str,
        configuration: dict[str, object],
        limits: ExtractionLimits,
        allowed_actor_ids: tuple[str, ...],
    ) -> SourceDefinition:
        template = parse_templates({"runtime": configuration})["runtime"]
        connection = self._connections.get(template.connection_ref)
        if connection is None:
            raise Forbidden("Source access is not authorized")
        source = SourceDefinition(
            source_id,
            version,
            actor.tenant_id,
            "databricks-sql",
            "1",
            canonical_json(
                {
                    "contract": RUNTIME_CONTRACT,
                    "template": configuration,
                    "template_digest": template.digest(connection),
                    "serialization_version": SERIALIZATION_VERSION,
                }
            ),
            limits,
            allowed_actor_ids=tuple(sorted(allowed_actor_ids)),
        )
        self.authorize(actor, source)
        return source

    def _check(self, source: SourceDefinition, actor_id: str, *, management: bool) -> None:
        template = validate_source(source, self.templates, self._connections)
        for grant in self._grants():
            approved = parse_templates({"grant": grant["template"]})["grant"]
            bounds = ExtractionLimits.parse(grant["limits"])
            if (
                grant["tenant_id"] == source.tenant_id
                and actor_id in grant["manager_ids" if management else "actor_ids"]
                and template.connection_ref == approved.connection_ref
                and template.table[: len(grant.get("table_scope", approved.table))]
                == tuple(grant.get("table_scope", approved.table))
                and set(template.projection) <= set(approved.projection)
                and all(p in template.predicates for p in approved.predicates)
                and source.allowed_actor_ids
                and set(source.allowed_actor_ids) <= set(grant["actor_ids"])
                and source.limits.max_bytes <= bounds.max_bytes
                and source.limits.max_records <= bounds.max_records
                and source.limits.max_line_bytes <= bounds.max_line_bytes
                and source.limits.timeout_seconds <= bounds.timeout_seconds
            ):
                return
        raise Forbidden("Source access is not authorized")

    def authorize(self, actor: Actor, source: SourceDefinition) -> None:
        if source.tenant_id != actor.tenant_id or source.adapter_type != "databricks-sql":
            raise Forbidden("Source access is not authorized")
        self._check(source, actor.actor_id, management=True)

    def execution(self, source: SourceDefinition, actor_id: str) -> None:
        # Legacy sources retain their existing operator-approved template contract.
        if source.adapter_type == "databricks-sql" and "contract" in json.loads(
            source.configuration_json
        ):
            self._check(source, actor_id, management=False)
