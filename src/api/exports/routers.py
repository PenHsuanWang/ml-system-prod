from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from src.api.auth import current_principal
from src.api.dependencies import RuntimeDependency
from src.application.use_cases import ExportService
from src.domain.exports.errors import (
    ExportRowTooLarge,
    ExportSourceNotFound,
    ExportUnavailable,
    InvalidExportRequest,
)
from src.domain.exports.models import MAX_EXPORT_ROWS, ExportRequest
from src.domain.identity.models import Principal

router = APIRouter(prefix="/exports", tags=["exports"])
PrincipalDependency = Annotated[Principal, Depends(current_principal)]


def get_export_service(runtime: RuntimeDependency) -> ExportService:
    service = runtime.exports
    if service is None:
        raise HTTPException(status_code=503, detail="Export service is unavailable")
    return service


ExportServiceDependency = Annotated[ExportService, Depends(get_export_service)]


class ExportColumnResponse(BaseModel):
    name: str
    permission: str | None


class ExportSourceResponse(BaseModel):
    source_name: str
    columns: list[ExportColumnResponse]


@router.get("/sources", response_model=list[ExportSourceResponse])
async def list_sources(
    principal: PrincipalDependency, service: ExportServiceDependency
) -> list[ExportSourceResponse]:
    return [
        ExportSourceResponse(
            source_name=descriptor.source_name,
            columns=[
                ExportColumnResponse(name=column.name, permission=column.permission)
                for column in descriptor.columns
            ],
        )
        for descriptor in service.list_sources(principal)
    ]


@router.get("/{source_name}")
async def download_export(
    source_name: str,
    principal: PrincipalDependency,
    service: ExportServiceDependency,
    format: Annotated[Literal["csv", "jsonl"], Query()] = "csv",
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_ROWS)] = 1000,
) -> StreamingResponse:
    try:
        prepared = await service.prepare(ExportRequest(source_name, format, limit), principal)
    except ExportSourceNotFound:
        raise HTTPException(status_code=404, detail="Export source does not exist") from None
    except InvalidExportRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ExportRowTooLarge:
        raise HTTPException(status_code=413, detail="Export row exceeds the size limit") from None
    except ExportUnavailable:
        raise HTTPException(status_code=503, detail="Export source is unavailable") from None
    return StreamingResponse(
        prepared.stream,
        media_type=prepared.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{prepared.filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
