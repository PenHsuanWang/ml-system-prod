"""Explicit operations: migrate, local demo, relay and reconciliation/controller."""

import argparse
import asyncio
import json
import time
from pathlib import Path

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.ml_telemetry import configure_telemetry
from src.infrastructure.ml_tracing import operation
from src.application.deployments.service import DeploymentController
from src.application.training.outbox import OutboxRelay
from src.domain.ml import Actor, canonical_json, Conflict
from src.domain.training.execution import ExecutionHandle
from src.infrastructure.deployment.local_serving import LocalServingExecutor
from src.infrastructure.messaging.ml_rabbitmq import RabbitMQCommandPublisher
from src.infrastructure.persistence.ml_database import deployments
from src.infrastructure.persistence.ml_deployments import deployment_from_row
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


def migrate(settings: MLSettings) -> None:
    from alembic import command
    from alembic.config import Config
    if settings.database_url.startswith("sqlite:///"):
        Path(settings.database_url.removeprefix("sqlite:///")
             ).parent.mkdir(parents=True, exist_ok=True)
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")


async def relay(settings: MLSettings, once: bool) -> None:
    components = build_components(settings)
    publisher = RabbitMQCommandPublisher(
        settings.rabbitmq_url, settings.queue_name, settings.ack_timeout_ms)
    await publisher.start()
    service = OutboxRelay(SQLOutboxRepository(components.database), publisher)
    try:
        while True:
            await service.run_once()
            if once:
                return
            await asyncio.sleep(1)
    finally:
        await publisher.close()
        components.database.close()


def control(settings: MLSettings, once: bool) -> None:
    from sqlalchemy import or_, select
    components = build_components(settings)
    executor = LocalServingExecutor(settings.execution_root/"serving")
    controller = DeploymentController(components.deployments, executor)
    supervisor = components.supervisor()
    try:
        while True:
            components.training.expire_queued()
            for claim, ref in components.training.recoverable(settings.stale_after):
                try:
                    supervisor.monitor(claim, ExecutionHandle(**json.loads(ref)))
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Job reconciliation pending job=%s", claim.job.job_id)
            with components.database.transaction() as connection:
                rows = connection.execute(select(deployments).where(deployments.c.state.not_in(
                    ["RETIRED", "ACTIVE"]), or_(deployments.c.state != "FAILED",
                                                deployments.c.resources_released.is_(False)))).mappings().all()
            for row in rows:
                deployment = deployment_from_row(row)
                try:
                    with operation("ml.deployment.reconcile", attributes={
                        "ml.deployment_id": deployment.deployment_id, "ml.generation": deployment.generation,
                    }):
                        controller.reconcile(deployment)
                except Conflict:
                    if components.deployments.fence(deployment):
                        if executor.retire(deployment):
                            components.deployments.release(deployment)
            if once:
                return
            time.sleep(1)
    finally:
        components.database.close()


def demo(settings: MLSettings) -> None:
    """Real PyTorch + durable SQL + exact-bundle evaluation; no implicit approval."""
    if settings.environment != "dev":
        raise ValueError("The fixture demo is available only in development")
    from src.api.ml_schemas import TrainingIntent
    migrate(settings)
    components = build_components(settings)
    actor = Actor("demo", "demo-scientist", frozenset({"ml:train", "ml:model:read"}))
    intent = TrainingIntent(recipe={"id": "nn_classifier", "version": "1.0.0"},
                            dataset={"id": "dataset-demo", "version": "2026-10-28-v1"})
    from uuid import uuid4
    job = components.services.training.submit(
        actor, str(uuid4()), canonical_json(intent.model_dump()))
    items = SQLOutboxRepository(components.database).lease(100)
    command = next(item.command for item in items if item.command.job_id == job.job_id)
    components.supervisor().execute(command)
    final = components.training.get(job.job_id)
    print(json.dumps({"job_id": final.job_id, "state": final.state,
                      "model_binding_id": final.model_binding_id}))
    components.database.close()
    if final.state != "SUCCEEDED":
        raise RuntimeError("Demo training did not succeed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["migrate",
                        "demo", "relay", "control", "register-dataset"])
    parser.add_argument("--tenant-id")
    parser.add_argument("--dataset-id")
    parser.add_argument("--version")
    parser.add_argument("--source-path", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = MLSettings.from_env()
    configure_telemetry(settings.role)
    if args.operation == "migrate":
        migrate(settings)
    elif args.operation == "relay":
        asyncio.run(relay(settings, args.once))
    elif args.operation == "control":
        control(settings, args.once)
    elif args.operation == "register-dataset":
        if not all((args.tenant_id, args.dataset_id, args.version, args.source_path)):
            parser.error("Dataset import requires tenant, dataset ID, version and source path")
        from src.infrastructure.persistence.ml_datasets import SQLDatasetCatalog
        components = build_components(settings)
        actor = Actor(args.tenant_id, "dataset-operator", frozenset({"ml:dataset:manage"}))
        try:
            dataset = SQLDatasetCatalog(components.database, settings.dataset_root).register(
                actor, args.dataset_id, args.version, args.source_path)
            print(json.dumps({"dataset_id": dataset.dataset_id, "version": dataset.version,
                              "content_digest": dataset.content_digest}))
        finally:
            components.database.close()
    else:
        demo(settings)


if __name__ == "__main__":
    main()
