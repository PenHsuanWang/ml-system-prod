"""Control retains a fenced recovery claim across bounded observations."""

import json
from typing import Protocol

from src.application.training.supervisor import TrainingSupervisor
from src.domain.ml import Conflict
from src.domain.training.execution import ExecutionHandle
from src.domain.training.models import Claim


class RecoveryRepository(Protocol):
    def recoverable(self, stale_after: int) -> tuple[tuple[Claim, str], ...]: ...


class TrainingReconciler:
    def __init__(
        self, repository: RecoveryRepository, supervisor: TrainingSupervisor, stale_after: int
    ) -> None:
        self.repository = repository
        self.supervisor = supervisor
        self.stale_after = stale_after
        self.claims: dict[str, tuple[Claim, ExecutionHandle]] = {}

    def tick(self) -> None:
        for claim, reference in self.repository.recoverable(self.stale_after):
            self.claims[claim.job.job_id] = claim, ExecutionHandle(**json.loads(reference))
        for job_id, (claim, handle) in tuple(self.claims.items()):
            try:
                if self.supervisor.reconcile_once(claim, handle):
                    del self.claims[job_id]
            except Conflict:
                # A terminal result or a newer SQL owner revokes this observation.
                del self.claims[job_id]
            except Exception:
                # External finalization failures retain the same fit and claim.
                import logging

                logging.getLogger(__name__).warning("Training recovery pending job=%s", job_id)
