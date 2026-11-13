"""Approved, framework-free workload pairings shared by orchestration and bootstrap."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
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


MLP_WORKLOAD = WorkloadContract(
    "nn_classifier", "classifier-v1", "torch-cpu-v1", "1", "1", "mlp-neural-bundle-v1"
)
EBM_WORKLOAD = WorkloadContract(
    EBM_RECIPE_ID,
    "ebm-neural-v1",
    "ebm-torch-cpu-v1",
    "ebm-spectrogram-input-v1",
    "ebm-neural-output-v1",
    "ebm-neural-bundle-v1",
)


def _build_workload_registry(
    registrations: Iterable[tuple[tuple[str, str], WorkloadContract]],
) -> Mapping[tuple[str, str], WorkloadContract]:
    """Validate trusted code registrations before constructing the static mapping."""
    recipes: dict[tuple[str, str], WorkloadContract] = {}
    workloads: dict[str, WorkloadContract] = {}
    for recipe, workload in registrations:
        if (
            not isinstance(recipe, tuple)
            or len(recipe) != 2
            or any(not isinstance(value, str) or not value.strip() for value in recipe)
            or not isinstance(workload, WorkloadContract)
            or any(
                not isinstance(value, str) or not value.strip() for value in vars(workload).values()
            )
        ):
            raise MLError("Invalid approved workload registration")
        if recipe in recipes:
            raise MLError("Duplicate approved workload recipe")
        if workload.workload_id in workloads and workloads[workload.workload_id] != workload:
            raise MLError("Conflicting approved workload contract")
        recipes[recipe] = workload
        workloads[workload.workload_id] = workload
    return MappingProxyType(recipes)


APPROVED_WORKLOADS = _build_workload_registry(
    (
        (("nn_classifier", "1.0.0"), MLP_WORKLOAD),
        ((EBM_RECIPE_ID, EBM_RECIPE_VERSION), EBM_WORKLOAD),
    )
)


def workload_for_spec(spec: Mapping[str, Any]) -> WorkloadContract:
    recipe = spec.get("recipe")
    if (
        not isinstance(recipe, Mapping)
        or set(recipe) != {"id", "version"}
        or not isinstance(recipe["id"], str)
        or not isinstance(recipe["version"], str)
    ):
        raise MLError("Unapproved workload recipe")
    workload = APPROVED_WORKLOADS.get((recipe["id"], recipe["version"]))
    if workload is None:
        raise MLError("Unapproved workload recipe")
    fields = {
        "workload_id": workload.workload_id,
        "evaluator_version": workload.evaluator_version,
        "compatibility_id": workload.compatibility_id,
        "input_schema_version": workload.input_schema_version,
        "feature_schema_version": workload.input_schema_version,
        "output_contract_version": workload.output_schema_version,
        "artifact_profile": workload.artifact_profile,
    }
    for name, expected in fields.items():
        if spec.get(name, expected if workload == MLP_WORKLOAD else None) != expected:
            raise MLError("Resolved workload components do not match the approved recipe")
    return workload
