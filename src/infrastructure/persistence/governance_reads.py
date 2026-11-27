"""Bounded, tenant-scoped read projections over the existing ML tables."""

from typing import Any

from sqlalchemy import Select, and_, func, select

from src.domain.ml import Actor, NotFound
from src.domain.pagination import CollectionWindow
from src.infrastructure.persistence.ml_database import (
    Database,
    approvals,
    attempts,
    audit,
    deployments,
    evaluations,
    jobs,
    models,
    routes,
)
from src.infrastructure.persistence.prediction_contract_schema import (
    public_contracts,
    usage_reviews,
)

ReadRow = dict[str, object]


class SQLGovernanceReader:
    def __init__(self, database: Database) -> None:
        self.db = database

    def _first(self, statement: Select[Any]) -> ReadRow | None:
        with self.db.transaction() as connection:
            row = connection.execute(statement.limit(1)).mappings().first()
            return dict(row) if row is not None else None

    def _page(self, statement: Select[Any], window: CollectionWindow) -> list[ReadRow]:
        with self.db.transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    statement.limit(window.limit + 1).offset(window.offset)
                ).mappings()
            ]

    def bindings(
        self, actor: Actor, window: CollectionWindow, name: str | None, version: str | None
    ) -> list[ReadRow]:
        statement = select(
            models.c.id.label("binding_id"),
            models.c.registered_name,
            models.c.version.label("model_version"),
            models.c.manifest_digest,
            models.c.binding_digest,
        ).where(models.c.tenant_id == actor.tenant_id)
        if name is not None:
            statement = statement.where(models.c.registered_name == name)
        if version is not None:
            statement = statement.where(models.c.version == version)
        return self._page(
            statement.order_by(models.c.registered_name, models.c.version, models.c.id), window
        )

    def count_versions(self, actor: Actor, name: str) -> int:
        with self.db.transaction() as connection:
            return int(
                connection.scalar(
                    select(func.count())
                    .select_from(models)
                    .where(models.c.tenant_id == actor.tenant_id, models.c.registered_name == name)
                )
                or 0
            )

    def contract(self, actor: Actor, binding_id: str) -> ReadRow | None:
        return self._first(
            select(
                public_contracts.c.manifest_digest,
                public_contracts.c.contract_digest,
                public_contracts.c.document_json,
                usage_reviews.c.review_json,
                usage_reviews.c.revision.label("usage_revision"),
                usage_reviews.c.review_digest,
            )
            .select_from(
                public_contracts.outerjoin(
                    usage_reviews,
                    and_(
                        usage_reviews.c.binding_id == public_contracts.c.binding_id,
                        usage_reviews.c.tenant_id == public_contracts.c.tenant_id,
                    ),
                )
            )
            .where(
                public_contracts.c.tenant_id == actor.tenant_id,
                public_contracts.c.binding_id == binding_id,
            )
            .order_by(usage_reviews.c.revision.desc())
        )

    def training_origin(self, actor: Actor, binding_id: str) -> ReadRow | None:
        statement = (
            select(
                jobs.c.id.label("job_id"),
                jobs.c.spec_json,
                jobs.c.spec_digest,
                attempts.c.number.label("attempt_number"),
            )
            .select_from(
                models.join(attempts, attempts.c.id == models.c.attempt_id).join(
                    jobs,
                    and_(jobs.c.id == attempts.c.job_id, jobs.c.tenant_id == models.c.tenant_id),
                )
            )
            .where(models.c.tenant_id == actor.tenant_id, models.c.id == binding_id)
        )
        if "ml:jobs:read_all" not in actor.permissions:
            statement = statement.where(jobs.c.owner_id == actor.actor_id)
        return self._first(statement)

    def evaluations(self, actor: Actor, binding_id: str, window: CollectionWindow) -> list[ReadRow]:
        statement = (
            select(
                evaluations.c.id.label("evaluation_id"),
                evaluations.c.model_binding_id,
                evaluations.c.manifest_digest,
                evaluations.c.dataset_digest,
                evaluations.c.policy_version,
                evaluations.c.evaluator_version,
                evaluations.c.decision,
                evaluations.c.metrics_json,
                evaluations.c.report_digest,
                evaluations.c.created_at,
            )
            .select_from(
                models.join(
                    evaluations,
                    and_(
                        evaluations.c.model_binding_id == models.c.id,
                        evaluations.c.manifest_digest == models.c.manifest_digest,
                    ),
                )
            )
            .where(
                models.c.tenant_id == actor.tenant_id,
                models.c.id == binding_id,
            )
            .order_by(evaluations.c.created_at.desc(), evaluations.c.id.desc())
        )
        return self._page(statement, window)

    def approvals(self, actor: Actor, binding_id: str, window: CollectionWindow) -> list[ReadRow]:
        statement = (
            select(
                approvals.c.id.label("approval_id"),
                approvals.c.model_binding_id,
                approvals.c.evaluation_id,
                approvals.c.report_digest,
                approvals.c.binding_digest,
                approvals.c.environment,
                approvals.c.created_at,
            )
            .select_from(
                models.join(
                    approvals,
                    and_(
                        approvals.c.tenant_id == models.c.tenant_id,
                        approvals.c.model_binding_id == models.c.id,
                        approvals.c.binding_digest == models.c.binding_digest,
                    ),
                ).join(
                    evaluations,
                    and_(
                        evaluations.c.id == approvals.c.evaluation_id,
                        evaluations.c.model_binding_id == models.c.id,
                        evaluations.c.manifest_digest == models.c.manifest_digest,
                        evaluations.c.report_digest == approvals.c.report_digest,
                        evaluations.c.decision == "APPROVED_BY_POLICY",
                    ),
                )
            )
            .where(
                models.c.tenant_id == actor.tenant_id,
                models.c.id == binding_id,
            )
            .order_by(approvals.c.created_at.desc(), approvals.c.id.desc())
        )
        return self._page(statement, window)

    def deployments(
        self,
        actor: Actor,
        window: CollectionWindow,
        endpoint_id: str | None,
        state: str | None,
        environment: str | None,
    ) -> list[ReadRow]:
        statement = (
            select(
                deployments.c.id.label("deployment_id"),
                deployments.c.endpoint_id,
                deployments.c.generation,
                deployments.c.state,
                deployments.c.model_binding_id,
                deployments.c.approval_id,
                deployments.c.resources_released,
                deployments.c.expected_route_revision,
                deployments.c.spec_digest,
                routes.c.revision.label("route_revision"),
                and_(
                    routes.c.deployment_id == deployments.c.id,
                    routes.c.generation == deployments.c.generation,
                    deployments.c.state == "ACTIVE",
                    deployments.c.resources_released.is_(False),
                )
                .is_(True)
                .label("routed"),
                models.c.registered_name,
                models.c.version.label("model_version"),
                models.c.manifest_digest,
                approvals.c.environment,
            )
            .select_from(
                deployments.join(
                    routes,
                    and_(
                        routes.c.tenant_id == deployments.c.tenant_id,
                        routes.c.endpoint_id == deployments.c.endpoint_id,
                    ),
                )
                .join(
                    models,
                    and_(
                        models.c.tenant_id == deployments.c.tenant_id,
                        models.c.id == deployments.c.model_binding_id,
                    ),
                )
                .join(
                    approvals,
                    and_(
                        approvals.c.tenant_id == deployments.c.tenant_id,
                        approvals.c.id == deployments.c.approval_id,
                        approvals.c.model_binding_id == models.c.id,
                    ),
                )
            )
            .where(deployments.c.tenant_id == actor.tenant_id)
        )
        if endpoint_id is not None:
            statement = statement.where(deployments.c.endpoint_id == endpoint_id)
        if state is not None:
            statement = statement.where(deployments.c.state == state)
        if environment is not None:
            statement = statement.where(approvals.c.environment == environment)
        return self._page(
            statement.order_by(deployments.c.endpoint_id, deployments.c.generation.desc()), window
        )

    @staticmethod
    def _routes(actor: Actor) -> Select[Any]:
        return (
            select(
                routes.c.endpoint_id,
                routes.c.revision.label("route_revision"),
                routes.c.deployment_id.label("assigned_id"),
                deployments.c.id.label("deployment_id"),
                deployments.c.generation,
                models.c.id.label("model_binding_id"),
                models.c.registered_name,
                models.c.version.label("model_version"),
                models.c.manifest_digest,
                approvals.c.id.label("approval_id"),
                approvals.c.environment,
            )
            .select_from(
                routes.outerjoin(
                    deployments.join(
                        models,
                        and_(
                            models.c.tenant_id == deployments.c.tenant_id,
                            models.c.id == deployments.c.model_binding_id,
                        ),
                    ).join(
                        approvals,
                        and_(
                            approvals.c.tenant_id == deployments.c.tenant_id,
                            approvals.c.id == deployments.c.approval_id,
                            approvals.c.model_binding_id == models.c.id,
                        ),
                    ),
                    and_(
                        routes.c.tenant_id == deployments.c.tenant_id,
                        routes.c.endpoint_id == deployments.c.endpoint_id,
                        routes.c.generation == deployments.c.generation,
                        routes.c.deployment_id == deployments.c.id,
                        deployments.c.state == "ACTIVE",
                        deployments.c.resources_released.is_(False),
                    ),
                )
            )
            .where(routes.c.tenant_id == actor.tenant_id)
        )

    @staticmethod
    def _route_status(row: ReadRow) -> ReadRow:
        assigned_id = row.pop("assigned_id")
        status = "unassigned"
        if assigned_id is not None:
            status = "active" if row["deployment_id"] is not None else "inconsistent"
        return {**row, "route_state": status, "readiness": "unknown"}

    def endpoints(self, actor: Actor, window: CollectionWindow) -> list[ReadRow]:
        return [
            self._route_status(row)
            for row in self._page(self._routes(actor).order_by(routes.c.endpoint_id), window)
        ]

    def endpoint(self, actor: Actor, endpoint_id: str) -> ReadRow:
        row = self._first(self._routes(actor).where(routes.c.endpoint_id == endpoint_id))
        if row is None:
            raise NotFound("Endpoint not found")
        return self._route_status(row)

    def events(self, actor: Actor, deployment_id: str, window: CollectionWindow) -> list[ReadRow]:
        statement = (
            select(
                audit.c.id.label("event_id"),
                audit.c.subject_id.label("deployment_id"),
                audit.c.action,
                audit.c.created_at,
            )
            .where(
                audit.c.tenant_id == actor.tenant_id,
                audit.c.subject_id == deployment_id,
                audit.c.action == "deployment.create",
            )
            .order_by(audit.c.created_at, audit.c.id)
        )
        return self._page(statement, window)
