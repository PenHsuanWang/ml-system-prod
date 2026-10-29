"""Publisher confirms plus mandatory returns; stable Taskiq-compatible command IDs."""

import json
from dataclasses import asdict

from src.domain.ml import Unavailable
from src.domain.training.models import TrainingCommand
from src.infrastructure.messaging.ml_topology import build_training_broker
from src.infrastructure.ml_tracing import operation, capture_context

TASK_NAME = "ml.training.execute.v1"


class RabbitMQCommandPublisher:
    def __init__(self, url: str, queue_name: str, ack_timeout_ms: int) -> None:
        self.url, self.queue_name, self.ack_timeout_ms = url, queue_name, ack_timeout_ms
        self.connection = None
        self.channel = None
        self.exchange = None

    async def start(self) -> None:
        self.topology = build_training_broker(self.url, self.queue_name, self.ack_timeout_ms)
        await self.topology.startup()
        self.connection = self.topology.write_conn
        self.channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
        self.exchange = await self.channel.get_exchange(self.queue_name+".exchange")

    async def publish(self, command: TrainingCommand) -> None:
        with operation("ml.command.publish", carrier=json.loads(command.context_json), attributes={
            "ml.command_id": command.command_id, "ml.job_id": command.job_id,
        }):
            await self._publish(command)

    async def _publish(self, command: TrainingCommand) -> None:
        import aio_pika
        from pamqp.commands import Basic
        if self.exchange is None:
            raise Unavailable("Command publisher is not connected")
        payload = {**asdict(command), "context_json": json.dumps(capture_context())}
        message = {"task_id": command.command_id, "task_name": TASK_NAME,
                   "labels": {"ack_type": "manual", "queue_name": self.queue_name},
                   "args": [payload], "kwargs": {}}
        confirmation = await self.exchange.publish(aio_pika.Message(
            json.dumps(message).encode(), content_type="application/json",
            message_id=command.command_id, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=self.queue_name, mandatory=True, timeout=10)
        if not isinstance(confirmation, Basic.Ack):
            raise Unavailable("Command publication was not positively confirmed")

    async def close(self) -> None:
        if self.connection is not None:
            await self.topology.shutdown()
