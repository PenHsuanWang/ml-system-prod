import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from src.domain.execution import (
    ExecutionBusy,
    ResourceBusy,
    ResourceLeaseLost,
    ResourceUnavailable,
)
from src.domain.exports.errors import (
    ExportError,
    ExportSourceNotFound,
    InvalidExportRequest,
)
from src.domain.identity.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    IdentityError,
    IdentityUnavailable,
    InvalidIdentityInput,
    UserAlreadyExists,
    UserNotFound,
)
from src.domain.jobs.errors import (
    DispatcherUnavailable,
    JobError,
    JobNotFound,
    QueueFull,
    UnknownTask,
)
from src.domain.kernels.models import (
    InvalidKernelInput,
    KernelDeadlineExceeded,
    KernelExecutionFailed,
    KernelNotFound,
)
from src.shared_models.dto import ErrorEnvelope

logger = logging.getLogger(__name__)


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "-")
    content: ErrorEnvelope = {"error": {"code": code, "message": message}, "request_id": request_id}
    return JSONResponse(
        status_code=status,
        content=content,
        headers={**(headers or {}), "X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(IdentityError)
    async def identity_error_handler(request: Request, exc: IdentityError) -> JSONResponse:
        if isinstance(exc, AuthenticationFailed):
            return error_response(
                request,
                401,
                "authentication_failed",
                "Invalid credentials",
                {"WWW-Authenticate": "Bearer"},
            )
        if isinstance(exc, AuthorizationDenied):
            return error_response(request, 403, "permission_denied", str(exc))
        if isinstance(exc, UserNotFound):
            return error_response(request, 404, "user_not_found", str(exc))
        if isinstance(exc, UserAlreadyExists):
            return error_response(request, 409, "user_already_exists", str(exc))
        if isinstance(exc, InvalidIdentityInput):
            return error_response(request, 400, "invalid_identity_input", str(exc))
        if isinstance(exc, IdentityUnavailable):
            return error_response(
                request, 503, "identity_unavailable", "Identity services are unavailable"
            )
        return error_response(request, 500, "identity_error", "Identity operation failed")

    async def execution_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return error_response(request, 503, "execution_unavailable", str(exc))

    for exception_type in (ExecutionBusy, ResourceBusy, ResourceLeaseLost, ResourceUnavailable):
        app.add_exception_handler(exception_type, execution_error_handler)

    @app.exception_handler(KernelNotFound)
    async def kernel_missing_handler(request: Request, exc: KernelNotFound) -> JSONResponse:
        return error_response(request, 404, "kernel_not_found", str(exc))

    @app.exception_handler(KernelExecutionFailed)
    async def kernel_failure_handler(request: Request, exc: KernelExecutionFailed) -> JSONResponse:
        if isinstance(exc, KernelDeadlineExceeded):
            return error_response(request, 504, "kernel_deadline_exceeded", str(exc))
        return error_response(request, 500, "kernel_execution_failed", str(exc))

    @app.exception_handler(InvalidKernelInput)
    async def kernel_input_handler(request: Request, exc: InvalidKernelInput) -> JSONResponse:
        return error_response(request, 422, "invalid_kernel_input", str(exc))

    @app.exception_handler(ExportError)
    async def export_error_handler(request: Request, exc: ExportError) -> JSONResponse:
        status = 404 if isinstance(exc, ExportSourceNotFound) else 503
        if isinstance(exc, InvalidExportRequest):
            status = 422
        return error_response(request, status, "export_error", str(exc))

    @app.exception_handler(JobError)
    async def job_error_handler(request: Request, exc: JobError) -> JSONResponse:
        if isinstance(exc, JobNotFound):
            return error_response(request, 404, "job_not_found", str(exc))
        if isinstance(exc, UnknownTask):
            return error_response(request, 400, "unknown_task", str(exc))
        if isinstance(exc, QueueFull):
            return error_response(request, 503, "queue_full", str(exc), {"Retry-After": "1"})
        if isinstance(exc, DispatcherUnavailable):
            return error_response(request, 503, "dispatcher_unavailable", str(exc))
        return error_response(request, 500, "job_error", "Job processing failed")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "validation_error", "Request validation failed")

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return error_response(request, exc.status_code, "http_error", message, exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled request failure", exc_info=(type(exc), exc, exc.__traceback__))
        return error_response(request, 500, "internal_error", "Internal server error")
