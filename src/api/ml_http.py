"""Thin transport adapters: validation, scoped identity and safe DTO rendering."""

import json
from dataclasses import asdict
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.ml_schemas import ApprovalRequest, DeploymentRequest, SubmitRequest
from src.application.ml_services import MLServices
from src.domain.ml import Actor, canonical_json, MLError, NotFound
from src.domain.model_registry.models import ModelBinding
from src.domain.training.models import Job

training_router = APIRouter(tags=["training"])
registry_router = APIRouter(tags=["model registry"])
deployment_router = APIRouter(tags=["deployments"])
dataset_router = APIRouter(tags=["approved datasets"])
bearer = HTTPBearer(auto_error=False)


def get_services(request: Request) -> MLServices:
    return request.app.state.ml_services


async def get_actor(request: Request, credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Actor:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    return await request.app.state.ml_identity.resolve(credentials.credentials)


ActorDependency = Annotated[Actor, Depends(get_actor)]
ServicesDependency = Annotated[MLServices, Depends(get_services)]


def public_job(job: Job) -> dict[str, object]:
    return {"job_id": job.job_id, "state": job.state, "phase": job.phase,
            "revision": job.revision, "attempt_id": job.attempt_id,
            "created_at": job.created_at, "error_code": job.error_code,
            "model_binding_id": job.model_binding_id,
            "retry_of_job_id": json.loads(job.spec_json).get("retry_of_job_id"),
            "status_url": f"/api/v1/training/jobs/{job.job_id}"}


@training_router.post("/jobs", status_code=202)
def submit(body: SubmitRequest, actor: ActorDependency, services: ServicesDependency,
           idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]) -> dict[str, object]:
    intent = body.spec.model_dump()
    if body.retry_of_job_id is not None:
        intent["retry_of_job_id"] = body.retry_of_job_id
    return public_job(services.training.submit(actor, idempotency_key,
                      canonical_json(intent)))


@training_router.get("/jobs/{job_id}")
def query_job(job_id: str, actor: ActorDependency, services: ServicesDependency) -> dict[str, object]:
    return public_job(services.training.get(actor, job_id))


@training_router.get("/jobs/{job_id}/attempts")
def query_attempts(job_id: str, actor: ActorDependency, services: ServicesDependency) -> list[dict[str, object]]:
    return [{"attempt_id": a.attempt_id, "number": a.number, "state": a.state,
             "phase": a.phase, "heartbeat_at": a.heartbeat_at}
            for a in services.training.attempts(actor, job_id)]


@training_router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel(job_id: str, actor: ActorDependency, services: ServicesDependency) -> dict[str, object]:
    return public_job(services.training.cancel(actor, job_id))


@registry_router.get("/models")
def list_models(actor: ActorDependency, services: ServicesDependency) -> list[dict[str, object]]:
    return [public_model(value) for value in services.registry.list(actor)]


def public_model(value: ModelBinding) -> dict[str, object]:
    return {"binding_id": value.binding_id, "registered_name": value.registered_name,
            "model_version": value.version, "manifest_digest": value.manifest_digest}


@registry_router.get("/bindings/{binding_id}")
def get_model(binding_id: str, actor: ActorDependency, services: ServicesDependency) -> dict[str, object]:
    return public_model(services.registry.get(actor, binding_id))


@registry_router.get("/models/{name}/versions")
def versions(name: str, actor: ActorDependency, services: ServicesDependency) -> list[dict[str, object]]:
    return [public_model(value) for value in services.registry.list(actor)
            if value.registered_name == name]


@registry_router.get("/bindings/{binding_id}/evaluations")
def model_evaluations(binding_id: str, actor: ActorDependency, services: ServicesDependency) -> list[dict[str, object]]:
    return [asdict(value) for value in services.registry.evaluations(actor, binding_id)]


@registry_router.post("/bindings/{binding_id}/approvals", status_code=201)
def approve(binding_id: str, body: ApprovalRequest, actor: ActorDependency,
            services: ServicesDependency) -> dict[str, object]:
    return asdict(services.registry.approve(actor, binding_id, body.evaluation_id, body.environment))


@registry_router.post("/models/{name}/versions/{version}/approvals", status_code=201)
def approve_version(name: str, version: str, body: ApprovalRequest, actor: ActorDependency,
                    services: ServicesDependency) -> dict[str, object]:
    for model in services.registry.list(actor):
        if model.registered_name == name and model.version == version:
            return asdict(services.registry.approve(actor, model.binding_id,
                          body.evaluation_id, body.environment))
    raise NotFound("Model version not found")


@deployment_router.post("", status_code=202)
def deploy(body: DeploymentRequest, actor: ActorDependency, services: ServicesDependency) -> dict[str, object]:
    value = services.deployments.create(
        actor, body.endpoint_id, body.model_binding_id, body.approval_id)
    return {"deployment_id": value.deployment_id, "generation": value.generation, "state": value.state}


@deployment_router.get("/{deployment_id}")
def get_deployment(deployment_id: str, actor: ActorDependency,
                   services: ServicesDependency) -> dict[str, object]:
    value = services.deployments.get(actor, deployment_id)
    return {"deployment_id": value.deployment_id, "endpoint_id": value.endpoint_id,
            "generation": value.generation, "state": value.state,
            "desired_state": "ACTIVE", "observed_state": value.state,
            "model_binding_id": value.model_binding_id, "approval_id": value.approval_id}


@deployment_router.post("/{deployment_id}/rollback", status_code=202)
def rollback(deployment_id: str, actor: ActorDependency, services: ServicesDependency) -> dict[str, object]:
    value = services.deployments.rollback(actor, deployment_id)
    return {"deployment_id": value.deployment_id, "generation": value.generation, "state": value.state}


def install_errors(app: FastAPI) -> None:
    from src.api.ml_body_limit import BodyLimitMiddleware
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(MLError)
    async def domain_error(request: Request, error: MLError) -> JSONResponse:
        return JSONResponse(status_code=error.status, content={"code": error.code,
                                                               "message": str(error), "request_id": getattr(request.state, "request_id", str(uuid4())),
                                                               "retryable": error.retryable})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _: RequestValidationError) -> JSONResponse:
        # Pydantic errors include raw input; never echo them into public errors/logs.
        return JSONResponse(status_code=422, content={"code": "SCHEMA_MISMATCH",
                                                      "message": "Request does not match the schema", "request_id": str(uuid4()), "retryable": False})


def mount_management(app: FastAPI) -> None:
    app.include_router(training_router, prefix="/api/v1/training")
    app.include_router(registry_router, prefix="/api/v1/model_registry")
    app.include_router(deployment_router, prefix="/api/v1/deployments")
    app.include_router(dataset_router, prefix="/api/v1/datasets")
    install_errors(app)


@dataset_router.get("/versions/{dataset_version_id}")
def dataset_version(dataset_version_id: str, actor: ActorDependency,
                    services: ServicesDependency) -> dict[str, object]:
    if "@" not in dataset_version_id:
        raise NotFound("Dataset version not found")
    dataset_id, version = dataset_version_id.rsplit("@", 1)
    value = services.datasets.get(actor, dataset_id, version)
    return {"dataset_id": value.dataset_id, "version": value.version,
            "content_digest": value.content_digest, **json.loads(value.summary_json)}
