"""Safe ingestion operation resources; acquisition runs in a separate worker."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from src.api.collection_schemas import COLLECTION_RESPONSES, MetadataPage
from src.api.ingestion.discovery_schemas import (
    IngestionJobMetadata,
    JobCollectionQuery,
    RawSnapshotMetadata,
    SnapshotCollectionQuery,
)
from src.api.ingestion.schemas import (
    AvailableSourcePage,
    AvailableSourceResponse,
    IngestionSubmitRequest,
)
from src.api.ml_docs import ml_url_path
from src.api.ml_http import ActorDependency
from src.application.ingestion.service import IngestionService
from src.domain.ingestion.models import IngestionJob, RawSnapshot
from src.domain.ml import Unavailable

router = APIRouter(tags=["data ingestion"])


def get_ingestion_service(request: Request) -> IngestionService:
    service = getattr(request.app.state, "ingestion_service", None)
    if service is None:
        raise Unavailable("Data ingestion is not configured")
    return cast(IngestionService, service)


IngestionDependency = Annotated[IngestionService, Depends(get_ingestion_service)]


def public_job(job: IngestionJob, request: Request) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "state": job.state,
        "revision": job.revision,
        "source_id": job.source_id,
        "source_version": job.source_version,
        "attempt_id": job.attempt_id,
        "attempt_count": job.attempt_count,
        "snapshot_id": job.snapshot_id,
        "created_at": job.created_at,
        "deadline": job.deadline,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
        "status_url": ml_url_path(request, "get_ingestion_job", job_id=job.job_id),
        "result_url": ml_url_path(request, "get_result", job_id=job.job_id),
    }


def public_snapshot(snapshot: RawSnapshot) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "job_id": snapshot.job_id,
        "source_id": snapshot.source_id,
        "source_version": snapshot.source_version,
        "source_configuration_digest": snapshot.source_configuration_digest,
        "content_digest": snapshot.content_digest,
        "manifest_digest": snapshot.manifest_digest,
        "data_format": snapshot.data_format,
        "schema_version": snapshot.schema_version,
        "size_bytes": snapshot.size_bytes,
        "record_count": snapshot.record_count,
        "created_at": snapshot.created_at,
        "source_revision": snapshot.source_revision,
    }


@router.post("/jobs", status_code=202)
def submit(
    body: IngestionSubmitRequest,
    request: Request,
    actor: ActorDependency,
    service: IngestionDependency,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    return public_job(service.submit(actor, idempotency_key, body.to_domain()), request)


@router.get(
    "/jobs/{job_id}",
    name="get_ingestion_job",
    operation_id="get_job_api_v1_ingestion_jobs__job_id__get",
)
def get_job(
    job_id: str, request: Request, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_job(service.get(actor, job_id), request)


@router.get("/jobs/{job_id}/result")
def get_result(
    job_id: str, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_snapshot(service.result(actor, job_id))


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(
    job_id: str, request: Request, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_job(service.cancel(actor, job_id), request)


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(
    snapshot_id: str, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_snapshot(service.snapshot(actor, snapshot_id))


@router.get("/available-sources", response_model=AvailableSourcePage)
def available_sources(
    actor: ActorDependency,
    service: IngestionDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> AvailableSourcePage:
    sources, more = service.available_sources(actor, limit, offset)
    return AvailableSourcePage(
        items=[
            AvailableSourceResponse(
                source_id=s.source_id,
                version=s.version,
                adapter_type=s.adapter_type,
                data_format=s.data_format,
                schema_version=s.schema_version,
                limits=s.limits.to_dict(),
            )
            for s in sources
        ],
        limit=limit,
        next_offset=offset + limit if more else None,
        has_more=more,
    )


@router.get(
    "/jobs",
    response_model=MetadataPage[IngestionJobMetadata],
    summary="List visible ingestion jobs",
    operation_id="list_ingestion_jobs",
    responses=COLLECTION_RESPONSES,
    description="Requires ml:ingest, ml:ingestion:read or ml:ingestion:operate. "
    "Filters the current tenant and owner in SQL; operate can inspect all jobs in that tenant. "
    "Filter by exact source/version, job state or strict UTC creation bounds. "
    "Order is created_at descending, then job_id descending. Default 25/max 100 items. "
    "Concurrent inserts may shift offset pages. Follow status_url or result_url for details.",
)
def jobs(
    request: Request,
    response: Response,
    actor: ActorDependency,
    service: IngestionDependency,
    page: Annotated[JobCollectionQuery, Query()],
) -> MetadataPage[IngestionJobMetadata]:
    response.headers["Cache-Control"] = "private, no-store"
    query = page.query()
    items, more = service.collections(actor, query)
    return MetadataPage[IngestionJobMetadata](
        items=[
            IngestionJobMetadata.model_validate(public_job(cast(IngestionJob, item), request))
            for item in items
        ],
        limit=page.limit,
        offset=page.offset,
        next_offset=query.window.next_offset(has_more=more),
    )


@router.get(
    "/snapshots",
    response_model=MetadataPage[RawSnapshotMetadata],
    summary="List published raw snapshot metadata",
    operation_id="list_raw_snapshots",
    responses=COLLECTION_RESPONSES,
    description="Uses the same tenant, owner/operate and read permissions as snapshot detail. "
    "Only durably PUBLISHED jobs are included. Filters use exact source/version and strict "
    "UTC creation bounds. Order is created_at descending, then snapshot_id descending. "
    "Default 25/max 100 items with offset continuation. Returns immutable identity and digests; "
    "raw bytes and filesystem paths are excluded.",
)
def snapshots(
    response: Response,
    actor: ActorDependency,
    service: IngestionDependency,
    page: Annotated[SnapshotCollectionQuery, Query()],
) -> MetadataPage[RawSnapshotMetadata]:
    response.headers["Cache-Control"] = "private, no-store"
    query = page.query()
    items, more = service.collections(actor, query, snapshots=True)
    return MetadataPage[RawSnapshotMetadata](
        items=[
            RawSnapshotMetadata.model_validate(public_snapshot(cast(RawSnapshot, item)))
            for item in items
        ],
        limit=page.limit,
        offset=page.offset,
        next_offset=query.window.next_offset(has_more=more),
    )
