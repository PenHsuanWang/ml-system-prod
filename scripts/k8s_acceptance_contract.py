"""Fail closed on incomplete or mixed-source engineering acceptance evidence."""

import re

CHECKS = (
    "synthetic_raw_snapshots_and_verified_dataset_builder",
    "http_idempotency_outbox_real_pod_fit_evaluation",
    "tenant_permission_and_explicit_approval_fences",
    "explicit_http_approval_native_activation_gateway_prediction",
    "serving_pod_and_active_resource_recovery_without_reactivation",
    "control_gateway_restart_preserves_durable_route",
    "real_quality_rejection_cannot_be_approved",
    "candidate_auth_failure_fenced_retired_active_route_preserved",
    "approved_update_and_rollback_with_confirmed_retirement",
    "ebm_raw_builder_worker_mlflow_evaluation_approval_native_inference",
    "two_global_serving_reservations_remain_enforced",
    "same_stack_training_faults_do_not_change_active_serving",
)


def validate(report):
    if report.get("status") != "K8S_ENGINEERING_ACCEPTANCE_PASSED":
        raise ValueError("Incomplete acceptance result")
    if (
        report.get("namespace") != "ml-k8s-acceptance"
        or report.get("production_accepted") is not False
    ):
        raise ValueError("Invalid engineering qualification scope")
    if report.get("checks") != list(CHECKS):
        raise ValueError("Required lifecycle or fault cases are missing")
    source, image = report.get("source_revision", ""), report.get("runtime_image_digest", "")
    if not re.fullmatch(r"[a-f0-9]{40}", source) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Immutable source/image evidence is required")
    training = report.get("training_faults", {})
    if (
        training.get("status") != "POD_TRAINING_RUNTIME_PASSED"
        or len(training.get("checks", [])) != 7
    ):
        raise ValueError("Complete Training fault evidence is required")
    if training.get("source_revision") != source or training.get("runtime_image_digest") != image:
        raise ValueError("Mixed-source Training qualification")
    transport = training.get("transport", {})
    if transport.get("ready") != 0 or transport.get("unacked") != 0:
        raise ValueError("Training transport has not drained")
    jobs = report.get("jobs", [])
    if len(jobs) != 4 or len({job["job_id"] for job in jobs}) != 4:
        raise ValueError("All four distinct lifecycle fits must execute")
    for job in jobs:
        if (
            job.get("state") != "SUCCEEDED"
            or job.get("attempt_count") != 1
            or job.get("stop_confirmed") is not True
            or job.get("code_revision") != source
            or job.get("image_digest") != image
        ):
            raise ValueError("Unconfirmed or mixed-source lifecycle execution")
