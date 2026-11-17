"""Validate responses against the immutable SQL deployment, independent of transport."""

import hashlib
import json
from typing import Any

from src.domain.deployments.models import Deployment
from src.domain.ml import Conflict


def deployment_spec(deployment: Deployment) -> dict[str, Any]:
    if hashlib.sha256(deployment.spec_json.encode()).hexdigest() != deployment.spec_digest:
        raise Conflict("Deployment specification digest mismatch")
    spec = json.loads(deployment.spec_json)
    if not isinstance(spec, dict):
        raise Conflict("Invalid deployment specification")
    for key in ("deployment_id", "tenant_id", "endpoint_id", "generation"):
        if spec.get(key) != getattr(deployment, key):
            raise Conflict("Deployment specification identity mismatch")
    return spec


def serving_identity(deployment: Deployment) -> dict[str, Any]:
    spec = deployment_spec(deployment)
    return {
        "deployment_id": deployment.deployment_id,
        "generation": deployment.generation,
        "endpoint_id": deployment.endpoint_id,
        "spec_digest": deployment.spec_digest,
        "manifest_digest": spec["manifest_digest"],
        "registered_model": spec["registered_name"],
        "model_version": spec["model_version"],
    }


def matches_identity(deployment: Deployment, value: object) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("generation")) is int
        and all(
            value.get(key) == expected for key, expected in serving_identity(deployment).items()
        )
    )
