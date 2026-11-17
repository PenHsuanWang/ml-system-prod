"""Taskiq worker using manual ACK after durable business and transport evidence."""

import asyncio
import json
import logging
import os
import signal
import threading

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import operation
from src.shared_models.context import bind_job_context
from src.domain.training.execution import ExecutionHandle
from src.domain.training.models import TERMINAL, TrainingCommand
from src.infrastructure.messaging.ml_rabbitmq import TASK_NAME
from src.application.training.delivery import acknowledge_terminal
from src.domain.ml import Unavailable
from src.infrastructure.messaging.training_ack import RedisAcknowledgementStore


def build_broker():
    from taskiq import Context, TaskiqDepends, TaskiqEvents
    from src.infrastructure.messaging.ml_topology import build_training_broker
    from redis.asyncio import Redis
    settings = MLSettings.from_env()
    broker = build_training_broker(
        settings.rabbitmq_url, settings.queue_name, settings.ack_timeout_ms)
    state = {}
    draining = threading.Event()

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def startup(_):
        configure_telemetry("supervisor")
        components = build_components(settings)
        if settings.training_backend == "pod":
            # Taskiq installed its receiver handlers before WORKER_STARTUP.
            # Preserve them so admission stops while our fit thread drains.
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous = signal.getsignal(signum)
                def drain(signum, frame, previous=previous):
                    draining.set()
                    if callable(previous):
                        previous(signum, frame)
                signal.signal(signum, drain)
        snapshots = Redis.from_url(os.environ.get("ML_REDIS_URL", "redis://localhost:6379/0"),
                                   socket_timeout=5, socket_connect_timeout=5)
        state.update(components=components, supervisor=components.supervisor(draining=draining.is_set),
                     snapshots=snapshots, acknowledgement=RedisAcknowledgementStore(snapshots, settings.retention_seconds))

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def shutdown(_):
        draining.set()
        state["components"].database.close()
        await state["snapshots"].aclose()

    async def run(command: TrainingCommand, context: Context) -> dict:
        components = state["components"]
        if settings.training_backend == "pod":
            while True:
                job = await asyncio.to_thread(components.training.validate_delivery, command)
                if job.state in TERMINAL or await asyncio.to_thread(state["supervisor"].executor.capacity_available):
                    break
                if draining.is_set():
                    return {"state": "DRAINING", "command_id": command.command_id}
                await asyncio.sleep(1)
        try:
            await asyncio.to_thread(state["supervisor"].execute, command)
        except Exception:
            # Keep the delivery unacked; never let generic error serialization ACK it.
            logging.getLogger(__name__).warning(
                "Training requires reconciliation command=%s", command.command_id)
        while True:
            try:
                snapshot = await acknowledge_terminal(components.training, command,
                                                       state["acknowledgement"], context.ack)
                if snapshot is not None:
                    return snapshot
            except Unavailable:
                logging.getLogger(__name__).warning("Acknowledgement pending command=%s", command.command_id)
            if draining.is_set():
                # Manual mode leaves this delivery unacked; channel closure requeues it.
                return {"state": "DRAINING", "command_id": command.command_id}
            # Only Control recovers stale SQL claims in the Pod profile. A Worker
            # never steals another attempt to decide whether its delivery can ACK.
            recoveries = (() if settings.training_backend == "pod" else
                          await asyncio.to_thread(components.training.recoverable, settings.stale_after))
            for claim, ref in recoveries:
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
