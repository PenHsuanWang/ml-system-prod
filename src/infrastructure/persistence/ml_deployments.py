"""An authoritative transactional router revision rejects late activation commands."""

from dataclasses import asdict
from uuid import uuid4
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, insert, select, update

from src.domain.deployments.models import Deployment, Route
from src.domain.ml import Actor, canonical_json, Conflict, digest, NotFound, CapacityExceeded
from src.infrastructure.persistence.ml_database import approvals, Database, deployments, evaluations, models, routes
from src.infrastructure.persistence.ml_training import lock_tenant, record_audit


def deployment_from_row(row: Mapping[str, Any]) -> Deployment:
    values = dict(row)
    values["deployment_id"] = values.pop("id")
    return Deployment(**values)


class SQLDeploymentRepository:
    def __init__(self, database: Database, *, serving_image: str = "development") -> None:
        self.db = database
        self.serving_image = serving_image

    def create(self, actor: Actor, endpoint: str, binding_id: str,
               approval_id: str, environment: str) -> Deployment:
        with self.db.transaction() as c:
            lock_tenant(c, "__ml_runtime_capacity__")
            lock_tenant(c, actor.tenant_id)
            pending = c.scalar(select(func.count()).select_from(deployments).where(
                deployments.c.tenant_id == actor.tenant_id, deployments.c.endpoint_id == endpoint,
                deployments.c.state.not_in(["ACTIVE", "FAILED", "RETIRED"])))
            if pending:
                raise Conflict("Endpoint already has a pending rollout or retirement")
            reserved = c.scalar(select(func.count()).select_from(deployments).where(
                deployments.c.resources_released.is_(False)))
            if reserved >= 2:
                raise CapacityExceeded("Reference CPU profile has no free rollout capacity")
            binding = c.execute(select(models).where(models.c.id == binding_id,
                                                     models.c.tenant_id == actor.tenant_id)).mappings().first()
            approval = c.execute(select(approvals).where(approvals.c.id == approval_id,
                                                         approvals.c.tenant_id == actor.tenant_id)).mappings().first()
            if not binding or not approval:
                raise NotFound("Deployment evidence not found")
            evaluation = c.execute(select(evaluations).where(
                evaluations.c.id == approval["evaluation_id"])).mappings().one()
            if (approval["model_binding_id"] != binding_id or
                approval["environment"] != environment or
                approval["binding_digest"] != binding["binding_digest"] or
                approval["report_digest"] != evaluation["report_digest"] or
                evaluation["model_binding_id"] != binding_id or
                evaluation["manifest_digest"] != binding["manifest_digest"] or
                    evaluation["decision"] != "APPROVED_BY_POLICY"):
                raise Conflict("Approval does not authorize this deployment")
            row = c.execute(select(routes).where(routes.c.tenant_id == actor.tenant_id,
                                                 routes.c.endpoint_id == endpoint).with_for_update()).mappings().first()
            if row is None:
                c.execute(insert(routes).values(tenant_id=actor.tenant_id, endpoint_id=endpoint,
                          revision=0, generation=0))
                revision = 0
            else:
                revision = row["revision"]
            generation = 1 + (c.scalar(select(func.max(deployments.c.generation)).where(
                deployments.c.tenant_id == actor.tenant_id,
                deployments.c.endpoint_id == endpoint)) or 0)
            deployment_id = str(uuid4())
            spec = {"schema_version": "1", "deployment_id": deployment_id,
                    "tenant_id": actor.tenant_id, "endpoint_id": endpoint,
                    "generation": generation, "approval_id": approval_id,
                    "expected_route_revision": revision,
                    "evaluation_id": approval["evaluation_id"],
                    "model_binding_id": binding_id, "manifest_digest": binding["manifest_digest"],
                    "artifact_ref": binding["artifact_ref"], "model_version": binding["version"],
                    "registered_name": binding["registered_name"],
                    "runtime_image_digest": self.serving_image}
            value = Deployment(deployment_id, actor.tenant_id, endpoint, generation,
                               binding_id, approval_id, "REQUESTED", revision, canonical_json(spec), digest(spec))
            values = asdict(value)
            values["id"] = values.pop("deployment_id")
            c.execute(insert(deployments).values(**values))
            record_audit(c, actor, "deployment.create", deployment_id)
            return value

    def get(self, actor: Actor, deployment_id: str) -> Deployment:
        with self.db.transaction() as c:
            row = c.execute(select(deployments).where(deployments.c.id == deployment_id,
                                                      deployments.c.tenant_id == actor.tenant_id)).mappings().first()
            if row is None:
                raise NotFound("Deployment not found")
            return deployment_from_row(row)

    def transition(self, deployment_id: str, expected: str, target: str) -> None:
        with self.db.transaction() as c:
            result = c.execute(update(deployments).where(deployments.c.id == deployment_id,
                                                         deployments.c.state == expected).values(state=target))
            if result.rowcount != 1:
                raise Conflict("Stale deployment state")

    def route(self, tenant: str, endpoint: str) -> Route:
        with self.db.transaction() as c:
            row = c.execute(select(routes).where(routes.c.tenant_id == tenant,
                                                 routes.c.endpoint_id == endpoint)).mappings().first()
            if row is None:
                raise NotFound("Endpoint not found")
            return Route(**dict(row))

    def activate(self, deployment: Deployment) -> Route:
        with self.db.transaction() as c:
            row = c.execute(select(deployments).where(
                deployments.c.id == deployment.deployment_id).with_for_update()).mappings().one()
            if row["state"] not in {"READY", "ACTIVATING", "RECONCILING"}:
                raise Conflict("Runtime has not passed readiness")
            result = c.execute(update(routes).where(routes.c.tenant_id == deployment.tenant_id,
                                                    routes.c.endpoint_id == deployment.endpoint_id,
                                                    routes.c.revision == deployment.expected_route_revision).values(
                revision=routes.c.revision+1, deployment_id=deployment.deployment_id,
                generation=deployment.generation))
            if result.rowcount != 1:
                raise Conflict("Routing revision has been fenced")
            c.execute(update(deployments).where(deployments.c.tenant_id == deployment.tenant_id,
                                                deployments.c.endpoint_id == deployment.endpoint_id,
                                                deployments.c.state == "ACTIVE").values(state="RETIRING"))
            c.execute(update(deployments).where(deployments.c.id == deployment.deployment_id)
                      .values(state="ACTIVE"))
        return self.route(deployment.tenant_id, deployment.endpoint_id)

    def fence(self, deployment: Deployment) -> bool:
        with self.db.transaction() as c:
            current = c.execute(select(deployments).where(deployments.c.id == deployment.deployment_id)
                                .with_for_update()).mappings().one()
            route = c.execute(select(routes).where(routes.c.tenant_id == deployment.tenant_id,
                                                   routes.c.endpoint_id == deployment.endpoint_id).with_for_update()).mappings().one()
            if route["deployment_id"] == deployment.deployment_id:
                if current["state"] in {"FAILED", "RETIRED"}:
                    raise Conflict("Terminal deployment has an inconsistent route")
                c.execute(update(deployments).where(deployments.c.id == deployment.deployment_id)
                          .values(state="ACTIVE"))
                return False
            # Even when the model stays unchanged, increment the authority revision.
            # Any previously emitted CAS expecting the old revision is now invalid.
            if current["state"] in {"FAILED", "RETIRING", "RETIRED"}:
                return True
            if route["revision"] == current["expected_route_revision"]:
                changed = c.execute(update(routes).where(routes.c.tenant_id == deployment.tenant_id,
                                                         routes.c.endpoint_id == deployment.endpoint_id,
                                                         routes.c.revision == route["revision"]).values(revision=routes.c.revision+1))
                if changed.rowcount != 1:
                    raise Conflict("Route changed while fencing; reconciliation is required")
            c.execute(update(deployments).where(deployments.c.id == deployment.deployment_id,
                                                deployments.c.state != "ACTIVE").values(state="FAILED"))
            return True

    def release(self, deployment: Deployment) -> None:
        with self.db.transaction() as c:
            c.execute(select(deployments).where(deployments.c.id == deployment.deployment_id)
                      .with_for_update()).mappings().one()
            route = c.execute(select(routes).where(routes.c.tenant_id == deployment.tenant_id,
                                                   routes.c.endpoint_id == deployment.endpoint_id).with_for_update()).mappings().one()
            if route["deployment_id"] == deployment.deployment_id:
                raise Conflict("An active routed deployment cannot release capacity")
            c.execute(update(deployments).where(deployments.c.id == deployment.deployment_id,
                                                deployments.c.state.in_(["FAILED", "RETIRING", "RETIRED"]))
                      .values(resources_released=True))
