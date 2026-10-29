"""Framework-free identities, errors, and versioned canonical JSON."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass
from decimal import Decimal


class MLError(Exception):
    code = "INVALID_REQUEST"
    status = 400
    retryable = False


class NotFound(MLError):
    code, status = "NOT_FOUND", 404


class Forbidden(MLError):
    code, status = "FORBIDDEN", 403


class Conflict(MLError):
    code, status = "STALE_REVISION", 409


class IdempotencyConflict(Conflict):
    code = "IDEMPOTENCY_CONFLICT"


class CapacityExceeded(MLError):
    code, status, retryable = "CAPACITY_EXCEEDED", 429, True


class Unavailable(MLError):
    code, status, retryable = "DEPENDENCY_UNAVAILABLE", 503, True


class SchemaMismatch(MLError):
    code, status = "SCHEMA_MISMATCH", 422


@dataclass(frozen=True)
class Actor:
    tenant_id: str
    actor_id: str
    permissions: frozenset[str]

    @classmethod
    def from_identity(cls, tenant_id: str, actor_id: str, permissions: frozenset[str]) -> "Actor":
        # The template accepts dot-separated permissions; public ML scope names use ':'.
        aliases = {"ml.train": "ml:train", "ml.cancel": "ml:cancel",
                   "ml.dataset.read": "ml:dataset:read", "ml.dataset.manage": "ml:dataset:manage",
                   "ml.model.read": "ml:model:read", "ml.model.approve": "ml:model:approve",
                   "ml.deploy": "ml:deploy", "ml.predict": "ml:predict",
                   "ml.jobs.read_all": "ml:jobs:read_all", "ml.operate": "ml:operate"}
        return cls(tenant_id, actor_id, permissions | frozenset(
            aliases[p] for p in permissions if p in aliases))

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise Forbidden("Permission denied")


def canonical_json(value: object) -> str:
    """ml-json-v1: NFC strings/keys, sorted keys, finite decimal numbers.

    This is a versioned application contract, not a claim to implement JCS.
    Equivalent integer/float representations compare equally. Surrogates and
    normalized duplicate keys are rejected; defaults are expanded by the DTO.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            raise MLError("Non-finite numbers are forbidden")
        number = Decimal(str(value))
        if not number.is_finite() or abs(number.adjusted()) > 308:
            raise MLError("Number outside canonical range")
        if not number:
            return "0"
        result = format(number, "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        try:
            normalized.encode("utf-8")
        except UnicodeEncodeError:
            raise MLError("Invalid Unicode string") from None
        return json.dumps(normalized, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise MLError("JSON keys must be strings")
        normalized_items = {unicodedata.normalize("NFC", key): item for key, item in value.items()}
        if len(normalized_items) != len(value):
            raise MLError("Duplicate normalized JSON keys")
        return "{" + ",".join(
            canonical_json(key) + ":" + canonical_json(normalized_items[key])
            for key in sorted(normalized_items)
        ) + "}"
    raise MLError("Unsupported canonical JSON value")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
