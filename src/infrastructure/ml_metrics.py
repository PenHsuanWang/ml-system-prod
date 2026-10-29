"""Low-cardinality operational gauges; no tenant, job or input values in labels."""


from sqlalchemy import func, select

from src.infrastructure.persistence.ml_database import Database, jobs, outbox


class SQLMetricsReader:
    def __init__(self, database: Database) -> None:
        self.database = database

    def render(self) -> str:
        lines = ["# TYPE ml_training_jobs gauge"]
        with self.database.transaction() as connection:
            for state, count in connection.execute(select(jobs.c.state, func.count())
                                                   .group_by(jobs.c.state)):
                lines.append(f'ml_training_jobs{{state="{state}"}} {count}')
            count = connection.scalar(select(func.count()).select_from(outbox).where(
                outbox.c.state != "PUBLISHED"))
            lines.extend(["# TYPE ml_outbox_pending gauge", f"ml_outbox_pending {count}"])
        return "\n".join(lines)+"\n"
