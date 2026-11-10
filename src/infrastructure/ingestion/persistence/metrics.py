"""Bounded-label ingress gauges; source, tenant, job and credentials are never labels."""

import math
import time

from sqlalchemy import func, select

from src.domain.ingestion.states import TERMINAL, TRANSITIONS
from src.infrastructure.ingestion.persistence.schema import ingestion_attempts, ingestion_jobs
from src.infrastructure.persistence.ml_database import Database

_STATES = frozenset(TRANSITIONS) | TERMINAL | {"SUPERSEDED"}


class SQLIngestionMetricsReader:
    def __init__(self, database: Database) -> None:
        self.database = database

    def render(self, *, now: float | None = None) -> str:
        current = time.time() if now is None else now
        if not math.isfinite(current):
            raise ValueError("Metrics timestamp must be finite")
        lines = []
        with self.database.transaction() as connection:
            for table, name in (
                (ingestion_jobs, "ml_ingestion_jobs"),
                (ingestion_attempts, "ml_ingestion_attempts"),
            ):
                counts = {
                    state: count
                    for state, count in connection.execute(
                        select(table.c.state, func.count()).group_by(table.c.state)
                    )
                }
                lines.append(f"# TYPE {name} gauge")
                for state in sorted(_STATES):
                    lines.append(f'{name}{{state="{state}"}} {counts.get(state, 0)}')
                unknown = sum(count for state, count in counts.items() if state not in _STATES)
                lines.append(f'{name}{{state="UNKNOWN"}} {unknown}')
            exhausted = connection.scalar(
                select(func.count())
                .select_from(ingestion_jobs)
                .where(
                    ingestion_jobs.c.state == "NEEDS_RECONCILIATION",
                    ingestion_jobs.c.error_code == "RECONCILIATION_BUDGET_EXHAUSTED",
                )
            )
            oldest = connection.scalar(
                select(func.min(ingestion_jobs.c.created_at)).where(
                    ingestion_jobs.c.state.not_in(TERMINAL)
                )
            )
            age = (
                max(0.0, current - oldest) if oldest is not None and math.isfinite(oldest) else 0.0
            )
            lines.extend(
                [
                    "# TYPE ml_ingestion_reconciliation_exhausted gauge",
                    f"ml_ingestion_reconciliation_exhausted {exhausted}",
                    "# TYPE ml_ingestion_oldest_pending_age_seconds gauge",
                    f"ml_ingestion_oldest_pending_age_seconds {age:.3f}",
                ]
            )
        return "\n".join(lines) + "\n"
