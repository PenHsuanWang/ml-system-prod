"""Tenant-authorized endpoint discovery with bounded SQL pages and safe public contracts."""

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.ml_http import ActorDependency
from src.api.prediction_docs import PredictionError
from src.application.prediction.catalog import EndpointDiscoveryService
from src.domain.ml import Unavailable

router = APIRouter(tags=["prediction endpoint discovery"])


class EndpointCacheControl:
    """Apply private no-store headers to discovery success and handled errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_private(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "private, no-store"
            await send(message)

        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        private = scope["type"] == "http" and path.startswith("/api/v1/prediction/endpoints")
        await self.app(scope, receive, send_private if private else send)


class EndpointPageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)


class AuthenticationErrorBody(BaseModel):
    detail: str


class EndpointSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    endpoint_id: str
    display_name: str
    purpose: str | None
    registered_model: str
    model_version: str = Field(
        description="Fixed registered version currently assigned to the route."
    )
    generation: int
    route_revision: int = Field(
        description="Authoritative SQL routing revision observed by this query."
    )
    route_state: Literal["active"]
    readiness: Literal["unknown"] = Field(
        default="unknown", description="Discovery makes no live runtime health observation."
    )
    contract_status: Literal["complete", "incomplete"] = Field(
        description="Complete requires verified technical metadata and reviewed usage."
    )
    input_schema_version: str | None
    detail_url: str = Field(description="Origin-relative detail path including ASGI root_path.")


class EndpointPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    observed_at: float = Field(description="UTC Unix time of this page's SQL observation.")
    items: list[EndpointSummary]
    next_cursor: str | None


class PublicTechnicalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contract_version: Literal["prediction-contract-v1"]
    input_schema_version: str
    output_schema_version: str
    input_schema: dict[str, Any] = Field(description="JSON Schema for this model's request DTO.")
    output_schema: dict[str, Any] = Field(description="JSON Schema for this model's response DTO.")
    request_example: dict[str, Any] = Field(
        description="Immutable synthetic template; use the detail's top-level executable example."
    )
    input_units: dict[str, str | None] = Field(description="Unknown physical units are null.")
    limits: dict[str, int]
    preprocessing: dict[str, Any]
    output_semantics: dict[str, Any]
    registry_backend: Literal["local", "mlflow"]


class EndpointDetail(EndpointSummary):
    observed_at: float
    deployment_id: str
    model_binding_id: str
    request_example: dict[str, Any] | None
    manifest_digest: str
    contract_digest: str | None
    usage_revision: int | None
    usage: dict[str, str] | None
    contract: PublicTechnicalContract | None
    predict_url: str = Field(description="Origin-relative predict path including ASGI root_path.")


def discovery(request: Request) -> EndpointDiscoveryService:
    service = getattr(request.app.state, "endpoint_discovery", None)
    if service is None:
        raise Unavailable("Endpoint discovery is not configured")
    return cast(EndpointDiscoveryService, service)


DiscoveryDependency = Annotated[EndpointDiscoveryService, Depends(discovery)]


@router.get(
    "/endpoints",
    response_model=EndpointPage,
    operation_id="list_prediction_endpoints",
    summary="List models assigned to your prediction endpoints",
    description=(
        "Requires Bearer authentication and ml:predict. Lists active SQL routes for your tenant, "
        "ordered by endpoint_id. Default 25, maximum 100. next_cursor expires after 15 minutes "
        "and is bound to your tenant, account and page limit. Reauthorization occurs on each page. "
        "Concurrent deployment changes can affect later pages. An empty items array is valid. "
        "readiness=unknown describes the absence of a live health observation; prediction checks "
        "runtime readiness. Legacy endpoints may have incomplete contracts. Open detail_url for "
        "the current model version, input contract and a synthetic prediction example."
    ),
    responses={
        401: {"description": "Sign in again.", "model": PredictionError | AuthenticationErrorBody},
        403: {"description": "ml:predict is required.", "model": PredictionError},
        422: {
            "description": "Invalid limit/cursor. Restart from the first page.",
            "model": PredictionError,
        },
        503: {
            "description": "Identity, storage or endpoint identity is unavailable.",
            "model": PredictionError,
        },
    },
)
def endpoints(
    request: Request,
    actor: ActorDependency,
    service: DiscoveryDependency,
    response: Response,
    page: Annotated[EndpointPageQuery, Query()],
) -> EndpointPage:
    response.headers["Cache-Control"] = "private, no-store"
    result = service.list(actor, page.limit, page.cursor)
    return EndpointPage.model_validate(
        {
            **result,
            "items": [
                {
                    **item,
                    "detail_url": request.url_for("endpoint", endpoint_id=item["endpoint_id"]).path,
                }
                for item in result["items"]
            ],
        }
    )


@router.get(
    "/endpoints/{endpoint_id}",
    response_model=EndpointDetail,
    operation_id="get_prediction_endpoint",
    summary="Get a deployed model's contract and prediction example",
    description=(
        "Requires Bearer authentication and ml:predict within the current tenant. Returns the "
        "active route's model name/version, generation and verified public contract. Use "
        "request_example with predict_url. An incomplete legacy contract is explicit "
        "and may be null; ask the owner to publish a verified contract. Lookup performs no live "
        "MLflow, artifact or Serving calls. The prediction response identifies the actual version "
        "used if an update or rollback occurs after this lookup."
    ),
    responses={
        401: {"description": "Sign in again.", "model": PredictionError | AuthenticationErrorBody},
        403: {"description": "ml:predict is required.", "model": PredictionError},
        404: {"description": "Unknown, inactive or invisible endpoint.", "model": PredictionError},
        503: {
            "description": "Storage or endpoint identity is unavailable.",
            "model": PredictionError,
        },
    },
)
def endpoint(
    request: Request,
    endpoint_id: str,
    actor: ActorDependency,
    service: DiscoveryDependency,
    response: Response,
) -> EndpointDetail:
    response.headers["Cache-Control"] = "private, no-store"
    return EndpointDetail.model_validate(
        {
            **service.get(actor, endpoint_id),
            "detail_url": request.url_for("endpoint", endpoint_id=endpoint_id).path,
            "predict_url": request.url_for("predict").path,
        }
    )
