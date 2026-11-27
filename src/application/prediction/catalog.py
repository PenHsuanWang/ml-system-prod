"""Authorized inference discovery and explicit publication of verified public contracts."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from src.application.model_registry.ports import RegistryRepository
from src.domain.ml import Actor, Conflict, SchemaMismatch, canonical_json
from src.domain.model_registry.models import ModelBinding
from src.domain.prediction.catalog import PublicModelContract, reviewed_usage


class EndpointCatalogRepository(Protocol):
    def page(self, actor: Actor, after: str, limit: int) -> list[dict[str, Any]]: ...
    def detail(self, actor: Actor, endpoint_id: str) -> dict[str, Any]: ...


class PublicContractWriter(Protocol):
    def publish_contract(
        self, actor: Actor, contract: PublicModelContract, usage_json: str
    ) -> int: ...


class ContractPublicationService:
    def __init__(
        self,
        registry: RegistryRepository,
        writer: PublicContractWriter,
        verified_reader: Callable[[ModelBinding], PublicModelContract],
    ) -> None:
        self.registry, self.writer, self.verified_reader = registry, writer, verified_reader

    def publish(self, actor: Actor, binding_id: str, usage: dict[str, Any]) -> int:
        actor.require("ml:model:approve")
        actor.require("ml:model:read")
        metadata = reviewed_usage(usage)
        binding = self.registry.get_model(actor, binding_id)
        contract = self.verified_reader(binding)
        if (
            contract.binding_id != binding.binding_id
            or contract.manifest_digest != binding.manifest_digest
        ):
            raise Conflict("Verified contract does not match the requested binding")
        return self.writer.publish_contract(actor, contract, metadata)


class EndpointDiscoveryService:
    def __init__(
        self,
        repository: EndpointCatalogRepository,
        signing_key: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(signing_key.encode()) < 32:
            raise ValueError("Discovery cursor signing key must have at least 32 bytes")
        self.repository = repository
        self.key = hmac.digest(
            signing_key.encode(), b"prediction-discovery-cursor-v1", hashlib.sha256
        )
        self.clock = clock

    def _cursor(self, actor: Actor, after: str, limit: int) -> str:
        raw = canonical_json(
            {
                "tenant": actor.tenant_id,
                "actor": actor.actor_id,
                "after": after,
                "limit": limit,
                "v": 1,
                "expires": int(self.clock()) + 900,
            }
        ).encode()
        signature = hmac.digest(self.key, raw, hashlib.sha256)
        return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")

    def _after(self, actor: Actor, cursor: str, limit: int) -> str:
        try:
            if len(cursor) > 2048:
                raise ValueError
            value = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            signature, raw = value[:32], value[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, raw, hashlib.sha256)):
                raise ValueError
            data = json.loads(raw)
            if (
                set(data) != {"tenant", "actor", "after", "limit", "v", "expires"}
                or data["tenant"] != actor.tenant_id
                or data["actor"] != actor.actor_id
                or data["limit"] != limit
                or data["v"] != 1
                or type(data["expires"]) is not int
                or data["expires"] <= self.clock()
                or not isinstance(data["after"], str)
            ):
                raise ValueError
            return data["after"]
        except (ValueError, TypeError, KeyError):
            raise SchemaMismatch("Invalid or expired cursor; restart from the first page") from None

    def list(self, actor: Actor, limit: int = 25, cursor: str | None = None) -> dict[str, Any]:
        actor.require("ml:predict")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SchemaMismatch("Page limit must be between 1 and 100")
        after = self._after(actor, cursor, limit) if cursor else ""
        rows = self.repository.page(actor, after, limit + 1)
        more = len(rows) > limit
        items = rows[:limit]
        return {
            "observed_at": self.clock(),
            "items": items,
            "next_cursor": self._cursor(actor, items[-1]["endpoint_id"], limit) if more else None,
        }

    def get(self, actor: Actor, endpoint_id: str) -> dict[str, Any]:
        actor.require("ml:predict")
        return {**self.repository.detail(actor, endpoint_id), "observed_at": self.clock()}
