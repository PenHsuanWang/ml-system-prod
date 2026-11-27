"""Read cases over the authoritative model binding and deployment records."""

import json
import math
from typing import Protocol

from src.application.deployments.service import DeploymentService, require_deployment_read
from src.application.model_registry.service import ModelRegistryService
from src.domain.ml import Actor, MLError, NotFound, Unavailable, digest
from src.domain.pagination import CollectionWindow
from src.domain.prediction.catalog import PublicModelContract, contract_document, reviewed_usage

ReadRow = dict[str, object]

PUBLIC_EVALUATION_METRICS = frozenset(
    {
        "accuracy",
        "samples",
        "lots",
        "cdf_anchor_smooth_l1",
        "lot_mean_raw_score_cdf_pearson",
        "quality_policy_configured",
    }
)


class GovernanceReader(Protocol):
    def bindings(
        self, actor: Actor, window: CollectionWindow, name: str | None, version: str | None
    ) -> list[ReadRow]: ...
    def count_versions(self, actor: Actor, name: str) -> int: ...
    def contract(self, actor: Actor, binding_id: str) -> ReadRow | None: ...
    def training_origin(self, actor: Actor, binding_id: str) -> ReadRow | None: ...
    def evaluations(
        self, actor: Actor, binding_id: str, window: CollectionWindow
    ) -> list[ReadRow]: ...
    def approvals(
        self, actor: Actor, binding_id: str, window: CollectionWindow
    ) -> list[ReadRow]: ...
    def deployments(
        self,
        actor: Actor,
        window: CollectionWindow,
        endpoint_id: str | None,
        state: str | None,
        environment: str | None,
    ) -> list[ReadRow]: ...
    def endpoints(self, actor: Actor, window: CollectionWindow) -> list[ReadRow]: ...
    def endpoint(self, actor: Actor, endpoint_id: str) -> ReadRow: ...
    def events(
        self, actor: Actor, deployment_id: str, window: CollectionWindow
    ) -> list[ReadRow]: ...


class GovernanceDiscoveryService:
    def __init__(
        self,
        reader: GovernanceReader,
        registry: ModelRegistryService,
        deployments: DeploymentService,
    ) -> None:
        self.reader, self.registry, self.deployment_service = reader, registry, deployments

    def bindings(
        self, actor: Actor, window: CollectionWindow, name: str | None, version: str | None
    ) -> list[ReadRow]:
        actor.require("ml:model:read")
        return self.reader.bindings(actor, window, name, version)

    def family(self, actor: Actor, name: str) -> ReadRow:
        actor.require("ml:model:read")
        count = self.reader.count_versions(actor, name)
        if count == 0:
            raise NotFound("Model family not found")
        return {"registered_name": name, "version_count": count}

    def contract(self, actor: Actor, binding_id: str) -> ReadRow:
        binding = self.registry.get(actor, binding_id)
        result: ReadRow = {
            "binding_id": binding_id,
            "manifest_digest": binding.manifest_digest,
            "status": "incomplete",
            "contract": None,
            "contract_digest": None,
            "usage": None,
            "usage_revision": None,
        }
        row = self.reader.contract(actor, binding_id)
        if row is None or row["manifest_digest"] != binding.manifest_digest:
            return result
        raw = row["document_json"]
        if not isinstance(raw, str):
            return result
        try:
            contract = PublicModelContract(binding_id, binding.manifest_digest, raw)
            if contract.contract_digest != row["contract_digest"]:
                return result
            result.update(contract=contract_document(raw), contract_digest=contract.contract_digest)
            revision, review = row["usage_revision"], row["review_json"]
            if type(revision) is int and isinstance(review, str) and len(review) <= 65536:
                usage = json.loads(review)
                if (
                    reviewed_usage(usage) == review
                    and digest([binding_id, revision, review]) == row["review_digest"]
                ):
                    result.update(status="complete", usage=usage, usage_revision=revision)
        except (ValueError, TypeError, MLError):
            pass  # Preserve independently verified technical metadata if the review is invalid.
        return result

    def lineage(self, actor: Actor, binding_id: str) -> ReadRow:
        binding = self.registry.get(actor, binding_id)
        result: ReadRow = {
            "binding_id": binding_id,
            "manifest_digest": binding.manifest_digest,
            "run_id": binding.run_id,
            "status": "incomplete",
            "training": None,
            "dataset": None,
        }
        row = self.reader.training_origin(actor, binding_id)
        if row is None:
            return result
        raw = row["spec_json"]
        if not isinstance(raw, str) or digest(["training-spec-v1", raw]) != row["spec_digest"]:
            return result
        try:
            spec = json.loads(raw)
            recipe = spec["recipe"]
            if not all(isinstance(recipe[key], str) and recipe[key] for key in ("id", "version")):
                return result
            result["training"] = {
                "job_id": row["job_id"],
                "attempt_id": binding.attempt_id,
                "attempt_number": row["attempt_number"],
                "spec_digest": row["spec_digest"],
                "recipe_id": recipe["id"],
                "recipe_version": recipe["version"],
            }
            if actor.permissions.intersection({"ml:train", "ml:dataset:read"}):
                dataset = spec["dataset"]
                keys = ("id", "version", "content_digest")
                if dataset.get("tenant_id") in {actor.tenant_id, "__public__"} and all(
                    isinstance(dataset[key], str) and dataset[key] for key in keys
                ):
                    result.update(status="complete", dataset={key: dataset[key] for key in keys})
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
        return result

    def evaluations(self, actor: Actor, binding_id: str, window: CollectionWindow) -> list[ReadRow]:
        self.registry.get(actor, binding_id)
        rows = self.reader.evaluations(actor, binding_id, window)
        for row in rows:
            # The recorded digest identifies the original evaluator report. This projection
            # omits report data, including the policy digest and replay semantics for EBM.
            raw = row.pop("metrics_json")
            try:
                if not isinstance(raw, str) or len(raw) > 65536:
                    raise ValueError
                metrics = json.loads(raw)
                if not isinstance(metrics, dict):
                    raise ValueError
                row["metrics"] = {
                    key: value
                    for key, value in metrics.items()
                    if key in PUBLIC_EVALUATION_METRICS
                    and (
                        value is None
                        or type(value) is bool
                        or type(value) in (int, float)
                        and math.isfinite(value)
                    )
                }
            except (ValueError, OverflowError):
                raise Unavailable("Stored model evaluation is unavailable") from None
        return rows

    def approvals(self, actor: Actor, binding_id: str, window: CollectionWindow) -> list[ReadRow]:
        self.registry.get(actor, binding_id)
        return self.reader.approvals(actor, binding_id, window)

    def deployments(
        self,
        actor: Actor,
        window: CollectionWindow,
        endpoint_id: str | None = None,
        state: str | None = None,
        environment: str | None = None,
    ) -> list[ReadRow]:
        require_deployment_read(actor)
        return self.reader.deployments(actor, window, endpoint_id, state, environment)

    def endpoints(self, actor: Actor, window: CollectionWindow) -> list[ReadRow]:
        require_deployment_read(actor)
        return self.reader.endpoints(actor, window)

    def endpoint(self, actor: Actor, endpoint_id: str) -> ReadRow:
        require_deployment_read(actor)
        return self.reader.endpoint(actor, endpoint_id)

    def history(self, actor: Actor, endpoint_id: str, window: CollectionWindow) -> list[ReadRow]:
        self.endpoint(actor, endpoint_id)
        return self.reader.deployments(actor, window, endpoint_id, None, None)

    def events(self, actor: Actor, deployment_id: str, window: CollectionWindow) -> list[ReadRow]:
        self.deployment_service.get(actor, deployment_id)
        return self.reader.events(actor, deployment_id, window)
