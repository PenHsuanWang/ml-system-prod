"""Submit, inspect and cancel jobs without framework or storage dependencies."""

import json

from src.application.training.ports import RecipeResolver, TrainingRepository
from src.domain.ml import Actor, digest, MLError, NotFound
from src.domain.training.models import Attempt, Job, TERMINAL


class TrainingService:
    def __init__(self, repository: TrainingRepository, recipes: RecipeResolver) -> None:
        self.repository = repository
        self.recipes = recipes

    def submit(self, actor: Actor, key: str, intent_json: str) -> Job:
        actor.require("ml:train")
        if not key or len(key.encode()) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise MLError("Idempotency-Key must be 1..128 visible ASCII characters")
        fingerprint = digest(["ml-json-v1", intent_json])
        existing = self.repository.find_submission(actor, key, fingerprint)
        if existing is not None:
            return self.get(actor, existing.job_id)
        origin = json.loads(intent_json).get("retry_of_job_id")
        if origin is not None and self.get(actor, origin).state not in TERMINAL:
            from src.domain.ml import Conflict
            raise Conflict("Only terminal jobs may be retried as a new job")
        spec_json = self.recipes.resolve(intent_json, actor)
        return self.repository.submit(actor, key, fingerprint, spec_json,
                                      digest(["training-spec-v1", spec_json]))

    def get(self, actor: Actor, job_id: str) -> Job:
        job = self.repository.get(job_id)
        if job.tenant_id != actor.tenant_id or (
            job.owner_id != actor.actor_id and "ml:jobs:read_all" not in actor.permissions
        ):
            raise NotFound("Job not found")
        return job

    def attempts(self, actor: Actor, job_id: str) -> tuple[Attempt, ...]:
        self.get(actor, job_id)
        return self.repository.attempts(job_id)

    def cancel(self, actor: Actor, job_id: str) -> Job:
        self.get(actor, job_id)
        actor.require("ml:cancel")
        return self.repository.cancel(job_id, actor)
