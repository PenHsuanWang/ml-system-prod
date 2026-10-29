"""Immutable model, evaluation and approval provenance."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelBinding:
    binding_id: str
    tenant_id: str
    attempt_id: str
    registered_name: str
    version: str
    run_id: str
    artifact_ref: str
    manifest_digest: str
    binding_digest: str


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    model_binding_id: str
    manifest_digest: str
    dataset_digest: str
    policy_version: str
    evaluator_version: str
    decision: str
    metrics_json: str
    report_digest: str


@dataclass(frozen=True)
class Approval:
    approval_id: str
    tenant_id: str
    model_binding_id: str
    evaluation_id: str
    report_digest: str
    binding_digest: str
    environment: str
    approver_id: str
