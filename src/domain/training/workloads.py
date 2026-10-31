"""Approved, framework-free workload pairings shared by orchestration and bootstrap."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.domain.ml import MLError
from src.domain.training.ebm import EBM_RECIPE_ID, EBM_RECIPE_VERSION


@dataclass(frozen=True)
class WorkloadContract:
    workload_id: str
    evaluator_version: str
    compatibility_id: str
    input_schema_version: str
    output_schema_version: str
    artifact_profile: str


MLP_WORKLOAD = WorkloadContract("nn_classifier", "classifier-v1", "torch-cpu-v1",
                                "1", "1", "mlp-neural-bundle-v1")
EBM_WORKLOAD = WorkloadContract(EBM_RECIPE_ID, "ebm-neural-v1", "ebm-torch-cpu-v1",
    "ebm-spectrogram-input-v1", "ebm-neural-output-v1", "ebm-neural-bundle-v1")


def workload_for_spec(spec: Mapping[str, Any]) -> WorkloadContract:
    recipe = spec.get("recipe")
    if recipe == {"id": "nn_classifier", "version": "1.0.0"}:
        workload = MLP_WORKLOAD
    elif recipe == {"id": EBM_RECIPE_ID, "version": EBM_RECIPE_VERSION}:
        workload = EBM_WORKLOAD
    else:
        raise MLError("Unapproved workload recipe")
    fields = {"workload_id": workload.workload_id,
              "evaluator_version": workload.evaluator_version,
              "compatibility_id": workload.compatibility_id,
              "input_schema_version": workload.input_schema_version,
              "feature_schema_version": workload.input_schema_version,
              "output_contract_version": workload.output_schema_version,
              "artifact_profile": workload.artifact_profile}
    for name, expected in fields.items():
        if spec.get(name, expected if workload == MLP_WORKLOAD else None) != expected:
            raise MLError("Resolved workload components do not match the approved recipe")
    return workload
