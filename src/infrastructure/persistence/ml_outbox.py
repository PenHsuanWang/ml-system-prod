"""Leased publication with fencing; no SQL transaction held across AMQP I/O."""

import random
import time
from uuid import uuid4

from sqlalchemy import select, update

from src.domain.ml import Conflict
from src.domain.training.models import LeasedCommand, TrainingCommand
from src.infrastructure.persistence.ml_database import Database, jobs, outbox


class SQLOutboxRepository:
    def __init__(self, database: Database, *, lease_seconds: int = 30) -> None:
        self.db = database
        self.lease_seconds = lease_seconds

    def lease(self, limit: int) -> tuple[LeasedCommand, ...]:
        now = time.time()
        result = []
        with self.db.transaction() as c:
            rows = c.execute(select(outbox).where(outbox.c.state != "PUBLISHED",
                                                  outbox.c.state != "DEAD", outbox.c.next_retry_at <= now,
                                                  outbox.c.lease_until <= now).order_by(outbox.c.next_retry_at)
                             .limit(limit).with_for_update(skip_locked=True)).mappings().all()
            for row in rows:
                token = str(uuid4())
                changed = c.execute(update(outbox).where(outbox.c.id == row["id"],
                                                         outbox.c.lease_until <= now).values(lease_token=token,
                                                                                             lease_until=now+self.lease_seconds, tries=outbox.c.tries+1))
                if changed.rowcount == 1:
                    result.append(LeasedCommand(TrainingCommand(row["id"], row["job_id"],
                                  row["attempt_id"], row["spec_digest"], context_json=row["context_json"]), token))
        return tuple(result)

    def published(self, item: LeasedCommand) -> None:
        with self.db.transaction() as c:
            result = c.execute(update(outbox).where(outbox.c.id == item.command.command_id,
                                                    outbox.c.lease_token == item.lease_token).values(state="PUBLISHED", lease_until=0))
            if result.rowcount != 1:
                raise Conflict("Outbox publication lease expired")
            c.execute(update(jobs).where(jobs.c.id == item.command.job_id,
                                         jobs.c.state.in_(["ACCEPTED", "RETRY_WAIT"])).values(
                state="QUEUED", revision=jobs.c.revision+1))

    def retry(self, item: LeasedCommand) -> None:
        with self.db.transaction() as c:
            row = c.execute(select(outbox).where(outbox.c.id == item.command.command_id,
                                                 outbox.c.lease_token == item.lease_token)).mappings().first()
            if row is None:
                return
            c.execute(update(outbox).where(outbox.c.id == row["id"],
                                           outbox.c.lease_token == item.lease_token).values(
                state="DEAD" if row["tries"] >= 20 else "PENDING", lease_until=0,
                next_retry_at=time.time()+min(300, 2**min(row["tries"], 8))+random.random()))
