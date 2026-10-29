"""Explicitly approved first workload; caller input never becomes executable code."""

import json
from typing import Protocol

from src.domain.ml import Actor, canonical_json, MLError
from src.domain.datasets.models import DatasetVersion


class DatasetResolver(Protocol):
    def resolve(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion: ...


class ApprovedRecipeResolver:
    def __init__(self, *, code_revision: str, image_digest: str, catalog: DatasetResolver) -> None:
        self.code_revision = code_revision
        self.image_digest = image_digest
        self.catalog = catalog

    def resolve(self, intent_json: str, actor: Actor | None = None) -> str:
        value = json.loads(intent_json)
        if value["recipe"] != {"id": "nn_classifier", "version": "1.0.0"}:
            raise MLError("Recipe is not approved")
        dataset = self.catalog.resolve(actor or Actor("__public__", "recipe", frozenset()),
                                       value["dataset"]["id"], value["dataset"]["version"])
        value["dataset"].update({"tenant_id": dataset.tenant_id, "content_digest": dataset.content_digest,
                                 "snapshot_ref": dataset.snapshot_ref})
        if value["resource_profile_id"] != "cpu-small-v1":
            raise MLError("Resource profile is not approved")
        if value["model"]["architecture"] != "mlp_v1":
            raise MLError("Architecture is not approved")
        value.update({"code_revision": self.code_revision,
                      "training_image_digest": self.image_digest, "feature_schema_version": "1",
                      "split": {"strategy": "fixed_indices", "split_id": dataset.content_digest},
                      "output_contract_version": "1", "evaluation_policy": "demo-accuracy-v1",
                      "request_normalization": "ml-json-v1"})
        return canonical_json(value)
