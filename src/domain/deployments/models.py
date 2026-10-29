"""Routing revisions fence delayed operations independently of model generation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    tenant_id: str
    endpoint_id: str
    revision: int
    deployment_id: str | None
    generation: int


@dataclass(frozen=True)
class Deployment:
    deployment_id: str
    tenant_id: str
    endpoint_id: str
    generation: int
    model_binding_id: str
    approval_id: str
    state: str
    expected_route_revision: int
    spec_json: str
    spec_digest: str
    resources_released: bool = False
