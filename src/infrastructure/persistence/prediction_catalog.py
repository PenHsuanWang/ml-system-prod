"""Bounded single-statement catalog reads from authoritative active deployment routes."""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, func, select

from src.domain.deployments.identity import deployment_spec
from src.domain.ml import Actor, Conflict, MLError, NotFound, Unavailable, digest
from src.domain.prediction.catalog import PublicModelContract, contract_document, reviewed_usage
from src.infrastructure.persistence.ml_database import Database, deployments, models, routes
from src.infrastructure.persistence.ml_deployments import deployment_from_row
from src.infrastructure.persistence.prediction_contract_schema import (
    public_contracts,
    usage_reviews,
)


class SQLPredictionCatalog:
    def __init__(self, database: Database) -> None:
        self.db = database

    def _query(self, actor: Actor) -> Any:
        latest = (
            select(func.max(usage_reviews.c.revision))
            .where(
                usage_reviews.c.binding_id == models.c.id,
                usage_reviews.c.tenant_id == actor.tenant_id,
            )
            .correlate(models)
            .scalar_subquery()
        )
        return (
            select(
                *deployments.c,
                routes.c.revision.label("route_revision"),
                models.c.registered_name,
                models.c.version.label("registered_version"),
                models.c.manifest_digest.label("binding_manifest"),
                public_contracts.c.document_json,
                public_contracts.c.contract_digest,
                public_contracts.c.manifest_digest.label("contract_manifest"),
                usage_reviews.c.review_json,
                usage_reviews.c.revision.label("review_revision"),
                usage_reviews.c.review_digest,
            )
            .select_from(
                routes.join(
                    deployments,
                    and_(
                        routes.c.deployment_id == deployments.c.id,
                        routes.c.tenant_id == deployments.c.tenant_id,
                        routes.c.endpoint_id == deployments.c.endpoint_id,
                        routes.c.generation == deployments.c.generation,
                    ),
                )
                .join(
                    models,
                    and_(
                        deployments.c.model_binding_id == models.c.id,
                        models.c.tenant_id == routes.c.tenant_id,
                    ),
                )
                .outerjoin(
                    public_contracts,
                    and_(
                        public_contracts.c.binding_id == models.c.id,
                        public_contracts.c.tenant_id == routes.c.tenant_id,
                    ),
                )
                .outerjoin(
                    usage_reviews,
                    and_(
                        usage_reviews.c.binding_id == models.c.id,
                        usage_reviews.c.tenant_id == routes.c.tenant_id,
                        usage_reviews.c.revision == latest,
                    ),
                )
            )
            .where(
                routes.c.tenant_id == actor.tenant_id,
                deployments.c.state == "ACTIVE",
                deployments.c.resources_released.is_(False),
            )
        )

    def _public(self, row: Mapping[str, Any], *, detailed: bool) -> dict[str, Any]:
        deployment = deployment_from_row({c.key: row[c.key] for c in deployments.c})
        try:
            spec = deployment_spec(deployment)
            if (
                spec["registered_name"] != row["registered_name"]
                or spec["model_version"] != row["registered_version"]
                or spec["manifest_digest"] != row["binding_manifest"]
                or spec["model_binding_id"] != deployment.model_binding_id
            ):
                raise Conflict("Model identity mismatch")
        except (Conflict, KeyError, ValueError):
            raise Unavailable("Endpoint identity is unavailable") from None

        technical = None
        usage = None
        try:
            if row["document_json"] is not None:
                contract = PublicModelContract(
                    deployment.model_binding_id, row["contract_manifest"], row["document_json"]
                )
                if (
                    contract.contract_digest == row["contract_digest"]
                    and contract.manifest_digest == row["binding_manifest"]
                ):
                    technical = contract_document(contract.document_json)
            if row["review_json"] is not None:
                review = json.loads(row["review_json"])
                if (
                    reviewed_usage(review) == row["review_json"]
                    and digest(
                        [deployment.model_binding_id, row["review_revision"], row["review_json"]]
                    )
                    == row["review_digest"]
                ):
                    usage = review
        except (ValueError, TypeError, KeyError, MLError):
            technical, usage = None, None

        result = {
            "endpoint_id": deployment.endpoint_id,
            "display_name": usage["display_name"] if usage else deployment.endpoint_id,
            "purpose": usage["purpose"] if usage else None,
            "registered_model": row["registered_name"],
            "model_version": row["registered_version"],
            "generation": deployment.generation,
            "route_revision": row["route_revision"],
            "route_state": "active",
            "readiness": "unknown",
            "contract_status": "complete" if technical and usage else "incomplete",
            "input_schema_version": technical["input_schema_version"] if technical else None,
            "detail_url": f"/api/v1/prediction/endpoints/{deployment.endpoint_id}",
        }
        if detailed:
            result.update(
                {
                    "deployment_id": deployment.deployment_id,
                    "model_binding_id": deployment.model_binding_id,
                    "request_example": None,
                    "manifest_digest": row["binding_manifest"],
                    "contract_digest": row["contract_digest"] if technical else None,
                    "usage_revision": row["review_revision"] if usage else None,
                    "usage": usage,
                    "contract": technical,
                    "predict_url": "/api/v1/prediction/predict",
                }
            )
            if technical:
                # Keep the persisted technical document byte-equivalent for digest verification.
                result["request_example"] = {
                    **technical["request_example"],
                    "endpoint_id": deployment.endpoint_id,
                }

        return result

    def page(self, actor: Actor, after: str, limit: int) -> list[dict[str, Any]]:
        actor.require("ml:predict")
        if not 1 <= limit <= 101:
            raise ValueError("Invalid endpoint scan budget")
        with self.db.transaction() as c:
            rows = c.execute(
                self._query(actor)
                .where(routes.c.endpoint_id > after)
                .order_by(routes.c.endpoint_id)
                .limit(limit)
            ).mappings()
            return [self._public(row, detailed=False) for row in rows]

    def detail(self, actor: Actor, endpoint_id: str) -> dict[str, Any]:
        actor.require("ml:predict")
        with self.db.transaction() as c:
            row = (
                c.execute(self._query(actor).where(routes.c.endpoint_id == endpoint_id).limit(1))
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound("Endpoint not found")
            return self._public(row, detailed=True)
