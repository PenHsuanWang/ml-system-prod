import re

from src.application.deployments.ports import DeploymentExecutor, DeploymentRepository
from src.domain.deployments.models import Deployment
from src.domain.ml import Actor, Conflict, MLError


def require_deployment_read(actor: Actor) -> None:
    if "ml:deploy" not in actor.permissions:
        actor.require("ml:deployment:read")


class DeploymentService:
    def __init__(self, repository: DeploymentRepository, environment: str) -> None:
        self.repository = repository
        self.environment = environment

    def create(self, actor: Actor, endpoint: str, binding_id: str,
               approval_id: str) -> Deployment:
        actor.require("ml:deploy")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", endpoint):
            raise MLError("Invalid endpoint identifier")
        return self.repository.create(actor, endpoint, binding_id, approval_id, self.environment)

    def get(self, actor: Actor, deployment_id: str) -> Deployment:
        require_deployment_read(actor)
        return self.repository.get(actor, deployment_id)

    def rollback(self, actor: Actor, deployment_id: str) -> Deployment:
        actor.require("ml:deploy")
        previous = self.get(actor, deployment_id)
        return self.create(actor, previous.endpoint_id, previous.model_binding_id,
                           previous.approval_id)


class DeploymentController:
    def __init__(self, repository: DeploymentRepository, executor: DeploymentExecutor) -> None:
        self.repository = repository
        self.executor = executor

    def reconcile(self, deployment: Deployment) -> None:
        state = deployment.state
        if state == "ACTIVE":
            route = self.repository.route(deployment.tenant_id, deployment.endpoint_id)
            if not deployment.resources_released and (route.deployment_id, route.generation) == (deployment.deployment_id, deployment.generation):
                # Restore the approved physical instance without another activation/CAS.
                # A stale ACTIVE snapshot must never recreate a superseded deployment.
                self.executor.prepare(deployment)
                self.executor.ready(deployment)
            return
        if state in {"FAILED", "RETIRING"}:
            if self.executor.retire(deployment):
                self.repository.release(deployment)
                if state == "RETIRING":
                    self.repository.transition(deployment.deployment_id, "RETIRING", "RETIRED")
            return
        if state == "REQUESTED":
            self.repository.transition(deployment.deployment_id, "REQUESTED", "VALIDATING")
            state = "VALIDATING"
        if state == "VALIDATING":
            self.repository.transition(deployment.deployment_id, "VALIDATING", "STARTING")
            state = "STARTING"
        if state in {"STARTING", "WARMING", "READY", "ACTIVATING", "RECONCILING"}:
            self.executor.prepare(deployment)
        if not self.executor.ready(deployment):
            return
        if state == "STARTING":
            self.repository.transition(deployment.deployment_id, "STARTING", "WARMING")
            state = "WARMING"
        if state == "WARMING":
            self.repository.transition(deployment.deployment_id, "WARMING", "READY")
        try:
            self.repository.activate(deployment)
        except Conflict:
            # Seal the router revision before marking failure or destroying a runtime.
            if self.repository.fence(deployment):
                if self.executor.retire(deployment):
                    self.repository.release(deployment)
            return
