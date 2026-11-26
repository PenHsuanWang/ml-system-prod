"""Authenticated read-only discovery. Blocking operations run in the HTTP threadpool."""

from dataclasses import asdict
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from src.api.catalog.schemas import (
    ColumnQuery,
    ConnectionReference,
    ConnectionsResponse,
    MetadataPageResponse,
    PageQuery,
    SchemaQuery,
    TableQuery,
)
from src.api.ml_http import ActorDependency
from src.application.catalog.service import CatalogService
from src.domain.catalog.models import MetadataPage, MetadataQuery
from src.domain.ml import Unavailable

router = APIRouter(tags=["catalog discovery"])


def get_catalog(request: Request) -> CatalogService:
    service = getattr(request.app.state, "catalog_service", None)
    if service is None:
        raise Unavailable("Catalog discovery is not configured")
    return cast(CatalogService, service)


CatalogDependency = Annotated[CatalogService, Depends(get_catalog)]


def response(page: MetadataPage) -> MetadataPageResponse:
    return MetadataPageResponse.model_validate(
        asdict(page) | {"items": [asdict(i) for i in page.items]}
    )


@router.get("/connections", response_model=ConnectionsResponse)
def connections(
    actor: ActorDependency, service: CatalogDependency, page: Annotated[PageQuery, Query()]
) -> ConnectionsResponse:
    values = service.connections(actor)
    more = len(values) > page.offset + page.limit
    return ConnectionsResponse(
        items=values[page.offset : page.offset + page.limit],
        limit=page.limit,
        next_offset=page.offset + page.limit if more else None,
        has_more=more,
    )


@router.get("/{connection_ref}/catalogs", response_model=MetadataPageResponse)
def catalogs(
    connection_ref: ConnectionReference,
    actor: ActorDependency,
    service: CatalogDependency,
    page: Annotated[PageQuery, Query()],
) -> MetadataPageResponse:
    return response(
        service.browse(
            actor, connection_ref, MetadataQuery("catalogs", (), page.limit, page.offset)
        )
    )


@router.get("/{connection_ref}/schemas", response_model=MetadataPageResponse)
def schemas(
    connection_ref: ConnectionReference,
    actor: ActorDependency,
    service: CatalogDependency,
    page: Annotated[SchemaQuery, Query()],
) -> MetadataPageResponse:
    return response(
        service.browse(
            actor,
            connection_ref,
            MetadataQuery("schemas", (page.catalog,), page.limit, page.offset),
        )
    )


@router.get("/{connection_ref}/tables", response_model=MetadataPageResponse)
def tables(
    connection_ref: ConnectionReference,
    actor: ActorDependency,
    service: CatalogDependency,
    page: Annotated[TableQuery, Query()],
) -> MetadataPageResponse:
    return response(
        service.browse(
            actor,
            connection_ref,
            MetadataQuery("tables", (page.catalog, page.schema_name), page.limit, page.offset),
        )
    )


@router.get("/{connection_ref}/columns", response_model=MetadataPageResponse)
def columns(
    connection_ref: ConnectionReference,
    actor: ActorDependency,
    service: CatalogDependency,
    page: Annotated[ColumnQuery, Query()],
) -> MetadataPageResponse:
    return response(
        service.browse(
            actor,
            connection_ref,
            MetadataQuery(
                "columns", (page.catalog, page.schema_name, page.table), page.limit, page.offset
            ),
        )
    )
