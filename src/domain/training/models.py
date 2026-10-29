"""Training lifecycle and immutable, backend-independent execution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.domain.ml import Conflict


class JobState(str, Enum):
    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
JOB_TRANSITIONS = {
    JobState.ACCEPTED: {JobState.QUEUED, JobState.RUNNING, JobState.FAILED,
                        JobState.CANCEL_REQUESTED},
    JobState.QUEUED: {JobState.RUNNING, JobState.FAILED, JobState.CANCEL_REQUESTED},
    JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.RETRY_WAIT,
                       JobState.CANCEL_REQUESTED, JobState.NEEDS_RECONCILIATION},
    JobState.RETRY_WAIT: {JobState.QUEUED, JobState.RUNNING, JobState.FAILED,
                          JobState.CANCEL_REQUESTED},
    JobState.CANCEL_REQUESTED: {JobState.CANCELLED, JobState.NEEDS_RECONCILIATION},
    JobState.NEEDS_RECONCILIATION: {JobState.RUNNING, JobState.CANCEL_REQUESTED,
                                    JobState.RETRY_WAIT, JobState.SUCCEEDED,
                                    JobState.FAILED, JobState.CANCELLED},
}


def validate_transition(current: str, target: str) -> None:
    if JobState(target) not in JOB_TRANSITIONS.get(JobState(current), set()):
        raise Conflict(f"Illegal job transition: {current} -> {target}")


@dataclass(frozen=True)
class TrainingCommand:
    command_id: str
    job_id: str
    attempt_id: str
    spec_digest: str
    schema_version: str = "1"
    context_json: str = "{}"


@dataclass(frozen=True)
class LeasedCommand:
    command: TrainingCommand
    lease_token: str


@dataclass(frozen=True)
class Job:
    job_id: str
    tenant_id: str
    owner_id: str
    state: str
    phase: str
    revision: int
    attempt_id: str
    spec_json: str
    spec_digest: str
    created_at: float
    deadline: float
    cancel_requested: bool = False
    error_code: str | None = None
    model_binding_id: str | None = None


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    job_id: str
    number: int
    state: str
    owner_token: str | None
    execution_ref: str | None
    heartbeat_at: float | None
    phase: str
    revision: int


@dataclass(frozen=True)
class Claim:
    job: Job
    owner_token: str
    already_complete: bool = False
