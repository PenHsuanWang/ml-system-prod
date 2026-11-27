"""Typed Management read contracts for model and deployment governance."""

from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.api.collection_schemas import COLLECTION_RESPONSES, CollectionError, MetadataPage
from src.api.ml_http import ActorDependency, get_services
from src.api.ml_schemas import StrictModel
from src.api.prediction_catalog import PublicTechnicalContract
from src.application.governance import GovernanceDiscoveryService, ReadRow
from src.application.ml_services import MLServices
from src.domain.ml import Unavailable
from src.domain.pagination import MAX_COLLECTION_OFFSET, CollectionWindow

ResourceId = Annotated[str, Path(min_length=1, max_length=64)]
ModelName = Annotated[str, Path(min_length=1, max_length=256)]
EndpointId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Environment = Literal["dev", "staging", "prod"]
DeploymentState = Literal[
    "REQUESTED",
    "VALIDATING",
    "STARTING",
    "WARMING",
    "READY",
    "ACTIVATING",
    "RECONCILING",
    "ACTIVE",
    "RETIRING",
    "RETIRED",
    "FAILED",
]
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "Unknown or inaccessible resource.", "model": CollectionError}
}
T = TypeVar("T", bound=BaseModel)


def private_read(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


registry_router = APIRouter(
    tags=["model discovery"], responses=COLLECTION_RESPONSES, dependencies=[Depends(private_read)]
)
deployment_router = APIRouter(
    tags=["deployment discovery"],
    responses=COLLECTION_RESPONSES,
    dependencies=[Depends(private_read)],
)


def discovery(services: Annotated[MLServices, Depends(get_services)]) -> GovernanceDiscoveryService:
    if services.governance is None:
        raise Unavailable("Governance discovery is not configured")
    return services.governance


Discovery = Annotated[GovernanceDiscoveryService, Depends(discovery)]


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=MAX_COLLECTION_OFFSET)

    def window(self) -> CollectionWindow:
        return CollectionWindow(limit=self.limit, offset=self.offset)


class BindingQuery(PageQuery):
    registered_name: str | None = Field(default=None, min_length=1, max_length=256)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)


class DeploymentQuery(PageQuery):
    endpoint_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    state: DeploymentState | None = None
    environment: Environment | None = None


def page(model: type[T], rows: list[ReadRow], window: CollectionWindow) -> MetadataPage[T]:
    return MetadataPage[T](
        items=[model.model_validate(row) for row in rows[: window.limit]],
        limit=window.limit,
        offset=window.offset,
        next_offset=window.next_offset(has_more=len(rows) > window.limit),
    )


class BindingSummary(StrictModel):
    binding_id: str
    registered_name: str
    model_version: str
    manifest_digest: str
    binding_digest: str


class ModelFamily(StrictModel):
    registered_name: str
    version_count: int


class ModelContract(StrictModel):
    binding_id: str
    manifest_digest: str
    status: Literal["complete", "incomplete"]
    contract: PublicTechnicalContract | None
    contract_digest: str | None
    usage: dict[str, str] | None
    usage_revision: int | None


class TrainingOrigin(StrictModel):
    job_id: str
    attempt_id: str
    attempt_number: int
    spec_digest: str
    recipe_id: str
    recipe_version: str


class DatasetOrigin(StrictModel):
    id: str
    version: str
    content_digest: str


class ModelLineage(StrictModel):
    binding_id: str
    manifest_digest: str
    run_id: str
    status: Literal["complete", "incomplete"]
    training: TrainingOrigin | None
    dataset: DatasetOrigin | None


class EvaluationRecord(StrictModel):
    evaluation_id: str
    model_binding_id: str
    manifest_digest: str
    dataset_digest: str
    policy_version: str
    evaluator_version: str
    decision: str
    metrics: dict[str, float | int | bool | None]
    report_digest: str
    created_at: float = Field(description="Time recorded in SQL, in UTC Unix seconds.")


class ApprovalRecord(StrictModel):
    approval_id: str
    model_binding_id: str
    evaluation_id: str
    report_digest: str
    binding_digest: str
    environment: Environment
    created_at: float = Field(description="Time recorded in SQL, in UTC Unix seconds.")


class DeploymentRecord(StrictModel):
    deployment_id: str
    endpoint_id: str
    generation: int
    state: DeploymentState
    model_binding_id: str
    approval_id: str
    resources_released: bool
    expected_route_revision: int
    route_revision: int = Field(description="Current route revision at read time.")
    spec_digest: str
    routed: bool
    registered_name: str
    model_version: str
    manifest_digest: str
    environment: Environment


class EndpointRecord(StrictModel):
    endpoint_id: str
    route_revision: int
    route_state: Literal["active", "unassigned", "inconsistent"]
    readiness: Literal["unknown"]
    deployment_id: str | None
    generation: int | None
    model_binding_id: str | None
    registered_name: str | None
    model_version: str | None
    manifest_digest: str | None
    approval_id: str | None
    environment: Environment | None


class DeploymentEvent(StrictModel):
    event_id: str
    deployment_id: str
    action: Literal["deployment.create"]
    created_at: float = Field(description="Stored audit event time in UTC Unix seconds.")


@registry_router.get(
    "/bindings",
    response_model=MetadataPage[BindingSummary],
    operation_id="list_model_bindings",
    summary="List model bindings",
    description="Requires ml:model:read. Lists tenant SQL bindings ordered by registered name, "
    "model version and binding ID. Optional exact registered_name and model_version filters.",
)
def bindings(
    actor: ActorDependency, service: Discovery, query: Annotated[BindingQuery, Query()]
) -> MetadataPage[BindingSummary]:
    window = query.window()
    return page(
        BindingSummary,
        service.bindings(actor, window, query.registered_name, query.model_version),
        window,
    )


@registry_router.get(
    "/models/{name}",
    response_model=ModelFamily,
    responses=NOT_FOUND,
    operation_id="get_model_family",
    summary="Read model family metadata",
    description="Requires ml:model:read. Returns the count of fixed versions visible in this "
    "tenant. Use the bindings list with registered_name to browse individual versions.",
)
def family(name: ModelName, actor: ActorDependency, service: Discovery) -> ModelFamily:
    return ModelFamily.model_validate(service.family(actor, name))


@registry_router.get(
    "/bindings/{binding_id}/contract",
    response_model=ModelContract,
    responses=NOT_FOUND,
    operation_id="get_model_binding_contract",
    summary="Read a published inference contract",
    description="Requires ml:model:read. Verifies the stored binding and manifest digests. "
    "Incomplete contracts or reviews are indicated explicitly.",
)
def model_contract(
    binding_id: ResourceId, actor: ActorDependency, service: Discovery
) -> ModelContract:
    row = service.contract(actor, binding_id)
    try:
        return ModelContract.model_validate(row)
    except ValidationError:
        return ModelContract.model_validate(
            {
                **row,
                "status": "incomplete",
                "contract": None,
                "contract_digest": None,
            }
        )


@registry_router.get(
    "/bindings/{binding_id}/lineage",
    response_model=ModelLineage,
    responses=NOT_FOUND,
    operation_id="get_model_binding_lineage",
    summary="Trace a model binding to training",
    description="Requires ml:model:read. Training lineage also requires job ownership or "
    "ml:jobs:read_all, and dataset identity requires ml:train or ml:dataset:read. "
    "Missing or inaccessible lineage is incomplete.",
)
def lineage(binding_id: ResourceId, actor: ActorDependency, service: Discovery) -> ModelLineage:
    return ModelLineage.model_validate(service.lineage(actor, binding_id))


@registry_router.get(
    "/bindings/{binding_id}/evaluation-results",
    response_model=MetadataPage[EvaluationRecord],
    responses=NOT_FOUND,
    operation_id="list_model_evaluation_results",
    summary="List recorded model evaluation results",
    description="Requires ml:model:read. Pages immutable evaluation evidence and approved "
    "public metric fields, newest SQL record first.",
)
def evaluation_results(
    binding_id: ResourceId,
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[PageQuery, Query()],
) -> MetadataPage[EvaluationRecord]:
    window = query.window()
    return page(EvaluationRecord, service.evaluations(actor, binding_id, window), window)


@registry_router.get(
    "/bindings/{binding_id}/approvals",
    response_model=MetadataPage[ApprovalRecord],
    responses=NOT_FOUND,
    operation_id="list_model_approvals",
    summary="List model approvals",
    description="Requires ml:model:read. Returns persisted promotion approvals with their "
    "pinned evaluation and environment, newest SQL record first.",
)
def approvals(
    binding_id: ResourceId,
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[PageQuery, Query()],
) -> MetadataPage[ApprovalRecord]:
    window = query.window()
    return page(ApprovalRecord, service.approvals(actor, binding_id, window), window)


@deployment_router.get(
    "",
    response_model=MetadataPage[DeploymentRecord],
    operation_id="list_deployments",
    summary="List deployment generations",
    description="Requires ml:deployment:read or ml:deploy. Filter by endpoint, state or "
    "approval environment. Ordered by endpoint and descending generation.",
)
def deployments(
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[DeploymentQuery, Query()],
) -> MetadataPage[DeploymentRecord]:
    window = query.window()
    rows = service.deployments(actor, window, query.endpoint_id, query.state, query.environment)
    return page(DeploymentRecord, rows, window)


@deployment_router.get(
    "/endpoints",
    response_model=MetadataPage[EndpointRecord],
    operation_id="list_deployment_endpoints",
    summary="List endpoint route assignments",
    description="Requires ml:deployment:read or ml:deploy. Returns tenant SQL assignments "
    "ordered by endpoint ID. Readiness is unknown because this API does not probe serving.",
)
def endpoints(
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[PageQuery, Query()],
) -> MetadataPage[EndpointRecord]:
    window = query.window()
    return page(EndpointRecord, service.endpoints(actor, window), window)


@deployment_router.get(
    "/endpoints/{endpoint_id}",
    response_model=EndpointRecord,
    responses=NOT_FOUND,
    operation_id="get_deployment_endpoint",
    summary="Read current endpoint assignment",
    description="Requires ml:deployment:read or ml:deploy. Returns the current persisted SQL "
    "route assignment and its revision, with unknown runtime readiness.",
)
def endpoint(endpoint_id: EndpointId, actor: ActorDependency, service: Discovery) -> EndpointRecord:
    return EndpointRecord.model_validate(service.endpoint(actor, endpoint_id))


@deployment_router.get(
    "/endpoints/{endpoint_id}/history",
    response_model=MetadataPage[DeploymentRecord],
    responses=NOT_FOUND,
    operation_id="list_endpoint_deployment_history",
    summary="List recorded endpoint generations",
    description="Requires ml:deployment:read or ml:deploy. Pages persisted generations newest "
    "first. State and route revision are observations at read time.",
)
def history(
    endpoint_id: EndpointId,
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[PageQuery, Query()],
) -> MetadataPage[DeploymentRecord]:
    window = query.window()
    return page(DeploymentRecord, service.history(actor, endpoint_id, window), window)


@deployment_router.get(
    "/{deployment_id}/events",
    response_model=MetadataPage[DeploymentEvent],
    responses=NOT_FOUND,
    operation_id="list_deployment_events",
    summary="Read persisted deployment audit events",
    description="Requires ml:deployment:read or ml:deploy. Returns recorded creation events "
    "in ascending audit-time order. Controller state transitions currently lack audit events.",
)
def events(
    deployment_id: ResourceId,
    actor: ActorDependency,
    service: Discovery,
    query: Annotated[PageQuery, Query()],
) -> MetadataPage[DeploymentEvent]:
    window = query.window()
    return page(DeploymentEvent, service.events(actor, deployment_id, window), window)
