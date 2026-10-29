from src.application.model_registry.ports import RegistryRepository
from src.domain.ml import Actor, Conflict, MLError
from src.domain.model_registry.models import Approval, Evaluation, ModelBinding


class ModelRegistryService:
    def __init__(self, repository: RegistryRepository) -> None:
        self.repository = repository

    def get(self, actor: Actor, binding_id: str) -> ModelBinding:
        actor.require("ml:model:read")
        return self.repository.get_model(actor, binding_id)

    def list(self, actor: Actor) -> tuple[ModelBinding, ...]:
        actor.require("ml:model:read")
        return self.repository.list_models(actor)

    def evaluations(self, actor: Actor, binding_id: str) -> tuple[Evaluation, ...]:
        self.get(actor, binding_id)
        return self.repository.list_evaluations(binding_id)

    def approve(self, actor: Actor, binding_id: str, evaluation_id: str,
                environment: str) -> Approval:
        actor.require("ml:model:approve")
        if environment not in {"dev", "staging", "prod"}:
            raise MLError("Unknown model environment")
        binding = self.repository.get_model(actor, binding_id)
        evaluation = self.repository.get_evaluation(evaluation_id)
        if (evaluation.model_binding_id != binding.binding_id or
            evaluation.manifest_digest != binding.manifest_digest or
                evaluation.decision != "APPROVED_BY_POLICY"):
            raise Conflict("Model has no matching approved evaluation")
        return self.repository.approve(actor, binding, evaluation, environment)
