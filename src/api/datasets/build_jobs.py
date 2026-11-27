"""Builder discovery and asynchronous immutable DatasetVersion creation."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import Field

from src.api.collection_schemas import (
    COLLECTION_RESPONSES,
    CollectionError,
    CollectionQuery,
    MetadataPage,
)
from src.api.datasets.schemas import DatasetKey
from src.api.ml_http import ActorDependency, get_services
from src.api.ml_schemas import StrictModel
from src.application.datasets.build_jobs import DatasetBuildService
from src.application.ml_services import MLServices
from src.domain.datasets.build_jobs import (
    MAX_INPUT_BYTES,
    MAX_INPUT_RECORDS,
    MAX_INPUT_SNAPSHOTS,
    BuildState,
    DatasetBuildJob,
    DatasetBuildSpec,
)
from src.domain.datasets.ebm import EBM_DATASET_SCHEMA, EBM_FEATURE_SHAPE
from src.domain.datasets.ebm_builder import (
    EBM_DATASET_BUILDER_ID,
    EBM_DATASET_BUILDER_VERSION,
    EBM_RAW_INPUT_SCHEMA,
    EBMDatasetBuilderConfig,
    EBMRawSnapshotReference,
)
from src.domain.ml import Conflict, NotFound, Unavailable

Key128 = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JobId = Annotated[str, Path(min_length=1, max_length=64)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": CollectionError, "description": "Unknown or inaccessible resource."}
}
UNPUBLISHED: dict[int | str, dict[str, Any]] = {
    409: {"model": CollectionError, "description": "Job has no successfully published dataset."}
}


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    tags=["dataset builds"], responses=COLLECTION_RESPONSES, dependencies=[Depends(_private)]
)


def _service(services: Annotated[MLServices, Depends(get_services)]) -> DatasetBuildService:
    if services.dataset_builds is None:
        raise Unavailable("Dataset build management is unavailable")
    return services.dataset_builds


BuildService = Annotated[DatasetBuildService, Depends(_service)]


class SnapshotInput(StrictModel):
    role: Literal["fdc", "wafer"]
    snapshot_id: Key128
    manifest_digest: Digest


class BuilderConfiguration(StrictModel):
    recipe_id: Key128
    segment_id: int = Field(ge=1, le=2**31 - 1)
    split_seed: int = Field(default=43, ge=0, le=2**31 - 1)
    log1p: bool = True


class BuildRequest(StrictModel):
    builder_id: Literal["ebm-dataset-builder"] = EBM_DATASET_BUILDER_ID
    builder_version: Literal["1.0.0"] = EBM_DATASET_BUILDER_VERSION
    dataset_id: DatasetKey
    version: DatasetKey
    inputs: list[SnapshotInput] = Field(min_length=2, max_length=MAX_INPUT_SNAPSHOTS)
    configuration: BuilderConfiguration

    def spec(self) -> DatasetBuildSpec:
        return DatasetBuildSpec(
            self.builder_id,
            self.builder_version,
            self.dataset_id,
            self.version,
            tuple(EBMRawSnapshotReference(**item.model_dump()) for item in self.inputs),
            EBMDatasetBuilderConfig(**self.configuration.model_dump()),
        )


class BuilderSummary(StrictModel):
    builder_id: str = EBM_DATASET_BUILDER_ID
    builder_version: str = EBM_DATASET_BUILDER_VERSION
    input_schema_version: str = EBM_RAW_INPUT_SCHEMA
    output_schema_version: str = EBM_DATASET_SCHEMA


class BuilderDetail(BuilderSummary):
    input_roles: list[Literal["fdc", "wafer"]]
    feature_shape: list[int]
    max_input_snapshots: int
    max_input_bytes: int
    max_input_records: int
    request_schema: dict[str, Any]
    submit_example: BuildRequest


class BuildJobPageQuery(CollectionQuery):
    state: BuildState | None = None


class BuildJobView(StrictModel):
    job_id: str
    dataset_id: str
    version: str
    state: BuildState
    spec_digest: Digest
    created_at: float = Field(description="Creation time in UTC Unix seconds.")
    updated_at: float = Field(description="Last durable update in UTC Unix seconds.")
    deadline: float = Field(description="Job deadline in UTC Unix seconds.")
    attempt_count: int
    error_code: str | None
    status_url: str
    result_url: str


class BuildResult(StrictModel):
    job_id: str
    dataset_id: str
    version: str
    content_digest: Digest
    receipt_digest: Digest
    dataset_url: str


def _public(job: DatasetBuildJob, request: Request) -> BuildJobView:
    return BuildJobView(
        job_id=job.job_id,
        dataset_id=job.dataset_id,
        version=job.version,
        state=job.state,
        spec_digest=job.spec_digest,
        created_at=job.created_at,
        updated_at=job.updated_at,
        deadline=job.deadline,
        attempt_count=job.attempt_count,
        error_code=job.error_code,
        status_url=request.url_for("dataset_build_status", job_id=job.job_id).path,
        result_url=request.url_for("dataset_build_result", job_id=job.job_id).path,
    )


@router.get(
    "/builders",
    response_model=list[BuilderSummary],
    operation_id="list_dataset_builders",
    summary="List approved dataset builders",
    description="Requires ml:dataset:manage. Lists the built-in EBM DatasetBuilder and "
    "its input and output schema. Use builder detail before submitting a job.",
)
def builders(actor: ActorDependency) -> list[BuilderSummary]:
    actor.require("ml:dataset:manage")
    return [BuilderSummary()]


@router.get(
    "/builders/{builder_id}/versions/{version}",
    response_model=BuilderDetail,
    operation_id="get_dataset_builder_version",
    responses=NOT_FOUND,
    summary="Read builder input, configuration and output contracts",
    description="Requires ml:dataset:manage. Returns confirmed input roles, aggregate limits, "
    "configuration schema and an illustrative request. Replace the snapshot IDs and digests "
    "with visible published snapshots before submitting.",
)
def builder(builder_id: Key128, version: DatasetKey, actor: ActorDependency) -> BuilderDetail:
    actor.require("ml:dataset:manage")
    if (builder_id, version) != (EBM_DATASET_BUILDER_ID, EBM_DATASET_BUILDER_VERSION):
        raise NotFound("DatasetBuilder version not found")
    return BuilderDetail(
        input_roles=["fdc", "wafer"],
        feature_shape=list(EBM_FEATURE_SHAPE),
        max_input_snapshots=MAX_INPUT_SNAPSHOTS,
        max_input_bytes=MAX_INPUT_BYTES,
        max_input_records=MAX_INPUT_RECORDS,
        request_schema=BuildRequest.model_json_schema(),
        submit_example=BuildRequest(
            dataset_id="example-ebm",
            version="v1",
            inputs=[
                SnapshotInput(role="fdc", snapshot_id="example-fdc", manifest_digest="a" * 64),
                SnapshotInput(role="wafer", snapshot_id="example-wafer", manifest_digest="b" * 64),
            ],
            configuration=BuilderConfiguration(recipe_id="recipe-a", segment_id=1),
        ),
    )


@router.post(
    "/build-jobs",
    response_model=BuildJobView,
    status_code=202,
    operation_id="submit_dataset_build",
    summary="Accept a durable dataset build job",
    responses={
        **NOT_FOUND,
        409: {
            "model": CollectionError,
            "description": "The key conflicts with stored intent or an input manifest differs.",
        },
        429: {"model": CollectionError, "description": "Tenant pending build budget exhausted."},
    },
    description="Requires ml:dataset:manage and ingestion read access to every published "
    "snapshot. Accepts metadata only. The worker rechecks access and verifies payloads "
    "before building. Replay the same intent and key to obtain the existing job. Poll "
    "status_url and read result_url after SUCCEEDED.",
)
def submit(
    body: BuildRequest,
    request: Request,
    actor: ActorDependency,
    service: BuildService,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> BuildJobView:
    return _public(service.submit(actor, key, body.spec()), request)


@router.get(
    "/build-jobs",
    response_model=MetadataPage[BuildJobView],
    operation_id="list_dataset_build_jobs",
    summary="List visible dataset build jobs",
    description="Lists jobs for the authenticated owner or ml:jobs:read_all within the tenant. "
    "Filter by exact state and exclusive UTC creation bounds. Sort by creation time and job ID "
    "descending. Default 25/max 100.",
)
def list_jobs(
    request: Request,
    actor: ActorDependency,
    service: BuildService,
    query: Annotated[BuildJobPageQuery, Query()],
) -> MetadataPage[BuildJobView]:
    rows, next_offset = service.page(actor, query.window(), query.state)
    return MetadataPage[BuildJobView](
        items=[_public(row, request) for row in rows],
        limit=query.limit,
        offset=query.offset,
        next_offset=next_offset,
    )


@router.get(
    "/build-jobs/{job_id}",
    response_model=BuildJobView,
    name="dataset_build_status",
    operation_id="get_dataset_build_job",
    responses=NOT_FOUND,
    summary="Read durable dataset build state",
    description="The owner or same-tenant ml:jobs:read_all can read status. QUEUED includes "
    "retry wait. FAILED and SUCCEEDED are terminal.",
)
def status(
    job_id: JobId, request: Request, actor: ActorDependency, service: BuildService
) -> BuildJobView:
    return _public(service.get(actor, job_id), request)


@router.get(
    "/build-jobs/{job_id}/result",
    response_model=BuildResult,
    name="dataset_build_result",
    operation_id="get_dataset_build_result",
    responses={**NOT_FOUND, **UNPUBLISHED},
    summary="Read a successful build's immutable dataset identity",
    description="Uses job ownership rules. Returns only the published dataset and receipt "
    "digests. Use dataset_url for metadata and its lineage route with their existing grants.",
)
def result(
    job_id: JobId, request: Request, actor: ActorDependency, service: BuildService
) -> BuildResult:
    job = service.get(actor, job_id)
    if job.state != "SUCCEEDED":
        raise Conflict("Dataset build has no published result")
    if job.content_digest is None or job.receipt_digest is None:
        raise Unavailable("Dataset build result metadata is unavailable")
    return BuildResult(
        job_id=job_id,
        dataset_id=job.dataset_id,
        version=job.version,
        content_digest=job.content_digest,
        receipt_digest=job.receipt_digest,
        dataset_url=request.url_for(
            "version_metadata", dataset_id=job.dataset_id, version=job.version
        ).path,
    )
