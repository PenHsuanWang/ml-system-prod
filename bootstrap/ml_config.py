"""Role settings are validated once, then passed as narrow constructor arguments."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MLSettings:
    role: str = "management"
    database_url: str = field(default="sqlite:///./var/ml.db", repr=False)
    artifact_root: Path = Path("var/artifacts")
    dataset_root: Path = Path("var/datasets")
    execution_root: Path = Path("var/executions")
    environment: str = "dev"
    tracker_backend: str = "local"
    tracking_uri: str = "http://localhost:5000"
    rabbitmq_url: str = field(default="amqp://guest:guest@localhost:5672/", repr=False)
    queue_name: str = "ml.training.v1"
    training_timeout: int = 300
    queue_timeout: int = 600
    cancel_grace: int = 5
    heartbeat_seconds: int = 2
    stale_after: int = 15
    retention_seconds: int = 86400
    max_pending: int = 100
    ack_timeout_ms: int = 1800000
    image_digest: str = "unqualified-development"
    code_revision: str = "development"

    @classmethod
    def from_env(cls) -> "MLSettings":
        values: dict[str, Any] = {}
        names = {"role": "ML_DEPLOYMENT_ROLE", "database_url": "ML_DATABASE_URL", "artifact_root": "ML_ARTIFACT_ROOT", "dataset_root": "ML_DATASET_ROOT",
                 "execution_root": "ML_EXECUTION_ROOT", "environment": "ML_MODEL_ENVIRONMENT",
                 "tracker_backend": "ML_TRACKER_BACKEND", "tracking_uri": "ML_TRACKING_URI",
                 "rabbitmq_url": "ML_RABBITMQ_URL", "queue_name": "ML_QUEUE_NAME",
                 "training_timeout": "ML_TRAINING_TIMEOUT_SECONDS",
                 "queue_timeout": "ML_QUEUE_TIMEOUT_SECONDS", "cancel_grace": "ML_CANCEL_GRACE_SECONDS",
                 "heartbeat_seconds": "ML_HEARTBEAT_SECONDS", "stale_after": "ML_STALE_AFTER_SECONDS",
                 "retention_seconds": "ML_IDEMPOTENCY_RETENTION_SECONDS",
                 "max_pending": "ML_MAX_PENDING_JOBS_PER_TENANT", "ack_timeout_ms": "ML_ACK_TIMEOUT_MS",
                 "image_digest": "ML_IMAGE_DIGEST", "code_revision": "ML_CODE_REVISION"}
        defaults = cls()
        for name, variable in names.items():
            if variable not in os.environ:
                continue
            raw = os.environ[variable]
            default = getattr(defaults, name)
            values[name] = int(raw) if isinstance(default, int) else Path(
                raw) if isinstance(default, Path) else raw
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        if self.role not in {"management", "relay", "supervisor", "runner", "control", "serving", "gateway"}:
            raise ValueError("Unknown ML deployment role")
        if self.environment not in {"dev", "staging", "prod"}:
            raise ValueError("Unknown ML environment")
        if self.tracker_backend not in {"local", "mlflow"}:
            raise ValueError("Unknown tracking backend")
        if min(self.training_timeout, self.queue_timeout, self.cancel_grace,
               self.heartbeat_seconds, self.stale_after, self.retention_seconds, self.max_pending) <= 0:
            raise ValueError("ML limits must be positive")
        if self.stale_after <= 2*self.heartbeat_seconds:
            raise ValueError("Stale threshold must exceed two heartbeats")
        if self.ack_timeout_ms <= 1000*(self.training_timeout+self.cancel_grace+120):
            raise ValueError("ACK timeout must cover bounded work and finalization margin")
        if self.environment != "dev" and (
            (self.role != "serving" and not self.database_url.startswith("postgresql")) or self.tracker_backend != "mlflow" or
            re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_digest) is None or
            re.fullmatch(r"(?:[a-f0-9]{40}|sha256:[a-f0-9]{64})", self.code_revision) is None
        ):
            raise ValueError(
                "Staging/production require PostgreSQL, MLflow and pinned source/image")
