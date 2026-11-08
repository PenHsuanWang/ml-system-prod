"""Safe ingestion operation resources; acquisition runs in a separate worker."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Request

from src.api.ingestion.schemas import IngestionSubmitRequest
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


def public_job(job: IngestionJob) -> dict[str, object]:
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
        "status_url": f"/api/v1/ingestion/jobs/{job.job_id}",
        "result_url": f"/api/v1/ingestion/jobs/{job.job_id}/result",
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
    actor: ActorDependency,
    service: IngestionDependency,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    return public_job(service.submit(actor, idempotency_key, body.to_domain()))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, actor: ActorDependency, service: IngestionDependency) -> dict[str, object]:
    return public_job(service.get(actor, job_id))


@router.get("/jobs/{job_id}/result")
def get_result(
    job_id: str, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_snapshot(service.result(actor, job_id))


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(
    job_id: str, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_job(service.cancel(actor, job_id))


@router.get("/snapshots/{snapshot_id}")
def get_snapshot(
    snapshot_id: str, actor: ActorDependency, service: IngestionDependency
) -> dict[str, object]:
    return public_snapshot(service.snapshot(actor, snapshot_id))
