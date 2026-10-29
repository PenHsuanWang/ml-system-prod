from uuid import uuid4

import pytest

from src.domain.ml import Conflict, digest
from src.domain.model_registry.models import Evaluation, ModelBinding
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


def evidence(components, actor, intent, key="model"):
    job = components.services.training.submit(actor, key, intent)
    binding = components.registry.save_binding(ModelBinding(str(uuid4()), actor.tenant_id,
                                                            job.attempt_id, "tenant-nn", str(uuid4()), "run", "/trusted/bundle", "a"*64, digest(key)))
    evaluation = components.registry.save_evaluation(Evaluation(str(uuid4()), binding.binding_id,
                                                                binding.manifest_digest, "data", "policy-v1", "evaluator-v1", "APPROVED_BY_POLICY",
                                                                '{"accuracy":1}', digest([key, "report"])))
    approval = components.services.registry.approve(
        actor, binding.binding_id, evaluation.evaluation_id, "dev")
    return binding, evaluation, approval


def ready(components, deployment):
    components.deployments.transition(deployment.deployment_id, "REQUESTED", "READY")


def test_approval_pins_exact_evidence(components, actor, intent):
    binding, evaluation, approval = evidence(components, actor, intent)
    other, _, _ = evidence(components, actor, intent, "other")
    with pytest.raises(Conflict):
        components.services.registry.approve(
            actor, other.binding_id, evaluation.evaluation_id, "dev")
    with pytest.raises(Conflict):
        components.services.deployments.create(
            actor, "quality", other.binding_id, approval.approval_id)
    assert approval.report_digest == evaluation.report_digest


def test_fenced_unknown_activation_cannot_arrive_late(components, actor, intent):
    binding, _, approval = evidence(components, actor, intent)
    candidate = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    ready(components, candidate)
    assert components.deployments.fence(candidate)
    with pytest.raises(Conflict):
        components.deployments.activate(candidate)
    route = components.deployments.route(actor.tenant_id, "quality")
    assert route.deployment_id is None
    assert route.revision > candidate.expected_route_revision
    from sqlalchemy import update
    from src.infrastructure.persistence.ml_database import routes
    with components.database.transaction() as connection:
        delayed = connection.execute(update(routes).where(routes.c.tenant_id == actor.tenant_id,
                                                          routes.c.endpoint_id == "quality", routes.c.revision == candidate.expected_route_revision)
                                     .values(deployment_id=candidate.deployment_id))
        assert delayed.rowcount == 0


def test_failed_rollout_preserves_active_and_rollback_is_new_generation(components, actor, intent):
    binding, _, approval = evidence(components, actor, intent)
    first = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    ready(components, first)
    components.deployments.activate(first)
    bad = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    ready(components, bad)
    components.deployments.fence(bad)
    # No runtime was launched by this repository-level test; its slot can close.
    components.deployments.release(bad)
    assert components.deployments.route(
        actor.tenant_id, "quality").deployment_id == first.deployment_id
    rollback = components.services.deployments.rollback(actor, first.deployment_id)
    assert rollback.generation > bad.generation
    assert rollback.deployment_id != first.deployment_id
    ready(components, rollback)
    assert components.deployments.activate(rollback).deployment_id == rollback.deployment_id


def test_complete_vs_cancel_has_one_terminal_winner(components, actor, intent):
    binding, evaluation, _ = evidence(components, actor, intent)
    item = SQLOutboxRepository(components.database).lease(1)[0]
    claim = components.training.claim(item.command)
    components.services.training.cancel(actor, claim.job.job_id)
    with pytest.raises(Conflict):
        components.training.finish(claim, binding.binding_id, evaluation.evaluation_id)
    components.training.stop(claim, "CANCELLED", True)
    assert components.training.get(claim.job.job_id).state == "CANCELLED"


@pytest.mark.parametrize("interrupted_state", ["VALIDATING", "STARTING", "WARMING", "READY"])
def test_controller_resumes_each_interrupted_rollout_phase(components, actor, intent, interrupted_state):
    from src.application.deployments.service import DeploymentController
    binding, _, approval = evidence(components, actor, intent)
    candidate = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    components.deployments.transition(candidate.deployment_id, "REQUESTED", interrupted_state)

    class ReadyExecutor:
        def prepare(self, deployment):
            pass

        def ready(self, deployment):
            return True

        def retire(self, deployment):
            raise AssertionError("A healthy candidate must stay running")

    observed = components.deployments.get(actor, candidate.deployment_id)
    DeploymentController(components.deployments, ReadyExecutor()).reconcile(observed)
    assert components.deployments.route(
        actor.tenant_id, "quality").deployment_id == candidate.deployment_id
    assert components.deployments.get(actor, candidate.deployment_id).state == "ACTIVE"


def test_failed_runtime_holds_capacity_until_stopped(components, actor, intent):
    from src.application.deployments.service import DeploymentController
    from src.domain.ml import CapacityExceeded
    binding, _, approval = evidence(components, actor, intent)
    candidate = components.services.deployments.create(
        actor, "quality", binding.binding_id, approval.approval_id)
    components.deployments.fence(candidate)
    failed = components.deployments.get(actor, candidate.deployment_id)
    components.services.deployments.create(
        actor, "second", binding.binding_id, approval.approval_id)

    class StoppingExecutor:
        stopped = False

        def retire(self, deployment):
            return self.stopped

    executor = StoppingExecutor()
    controller = DeploymentController(components.deployments, executor)
    controller.reconcile(failed)
    with pytest.raises(CapacityExceeded):
        components.services.deployments.create(
            actor, "third", binding.binding_id, approval.approval_id)
    executor.stopped = True
    controller.reconcile(failed)
    assert components.deployments.get(actor, candidate.deployment_id).resources_released
    assert components.services.deployments.create(
        actor, "third", binding.binding_id, approval.approval_id).state == "REQUESTED"
