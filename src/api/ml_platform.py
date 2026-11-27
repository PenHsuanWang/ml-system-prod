"""First-use links and authenticated entry points for the current ML API."""

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request, Response
from pydantic import Field

from src.api.collection_schemas import (
    COLLECTION_RESPONSES,
    CollectionAuthenticationError,
    CollectionError,
    CollectionQuery,
    MetadataPage,
)
from src.api.governance_discovery import Discovery, page
from src.api.ml_docs import ml_url_path
from src.api.ml_http import ActorDependency
from src.api.ml_schemas import StrictModel


class IdentityLinks(StrictModel):
    login_url: str = Field(description="POST to obtain an access token.")
    me_url: str = Field(description="GET with a Bearer token to inspect the current account.")
    logout_url: str = Field(description="POST with a Bearer token to revoke its session.")


class PlatformInfo(StrictModel):
    service: Literal["management", "prediction-gateway"]
    swagger_url: str = Field(description="Interactive API reference.")
    redoc_url: str = Field(description="Read-only API reference.")
    openapi_url: str = Field(description="Machine-readable API description.")
    capabilities_url: str = Field(description="GET with a Bearer token to find entry points.")
    identity: IdentityLinks | None = Field(
        description="Identity routes available on this origin when configured."
    )
    sign_in: str = Field(description="How to locate the deployment's trusted Identity URL.")


class Capability(StrictModel):
    id: str
    method: Literal["GET", "POST"]
    url: str = Field(description="Origin-relative entry point including the proxy prefix.")
    description: str


class Capabilities(StrictModel):
    items: list[Capability]


class TemplateErrorEnvelope(StrictModel):
    error: dict[str, str]
    request_id: str


_PLATFORM_RESPONSES = {
    **COLLECTION_RESPONSES,
    401: {
        "description": "Sign in again using the deployment's Identity URL.",
        "model": CollectionAuthenticationError | CollectionError | TemplateErrorEnvelope,
    },
    503: {
        "description": "Identity or SQL is unavailable. Retry with backoff.",
        "model": CollectionError | TemplateErrorEnvelope,
    },
}


@dataclass(frozen=True)
class EntryPoint:
    id: str
    permissions: tuple[str, ...]
    path: str
    description: str
    method: Literal["GET", "POST"] = "GET"


CATALOG = EntryPoint(
    "catalog",
    ("ml:catalog:read",),
    "/api/v1/catalog/connections",
    "Browse approved catalog metadata.",
)
SOURCE_MANAGEMENT = EntryPoint(
    "source-management",
    ("ml:source:manage",),
    "/api/v1/ingestion/sources",
    "Browse approved source definitions and their versions.",
)
INGESTION = (
    EntryPoint(
        "ingestion-submit",
        ("ml:ingest",),
        "/api/v1/ingestion/available-sources",
        "Find approved source versions for ingestion.",
    ),
    EntryPoint(
        "snapshot-read",
        ("ml:ingest", "ml:ingestion:read", "ml:ingestion:operate"),
        "/api/v1/ingestion/snapshots",
        "Find published snapshot metadata.",
    ),
)
DATASET_BUILD = EntryPoint(
    "dataset-build",
    ("ml:dataset:manage",),
    "/api/v1/datasets/builders",
    "Find builders. Submissions also require access to the input snapshots.",
)
MANAGEMENT = (
    EntryPoint(
        "dataset-read",
        ("ml:train", "ml:dataset:read"),
        "/api/v1/datasets",
        "Find immutable dataset versions and lineage.",
    ),
    EntryPoint(
        "training",
        ("ml:train",),
        "/api/v1/training/recipes",
        "Find approved training recipes and admission requirements.",
    ),
    EntryPoint(
        "deployment-submit",
        ("ml:deploy",),
        "/api/v1/deployments",
        "Request a deployment using an approved binding.",
        "POST",
    ),
)
GOVERNANCE = (
    EntryPoint(
        "model-registry",
        ("ml:model:read",),
        "/api/v1/model_registry/bindings",
        "Find registered bindings, evaluations and approvals.",
    ),
    EntryPoint(
        "deployment-read",
        ("ml:deploy", "ml:deployment:read"),
        "/api/v1/deployments",
        "Review deployments and route assignments.",
    ),
    EntryPoint(
        "audit",
        ("ml:operate",),
        "/api/v1/platform/audit-events",
        "Find recorded events for a subject in a bounded time window.",
    ),
)
METRICS = EntryPoint(
    "metrics",
    ("ml:operate",),
    "/metrics",
    "Read operational metrics.",
)
PREDICTION_ENDPOINTS = EntryPoint(
    "prediction-discovery",
    ("ml:predict",),
    "/api/v1/prediction/endpoints",
    "Find assigned endpoints and their inference contracts.",
)
PREDICT = EntryPoint(
    "prediction",
    ("ml:predict",),
    "/api/v1/prediction/predict",
    "Run inference with an active endpoint.",
    "POST",
)


def install_platform_discovery(
    app: FastAPI,
    service: Literal["management", "prediction-gateway"],
    entry_points: tuple[EntryPoint, ...],
    *,
    local_identity: bool = False,
    proxy_identity: IdentityLinks | None = None,
) -> None:
    @app.get(
        "/api/v1/platform/info",
        tags=["platform"],
        operation_id="get_platform_info",
        summary="Find API documentation and sign-in instructions",
        description="Public, static first-use links for this HTTP service. Paths are relative "
        "to the origin and include its configured ASGI prefix.",
    )
    def platform_info(request: Request) -> PlatformInfo:
        identity = proxy_identity
        if local_identity:
            identity = IdentityLinks(
                login_url=ml_url_path(request, "login"),
                me_url=ml_url_path(request, "me"),
                logout_url=ml_url_path(request, "logout"),
            )
        return PlatformInfo(
            service=service,
            swagger_url=ml_url_path(request, "swagger_ui_html"),
            redoc_url=ml_url_path(request, "redoc_html"),
            openapi_url=ml_url_path(request, "openapi"),
            capabilities_url=ml_url_path(request, "platform_capabilities"),
            identity=identity,
            sign_in="Use your deployment's trusted HTTPS Identity URL. Sign in with "
            "POST /api/v1/identity/login, then use the returned access_token as a Bearer token.",
        )

    @app.get(
        "/api/v1/platform/capabilities",
        tags=["platform"],
        operation_id="list_platform_capabilities",
        responses=_PLATFORM_RESPONSES,
        summary="List workflow entry points for the current account",
        description="Requires a current Bearer session. Returns enabled entry points whose "
        "listed permission alternatives match the account. Each operation applies its own "
        "tenant, resource and approval checks. An empty list means none of these entry points "
        "match. Capability presence does not attest to worker or model readiness.",
    )
    def platform_capabilities(
        request: Request, response: Response, actor: ActorDependency
    ) -> Capabilities:
        response.headers["Cache-Control"] = "private, no-store"
        prefix = request.scope.get("root_path", "").rstrip("/")
        available = entry_points
        if (
            service == "prediction-gateway"
            and getattr(app.state, "endpoint_discovery", None) is not None
        ):
            available = (PREDICTION_ENDPOINTS, *available)
        return Capabilities(
            items=[
                Capability(
                    id=entry.id,
                    method=entry.method,
                    url=prefix + entry.path,
                    description=entry.description,
                )
                for entry in available
                if not actor.permissions.isdisjoint(entry.permissions)
            ]
        )


class AuditQuery(CollectionQuery):
    subject_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    created_after: float = Field(ge=0, description="Exclusive start in UTC Unix seconds.")
    created_before: float = Field(ge=0, description="Exclusive end in UTC Unix seconds.")


class AuditEvent(StrictModel):
    event_id: str
    subject_id: str
    actor_id: str
    action: str
    created_at: float = Field(description="Stored event timestamp in UTC Unix seconds.")


def install_platform_audit(app: FastAPI) -> None:
    @app.get(
        "/api/v1/platform/audit-events",
        tags=["platform"],
        operation_id="list_platform_audit_events",
        responses=_PLATFORM_RESPONSES,
        summary="Read recorded audit events for a subject",
        description="Requires ml:operate. The authenticated tenant, exact subject ID and "
        "exclusive time bounds (maximum seven days) constrain the SQL query. Order is "
        "newest first, then event ID descending. Default 25 and maximum 100 items. "
        "Concurrent writes can shift offset pages. An empty page means no matching records. "
        "Only persisted audit events are returned.",
    )
    def platform_audit_events(
        response: Response,
        actor: ActorDependency,
        service: Discovery,
        query: Annotated[AuditQuery, Query()],
    ) -> MetadataPage[AuditEvent]:
        response.headers["Cache-Control"] = "private, no-store"
        window = query.window()
        return page(AuditEvent, service.audit_events(actor, query.subject_id, window), window)
