"""Taskiq worker using manual ACK after durable business and transport evidence."""

import asyncio
import json
import logging
import os

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import operation
from src.shared_models.context import bind_job_context
from src.domain.training.execution import ExecutionHandle
from src.domain.training.models import TERMINAL, TrainingCommand
from src.infrastructure.messaging.ml_rabbitmq import TASK_NAME


def build_broker():
    from taskiq import Context, TaskiqDepends, TaskiqEvents
    from src.infrastructure.messaging.ml_topology import build_training_broker
    from redis.asyncio import Redis
    settings = MLSettings.from_env()
    broker = build_training_broker(
        settings.rabbitmq_url, settings.queue_name, settings.ack_timeout_ms)
    state = {}

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def startup(_):
        configure_telemetry("supervisor")
        components = build_components(settings)
        state.update(components=components, supervisor=components.supervisor(),
                     snapshots=Redis.from_url(os.environ.get("ML_REDIS_URL", "redis://localhost:6379/0")))

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def shutdown(_):
        state["components"].database.close()
        await state["snapshots"].aclose()

    async def run(command: TrainingCommand, context: Context) -> dict:
        components = state["components"]
        try:
            await asyncio.to_thread(state["supervisor"].execute, command)
        except Exception:
            # Keep the delivery unacked; never let generic error serialization ACK it.
            logging.getLogger(__name__).warning(
                "Training requires reconciliation command=%s", command.command_id)
        while True:
            job = await asyncio.to_thread(components.training.get, command.job_id)
            if job.state in TERMINAL:
                snapshot = {"command_id": command.command_id, "job_id": job.job_id,
                            "state": job.state, "revision": job.revision}
                # A technical snapshot is an ACK prerequisite, never business truth.
                await state["snapshots"].set("ml:ack:"+command.command_id,
                                             json.dumps(snapshot), ex=settings.retention_seconds)
                await context.ack()
                return snapshot
            for claim, ref in await asyncio.to_thread(components.training.recoverable, settings.stale_after):
                try:
                    await asyncio.to_thread(state["supervisor"].monitor, claim, ExecutionHandle(**json.loads(ref)))
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Reconciliation pending job=%s", claim.job.job_id)
            await asyncio.sleep(2)

    @broker.task(task_name=TASK_NAME, ack_type="manual")
    async def execute(payload: dict, context: Context = TaskiqDepends()) -> dict:
        command = TrainingCommand(**payload)
        carrier = json.loads(command.context_json)
        with bind_job_context(command.job_id, carrier.get("x-request-id")), operation(
            "ml.training.execute", carrier=carrier, attributes={"ml.job_id": command.job_id,
                                                                "ml.attempt_id": command.attempt_id, "ml.command_id": command.command_id},
        ):
            return await run(command, context)
    return broker


# Explicit CLI factory avoids creating connections at import time.
broker = build_broker()
