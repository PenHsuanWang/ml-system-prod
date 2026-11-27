"""Read-only training catalog and job evidence on the Management API."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Response
from pydantic import Field, ValidationError

from src.api.collection_schemas import (
    COLLECTION_RESPONSES,
    CollectionError,
    CollectionQuery,
    MetadataPage,
)
from src.api.ml_http import ActorDependency, ServicesDependency
from src.api.ml_schemas import (
    EBMHyperparameters,
    EBMModelIntent,
    EBMTrainingIntent,
    Hyperparameters,
    ModelIntent,
    StrictModel,
    SubmitRequest,
    TrainingIntent,
    VersionRef,
)
from src.domain.datasets.ebm import EBM_DATASET_SCHEMA, EBM_FEATURE_SHAPE
from src.domain.ml import Unavailable
from src.domain.training.models import JobState
from src.domain.training.workloads import EBM_WORKLOAD, RecipeAdmission, WorkloadContract

router = APIRouter(tags=["training discovery"], responses=COLLECTION_RESPONSES)
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown or inaccessible resource.", "model": CollectionError}
}
JobId = Annotated[str, Path(min_length=1, max_length=64)]


class RecipeSummary(StrictModel):
    recipe: VersionRef
    architecture: str
    input_schema_version: str
    dataset_schema_version: str | None
    resource_profile_id: str
    artifact_profile: str


class RecipeDetail(RecipeSummary):
    dataset_requirements: str
    feature_shape: list[int]
    evaluator_version: str
    evaluation_policy: str = Field(description="Policy version selected for new submissions.")
    evaluation_policy_configurable: bool
    output_schema_version: str
    spec_schema: dict[str, Any] = Field(description="JSON Schema for the POST /training/jobs spec.")
    submit_example: SubmitRequest = Field(
        description="Use a compatible registered dataset version."
    )


class ResourceProfile(StrictModel):
    resource_profile_id: str
    device: Literal["cpu"] = "cpu"
    recipes: list[VersionRef]


class JobPageQuery(CollectionQuery):
    state: JobState | None = None


class JobSummary(StrictModel):
    job_id: str
    state: str
    phase: str
    revision: int
    attempt_id: str
    spec_digest: str
    created_at: float = Field(description="Creation time in Unix seconds.")
    deadline: float = Field(description="Job deadline in Unix seconds.")
    model_binding_id: str | None
    error_code: str | None


class DatasetSpecRef(VersionRef):
    content_digest: str


class JobSpec(StrictModel):
    job_id: str
    spec_digest: str = Field(description="Identity of the full immutable execution specification.")
    schema_version: str
    recipe: VersionRef
    dataset: DatasetSpecRef
    model: ModelIntent | EBMModelIntent
    training: Hyperparameters | EBMHyperparameters
    resource_profile_id: str
    evaluation_policy: str
    feature_schema_version: str
    output_contract_version: str
    retry_of_job_id: str | None


class MetricSample(StrictModel):
    step: int = Field(description="Zero-based training epoch index from the recorded attempt.")
    values: dict[str, float]


class JobMetrics(StrictModel):
    job_id: str
    attempt_id: str
    status: Literal["recorded", "unknown"]
    samples: list[MetricSample]


def recipe_summary(
    recipe_id: str, version: str, workload: WorkloadContract, admission: RecipeAdmission
) -> RecipeSummary:
    return RecipeSummary(
        recipe=VersionRef(id=recipe_id, version=version),
        architecture=admission.architecture,
        input_schema_version=workload.input_schema_version,
        dataset_schema_version=EBM_DATASET_SCHEMA if workload == EBM_WORKLOAD else None,
        resource_profile_id=admission.resource_profile_id,
        artifact_profile=workload.artifact_profile,
    )


@router.get(
    "/recipes",
    response_model=list[RecipeSummary],
    operation_id="list_training_recipes",
    summary="List approved training recipes",
    description="Requires ml:train. Returns the built-in recipe versions in ID/version order. "
    "Use recipe detail and the dataset catalog to select a compatible immutable dataset.",
)
def recipes(actor: ActorDependency, services: ServicesDependency) -> list[RecipeSummary]:
    return [recipe_summary(*recipe) for recipe in services.training.approved_recipes(actor)]


@router.get(
    "/recipes/{recipe_id}/versions/{version}",
    operation_id="get_training_recipe_version",
    response_model=RecipeDetail,
    responses=NOT_FOUND,
    summary="Read a training recipe contract",
    description="Requires ml:train. Returns the versioned input and artifact contracts, "
    "model/hyperparameter constraints and a synthetic request for POST /training/jobs. "
    "Resolve the dataset ID/version in this tenant before submitting.",
)
def recipe_version(
    recipe_id: str, version: str, actor: ActorDependency, services: ServicesDependency
) -> RecipeDetail:
    workload, admission = services.training.approved_recipe(actor, recipe_id, version)
    is_ebm = workload == EBM_WORKLOAD
    intent_model = EBMTrainingIntent if is_ebm else TrainingIntent
    summary = recipe_summary(recipe_id, version, workload, admission)
    example = SubmitRequest.model_validate(
        {
            "spec": {
                "recipe": summary.recipe.model_dump(),
                "dataset": {"id": "fdc", "version": "v1"}
                if is_ebm
                else {"id": "dataset-demo", "version": "2026-10-28-v1"},
            },
        }
    )
    spec_schema = intent_model.model_json_schema()
    spec_schema["properties"]["recipe"]["const"] = summary.recipe.model_dump()
    return RecipeDetail(
        **summary.model_dump(),
        dataset_requirements=(
            "Approved EBM replay dataset in signal, pad, frequency, time axis order."
            if is_ebm
            else "Approved non-EBM dataset with feature schema 1 (sensor_a, sensor_b)."
        ),
        feature_shape=list(EBM_FEATURE_SHAPE) if is_ebm else [2],
        evaluator_version=workload.evaluator_version,
        evaluation_policy=admission.evaluation_policy,
        evaluation_policy_configurable=is_ebm,
        output_schema_version=workload.output_schema_version,
        spec_schema=spec_schema,
        submit_example=example,
    )


@router.get(
    "/resource-profiles",
    response_model=list[ResourceProfile],
    operation_id="list_training_resource_profiles",
    summary="List approved training resource profiles",
    description="Requires ml:train. Lists CPU profiles and their compatible recipe versions. "
    "Role settings enforce the actual CPU, memory and lifecycle limits; this is not live capacity.",
)
def resource_profiles(
    actor: ActorDependency, services: ServicesDependency
) -> list[ResourceProfile]:
    profiles: dict[str, list[VersionRef]] = {}
    for recipe_id, version, _, admission in services.training.approved_recipes(actor):
        profiles.setdefault(admission.resource_profile_id, []).append(
            VersionRef(id=recipe_id, version=version)
        )
    return [
        ResourceProfile(resource_profile_id=key, recipes=profiles[key]) for key in sorted(profiles)
    ]


@router.get(
    "/jobs",
    response_model=MetadataPage[JobSummary],
    operation_id="list_training_jobs",
    summary="List visible training jobs",
    description="Authenticated owners can list their jobs. ml:jobs:read_all includes all jobs in "
    "the current tenant. Optional state and exclusive Unix-time bounds filter jobs. Ordered by "
    "creation time descending, then job ID descending. Use spec and metrics for details.",
)
def list_jobs(
    actor: ActorDependency,
    services: ServicesDependency,
    response: Response,
    query: Annotated[JobPageQuery, Query()],
) -> MetadataPage[JobSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    rows, continuation = services.training.list_jobs(actor, query.window(), query.state)
    return MetadataPage[JobSummary](
        items=[JobSummary.model_validate(row, from_attributes=True) for row in rows],
        limit=query.limit,
        offset=query.offset,
        next_offset=continuation,
    )


@router.get(
    "/jobs/{job_id}/spec",
    response_model=JobSpec,
    operation_id="get_training_job_spec",
    summary="Read a job's immutable training inputs",
    responses=NOT_FOUND,
    description="Uses job owner visibility or same-tenant ml:jobs:read_all. Returns the dataset "
    "content digest, resolved recipe, model settings and spec identity.",
)
def job_spec(
    job_id: JobId, actor: ActorDependency, services: ServicesDependency, response: Response
) -> JobSpec:
    response.headers["Cache-Control"] = "private, no-store"
    data = services.training.job_spec(actor, job_id)
    try:
        return JobSpec.model_validate(data)
    except ValidationError:
        raise Unavailable("Stored training specification is unavailable") from None


@router.get(
    "/jobs/{job_id}/metrics",
    response_model=JobMetrics,
    operation_id="get_training_job_metrics",
    summary="Read persisted training metrics",
    responses=NOT_FOUND,
    description="Uses job owner visibility or same-tenant ml:jobs:read_all. Returns finite metrics "
    "from the recorded output for the observed current attempt, in epoch order (up to 1,000). "
    "Before checkpointed output exists, status is unknown and samples is empty.",
)
def job_metrics(
    job_id: JobId, actor: ActorDependency, services: ServicesDependency, response: Response
) -> JobMetrics:
    response.headers["Cache-Control"] = "private, no-store"
    attempt_id, samples = services.training.job_metrics(actor, job_id)
    return JobMetrics(
        job_id=job_id,
        attempt_id=attempt_id,
        status="recorded" if samples else "unknown",
        samples=[MetricSample(step=step, values=values) for step, values in samples],
    )
