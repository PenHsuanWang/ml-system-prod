"""Submit, inspect and cancel jobs without framework or storage dependencies."""

import json
import math

from src.application.training.ports import (
    RecipeResolver,
    TrainingDiscoveryRepository,
    TrainingRepository,
)
from src.domain.ml import Actor, MLError, NotFound, Unavailable, digest
from src.domain.pagination import CollectionWindow
from src.domain.training.models import TERMINAL, Attempt, Job, JobState
from src.domain.training.workloads import (
    APPROVED_WORKLOADS,
    EBM_WORKLOAD,
    WorkloadContract,
    workload_for_spec,
)

MLP_METRICS = frozenset({"loss", "validation_accuracy"})
EBM_METRICS = frozenset(
    {"loss", "cdf_anchor_loss", "lot_mean_corr_loss", "wafer_rank_loss", "reconstruction_loss"}
)


def _verified_spec(job: Job) -> dict[str, object]:
    try:
        value = json.loads(job.spec_json)
        if (
            not isinstance(value, dict)
            or digest(["training-spec-v1", job.spec_json]) != job.spec_digest
        ):
            raise ValueError
    except (ValueError, MLError):
        raise Unavailable("Stored training specification is unavailable") from None
    return value


class TrainingService:
    def __init__(
        self,
        repository: TrainingRepository,
        recipes: RecipeResolver,
        *,
        discovery: TrainingDiscoveryRepository | None = None,
    ) -> None:
        self.repository = repository
        self.recipes = recipes
        self.discovery = discovery

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
        return self.repository.submit(
            actor, key, fingerprint, spec_json, digest(["training-spec-v1", spec_json])
        )

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

    def approved_recipes(self, actor: Actor) -> tuple[tuple[str, str, WorkloadContract], ...]:
        actor.require("ml:train")
        return tuple(
            (recipe_id, version, workload)
            for (recipe_id, version), workload in sorted(APPROVED_WORKLOADS.items())
        )

    def approved_recipe(
        self, actor: Actor, recipe_id: str, version: str
    ) -> tuple[WorkloadContract, str]:
        actor.require("ml:train")
        workload = APPROVED_WORKLOADS.get((recipe_id, version))
        if workload is None:
            raise NotFound("Training recipe version not found")
        return workload, self.recipes.evaluation_policy_version(workload)

    def _discovery(self) -> TrainingDiscoveryRepository:
        if self.discovery is None:
            raise Unavailable("Training job discovery is not configured")
        return self.discovery

    def list_jobs(
        self,
        actor: Actor,
        window: CollectionWindow,
        state: JobState | None,
    ) -> tuple[list[Job], int | None]:
        rows = self._discovery().list_visible(actor, window, state)
        return rows[: window.limit], window.next_offset(has_more=len(rows) > window.limit)

    def job_spec(self, actor: Actor, job_id: str) -> dict[str, object]:
        job = self.get(actor, job_id)
        value = _verified_spec(job)
        dataset = value.get("dataset")
        if not isinstance(dataset, dict):
            raise Unavailable("Stored training specification is unavailable")
        return {
            **{
                name: value.get(name)
                for name in (
                    "schema_version",
                    "recipe",
                    "model",
                    "training",
                    "resource_profile_id",
                    "evaluation_policy",
                    "feature_schema_version",
                    "output_contract_version",
                    "retry_of_job_id",
                )
            },
            "job_id": job.job_id,
            "spec_digest": job.spec_digest,
            "dataset": {name: dataset.get(name) for name in ("id", "version", "content_digest")},
        }

    def job_metrics(
        self,
        actor: Actor,
        job_id: str,
    ) -> tuple[str, list[tuple[int, dict[str, float]]]]:
        job = self.get(actor, job_id)
        evidence = self._discovery().attempt_result(job.job_id, job.attempt_id)
        if evidence is None:
            return job.attempt_id, []
        spec = _verified_spec(job)
        dataset = spec.get("dataset")
        samples: list[tuple[int, dict[str, float]]] = []
        try:
            names = EBM_METRICS if workload_for_spec(spec) == EBM_WORKLOAD else MLP_METRICS
            if not isinstance(dataset, dict) or len(evidence.encode()) > 512 * 1024:
                raise ValueError
            result = json.loads(evidence)
            if (
                not isinstance(result, dict)
                or result["dataset_digest"] != dataset["content_digest"]
            ):
                raise ValueError
            metrics = result["metrics"]
            if not isinstance(metrics, list) or len(metrics) > 1000:
                raise ValueError
            previous_step = -1
            for sample in metrics:
                step, pairs = sample["step"], sample["values"]
                if type(step) is not int or not previous_step < step <= 1000:
                    raise ValueError
                if not isinstance(pairs, list) or len(pairs) > 32:
                    raise ValueError
                values: dict[str, float] = {}
                for name, number in pairs:
                    if name not in names:
                        continue
                    if (
                        name in values
                        or type(number) not in (int, float)
                        or not math.isfinite(number)
                    ):
                        raise ValueError
                    values[name] = float(number)
                if values:
                    samples.append((step, values))
                previous_step = step
        except (KeyError, TypeError, ValueError, OverflowError, MLError):
            raise Unavailable("Stored training metrics are unavailable") from None
        return job.attempt_id, samples
