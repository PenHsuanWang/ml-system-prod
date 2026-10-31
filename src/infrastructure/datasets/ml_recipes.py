"""Explicitly approved first workload; caller input never becomes executable code."""

import json
import math
from typing import Any, Protocol

from src.domain.ml import Actor, canonical_json, MLError
from src.domain.datasets.models import DatasetVersion
from src.domain.datasets.ebm import EBM_DATASET_SCHEMA
from src.domain.training.ebm import EBM_RECIPE_ID, EBM_RECIPE_VERSION, EBMTrainingConfig


def _ebm_model_config(value: Any) -> dict[str, Any]:
    """Validate the complete approved CNN shape without loading model code."""
    defaults: dict[str, Any] = {"channels": [16, 32], "kernel_sizes": [[3, 3], [3, 3]],
        "strides": [[1, 1], [1, 1]], "features_per_pad": 4, "dropout": 0.1, "seed_size": [4, 4]}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise MLError("Unsupported EBM architecture configuration")
    config = {**defaults, **value}

    def integers(values: Any, low: int, high: int, count: int | None = None) -> bool:
        return (isinstance(values, list) and bool(values) and
                (count is None or len(values) == count) and
                all(type(item) is int and low <= item <= high for item in values))

    channels = config["channels"]
    if not integers(channels, 1, 128) or len(channels) > 4:
        raise MLError("Invalid EBM CNN channel configuration")
    for name, maximum in (("kernel_sizes", 7), ("strides", 4)):
        pairs = config[name]
        if (not isinstance(pairs, list) or len(pairs) != len(channels)
                or any(not integers(pair, 1, maximum, 2) for pair in pairs)):
            raise MLError("Invalid EBM CNN kernel or stride configuration")
    if (not integers(config["seed_size"], 1, 16, 2)
            or type(config["features_per_pad"]) is not int
            or not 1 <= config["features_per_pad"] <= 64
            or isinstance(config["dropout"], bool)
            or not isinstance(config["dropout"], (int, float))
            or not math.isfinite(config["dropout"]) or not 0 <= config["dropout"] < 1):
        raise MLError("Invalid EBM CNN projection or dropout configuration")
    return config


class DatasetResolver(Protocol):
    def resolve(self, actor: Actor, dataset_id: str, version: str) -> DatasetVersion: ...


class ApprovedRecipeResolver:
    def __init__(self, *, code_revision: str, image_digest: str, catalog: DatasetResolver,
                 ebm_evaluation_policy: dict[str, Any] | None = None) -> None:
        self.code_revision = code_revision
        self.image_digest = image_digest
        self.catalog = catalog
        # Configuration is supplied by Bootstrap, never by a request body.
        self.ebm_evaluation_policy = (json.loads(canonical_json(ebm_evaluation_policy))
                                      if ebm_evaluation_policy is not None else None)

    def resolve(self, intent_json: str, actor: Actor | None = None) -> str:
        value = json.loads(intent_json)
        allowed = {"schema_version", "recipe", "dataset", "model", "training",
                   "resource_profile_id", "retry_of_job_id"}
        if (not isinstance(value, dict) or set(value) - allowed
                or value.get("schema_version") != "1"):
            raise MLError("Unsupported training intent")
        is_ebm = value.get("recipe") == {"id": EBM_RECIPE_ID, "version": EBM_RECIPE_VERSION}
        if not is_ebm and value.get("recipe") != {"id": "nn_classifier", "version": "1.0.0"}:
            raise MLError("Recipe is not approved")
        if (not isinstance(value.get("dataset"), dict)
                or set(value["dataset"]) != {"id", "version"}):
            raise MLError("Training requires an approved dataset ID/version")
        dataset = self.catalog.resolve(actor or Actor("__public__", "recipe", frozenset()),
                                       value["dataset"]["id"], value["dataset"]["version"])
        value["dataset"].update({"tenant_id": dataset.tenant_id, "content_digest": dataset.content_digest,
                                 "snapshot_ref": dataset.snapshot_ref})
        summary = json.loads(dataset.summary_json)
        if is_ebm:
            return self._resolve_ebm(value, dataset, summary)
        if (summary.get("dataset_schema_version") == EBM_DATASET_SCHEMA
                or summary.get("feature_schema_version") != "1"):
            raise MLError("Dataset contract does not match the approved MLP recipe")
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

    def _resolve_ebm(self, value: dict[str, Any], dataset: DatasetVersion,
                     summary: dict[str, Any]) -> str:
        if (summary.get("dataset_schema_version") != EBM_DATASET_SCHEMA
                or summary.get("feature_schema_version") != "ebm-spectrogram-input-v1"):
            raise MLError("Dataset contract does not match the approved EBM recipe")
        if value.get("resource_profile_id") != "cpu-ebm-v1":
            raise MLError("Resource profile is not approved for EBM")
        model = value.get("model")
        if (not isinstance(model, dict) or set(model) != {"architecture", "config"}
                or model.get("architecture") != "ebm_cnn_v1"):
            raise MLError("Architecture is not approved for EBM")
        model["config"] = _ebm_model_config(model["config"])
        value["training"] = EBMTrainingConfig.from_dict(value.get("training", {})).to_dict()
        policy = self.ebm_evaluation_policy
        value.update({"code_revision": self.code_revision,
            "training_image_digest": self.image_digest, "workload_id": EBM_RECIPE_ID,
            "feature_schema_version": "ebm-spectrogram-input-v1",
            "input_schema_version": "ebm-spectrogram-input-v1",
            "split": {"strategy": "fixed_indices", "split_id": dataset.content_digest},
            "output_contract_version": "ebm-neural-output-v1",
            "evaluator_version": "ebm-neural-v1", "compatibility_id": "ebm-torch-cpu-v1",
            "artifact_profile": "ebm-neural-bundle-v1",
            "evaluation_policy": policy["policy_version"] if policy else "ebm-unqualified-v1",
            "evaluation_policy_config": policy, "request_normalization": "ml-json-v1"})
        return canonical_json(value)
