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
TRAINING_CHECKS = (
    "real_mlp_management_outbox_worker_and_manual_ack",
    "real_ebm_same_pod_runtime_contract",
    "cancel_confirms_compute_group_stop_before_ack",
    "guardian_wall_deadline_terminates_paused_compute",
    "worker_crash_control_recovers_same_fit_and_redelivery_ack",
    "redis_outage_preserves_terminal_sql_and_unacked_delivery",
    "normal_supervisor_pod_drain_confirms_stop_and_new_pod_identity",
)


def validate_checkpoint(previous, current):
    if not (
        previous.get("status") == "INCOMPLETE"
        and previous.get("namespace") == current.get("namespace") == "ml-k8s-acceptance"
        and previous.get("checks") == list(CHECKS[:5])
        and all(
            previous.get(key) == current.get(key)
            for key in ("source_revision", "runtime_image_digest", "tools_image")
        )
        and len(previous.get("jobs", [])) == 1
        and len(previous.get("deployments", [])) == 1
    ):
        raise ValueError("Only the verified early recovery checkpoint can resume")


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
    if (
        not re.fullmatch(r"[a-f0-9]{40}", source)
        or source == "0" * 40
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        or image.endswith("0" * 64)
    ):
        raise ValueError("Immutable source/image evidence is required")
    training = report.get("training_faults", {})
    if training.get("status") != "POD_TRAINING_RUNTIME_PASSED" or training.get("checks") != list(
        TRAINING_CHECKS
    ):
        raise ValueError("Complete Training fault evidence is required")
    if training.get("source_revision") != source or training.get("runtime_image_digest") != image:
        raise ValueError("Mixed-source Training qualification")
    transport = training.get("transport", {})
    if transport.get("ready") != 0 or transport.get("unacked") != 0:
        raise ValueError("Training transport has not drained")
    fault_jobs = training.get("jobs", [])
    if len(fault_jobs) != 7 or len({job["job_id"] for job in fault_jobs}) != 7:
        raise ValueError("Seven distinct real fault executions are required")
    for index, job in enumerate(fault_jobs):
        receipt = job.get("receipt") or {}
        state = {2: "CANCELLED", 3: "FAILED", 6: "FAILED"}.get(index, "SUCCEEDED")
        if (
            job.get("code_revision") != source
            or job.get("image_digest") != image
            or job.get("attempt_count") != 1
            or receipt.get("confirmed") is not True
            or not job.get("execution_id")
            or not job.get("pod_uid")
            or receipt.get("pod_uid") != job["pod_uid"]
            or job.get("owner_changed") is not (index == 4)
            or job.get("state") != state
            or (index == 3 and job.get("error_code") != "TRAINING_DEADLINE")
            or (index == 6 and job.get("error_code") != "SUPERVISOR_DRAIN")
        ):
            raise ValueError("Unconfirmed or mixed-source Training fault execution")
    snapshot_jobs = {snapshot["job_id"] for snapshot in transport.get("snapshots", [])}
    if not {job["job_id"] for job in fault_jobs} <= snapshot_jobs:
        raise ValueError("Each fault job requires its actual technical snapshot")
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
