from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionHandle:
    execution_id: str
    directory: str
    identity_json: str


@dataclass(frozen=True)
class ExecutionObservation:
    state: str
    exit_code: int | None = None


@dataclass(frozen=True)
class MetricSnapshot:
    step: int
    values: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class TrainingOutput:
    bundle_ref: str
    manifest_digest: str
    metrics: tuple[MetricSnapshot, ...]
    dataset_digest: str
