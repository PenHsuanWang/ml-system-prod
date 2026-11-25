"""Authenticated source transport. Application services own authorization."""

import json
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from src.api.ingestion.source_schemas import (
    ConnectionResponse,
    SourcePage,
    SourceRegisterRequest,
    SourceResponse,
    SourceVersionRequest,
)
from src.api.ml_http import ActorDependency
from src.application.ingestion.source_management import SourceManagementService
from src.domain.ingestion.models import ExtractionLimits, SourceDefinition
from src.domain.ml import Unavailable

router = APIRouter(tags=["data sources"])


def management(request: Request) -> SourceManagementService:
    service = getattr(request.app.state, "source_management", None)
    if service is None:
        raise Unavailable("Source management is not configured")
    return cast(SourceManagementService, service)


SourceManagementDependency = Annotated[SourceManagementService, Depends(management)]


def public_source(value: tuple[SourceDefinition, bool]) -> dict[str, object]:
    source, enabled = value
    configuration = json.loads(source.configuration_json)
    # Only the typed runtime template crosses this management boundary.
    return {
        "source_id": source.source_id,
        "version": source.version,
        "adapter_type": source.adapter_type,
        "configuration_digest": source.configuration_digest,
        "configuration": configuration.get("template", {}),
        "limits": source.limits.to_dict(),
        "allowed_actor_ids": list(source.allowed_actor_ids),
        "enabled": enabled,
        "external_qualification": "not_verified",
    }


@router.get("/connections", response_model=list[ConnectionResponse])
def connections(
    actor: ActorDependency, service: SourceManagementDependency
) -> list[dict[str, str]]:
    return [{"connection_ref": ref} for ref in service.connections(actor)]


@router.post("/sources", response_model=SourceResponse, status_code=201)
def register(
    body: SourceRegisterRequest, actor: ActorDependency, service: SourceManagementDependency
) -> dict[str, object]:
    return public_source(
        service.register(
            actor,
            body.source_id,
            body.version,
            body.configuration(),
            ExtractionLimits(**body.limits.model_dump()),
            tuple(body.allowed_actor_ids),
        )
    )


@router.post("/sources/{source_id}/versions", response_model=SourceResponse, status_code=201)
def version(
    source_id: str,
    body: SourceVersionRequest,
    actor: ActorDependency,
    service: SourceManagementDependency,
) -> dict[str, object]:
    return public_source(
        service.register(
            actor,
            source_id,
            body.version,
            body.configuration(),
            ExtractionLimits(**body.limits.model_dump()),
            tuple(body.allowed_actor_ids),
        )
    )


@router.get("/sources", response_model=SourcePage)
@router.get("/sources/{source_id}/versions", response_model=SourcePage)
def sources(
    actor: ActorDependency,
    service: SourceManagementDependency,
    source_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict[str, object]:
    return {
        "items": [public_source(v) for v in service.list(actor, source_id, limit, offset)],
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit,
    }


@router.get("/sources/{source_id}/versions/{version}", response_model=SourceResponse)
def get(
    source_id: str, version: str, actor: ActorDependency, service: SourceManagementDependency
) -> dict[str, object]:
    return public_source(service.get(actor, source_id, version))


@router.post("/sources/{source_id}/versions/{version}/enable", response_model=SourceResponse)
def enable(
    source_id: str, version: str, actor: ActorDependency, service: SourceManagementDependency
) -> dict[str, object]:
    return public_source(service.set_enabled(actor, source_id, version, True))


@router.post("/sources/{source_id}/versions/{version}/disable", response_model=SourceResponse)
def disable(
    source_id: str, version: str, actor: ActorDependency, service: SourceManagementDependency
) -> dict[str, object]:
    return public_source(service.set_enabled(actor, source_id, version, False))
