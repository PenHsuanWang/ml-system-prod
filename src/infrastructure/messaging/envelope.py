"""Versioned JSON task envelopes shared by local and distributed admission."""

import json
from datetime import datetime
from typing import Any

from src.domain.jobs.models import TaskRequest
from src.utils.validation import is_valid_request_id


def encode_request(request: TaskRequest) -> dict[str, Any]:
    data = {
        "schema_version": request.schema_version,
        "job_id": request.job_id,
        "name": request.name,
        "payload": request.payload,
        "submitted_at": request.submitted_at.isoformat(),
        "tenant_id": request.tenant_id,
        "owner_id": request.owner_id,
        "request_id": request.request_id,
    }
    # Outbound and inbound delivery envelopes share the same validation contract.
    decode_request(data)
    return data


def decode_request(data: dict[str, Any]) -> TaskRequest:
    if not isinstance(data, dict):
        raise ValueError("Task envelope must be an object")
    version = data.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ValueError("Unsupported task envelope schema version")
    for field in ("job_id", "name"):
        value = data.get(field)
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"Task envelope {field} must be a nonempty identifier")
    tenant_id, owner_id = data.get("tenant_id"), data.get("owner_id")
    if (tenant_id is None) != (owner_id is None):
        raise ValueError("Task ownership requires both tenant_id and owner_id")
    for field, value in (("tenant_id", tenant_id), ("owner_id", owner_id)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"Task envelope {field} must be a nonempty string")
    request_id = data.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not is_valid_request_id(request_id)
    ):
        raise ValueError("Task request_id must be a valid correlation identifier")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Task payload must be a JSON object")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Task payload must contain JSON values") from None
    submitted_at = data.get("submitted_at")
    if not isinstance(submitted_at, str):
        raise ValueError("Task submitted_at must be an ISO datetime string")
    try:
        submitted = datetime.fromisoformat(submitted_at)
    except ValueError:
        raise ValueError("Task submitted_at must be an ISO datetime string") from None
    return TaskRequest(
        job_id=data["job_id"],
        name=data["name"],
        payload=payload,
        submitted_at=submitted,
        tenant_id=tenant_id,
        owner_id=owner_id,
        schema_version=version,
        request_id=request_id,
    )
