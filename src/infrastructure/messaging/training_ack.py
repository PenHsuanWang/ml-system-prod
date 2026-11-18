"""Redis snapshots are an ACK prerequisite, never the authority for job state."""

import json
from dataclasses import asdict

from src.domain.ml import Unavailable


class RedisAcknowledgementStore:
    def __init__(self, redis, retention: int) -> None:
        self.redis, self.retention = redis, retention

    async def persist(self, snapshot) -> None:
        try:
            accepted = await self.redis.set(
                "ml:ack:" + snapshot.command_id, json.dumps(asdict(snapshot)), ex=self.retention
            )
            if accepted is not True:
                raise Unavailable("Acknowledgement evidence was not persisted")
        except Exception:
            raise Unavailable("Acknowledgement evidence is unavailable") from None
