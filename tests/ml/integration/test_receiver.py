"""Opt-in real Taskiq Receiver + RabbitMQ + Redis + SQL + PyTorch qualification."""

import asyncio
import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from src.domain.training.models import TERMINAL
from src.infrastructure.messaging.ml_rabbitmq import RabbitMQCommandPublisher
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("block_terminal_commit", [False, True])
async def test_real_receiver_persists_before_ack_and_deduplicates(
        components, actor, intent, tmp_path, block_terminal_commit):
    url = os.environ.get("ML_TEST_AMQP_URL")
    redis_url = os.environ.get("ML_TEST_REDIS_URL")
    if not url or not redis_url:
        pytest.skip("Set ML_TEST_AMQP_URL and ML_TEST_REDIS_URL for real receiver tests")
    pytest.importorskip("taskiq")
    pytest.importorskip("torch")
    from redis.asyncio import Redis
    queue = "ml-test-"+uuid4().hex
    job = components.services.training.submit(actor, "receiver", intent)
    if block_terminal_commit:
        set_terminal_write_barrier(components.database, enabled=True)
    item = SQLOutboxRepository(components.database).lease(1)[0]
    environment = {**os.environ, "ML_DATABASE_URL": components.settings.database_url,
                   "ML_ARTIFACT_ROOT": str(components.settings.artifact_root),
                   "ML_DATASET_ROOT": str(components.settings.dataset_root),
                   "ML_EXECUTION_ROOT": str(components.settings.execution_root),
                   "ML_TRACKER_BACKEND": "local", "ML_RABBITMQ_URL": url, "ML_REDIS_URL": redis_url,
                   "ML_QUEUE_NAME": queue, "ML_MODEL_ENVIRONMENT": "dev", "ML_STALE_AFTER_SECONDS": "15"}
    publisher = RabbitMQCommandPublisher(url, queue, components.settings.ack_timeout_ms)
    await publisher.start()
    with (tmp_path/"worker.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "taskiq", "worker",
                                    "bootstrap.ml_worker:broker", "--workers", "1", "--max-async-tasks", "1", "--ack-type", "manual"],
                                   env=environment, stdout=log, stderr=subprocess.STDOUT)
    snapshots = Redis.from_url(redis_url)
    try:
        await publisher.publish(item.command)
        if block_terminal_commit:
            deadline = time.monotonic()+60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Taskiq receiver exited before terminal persistence")
                if components.training.get(job.job_id).phase == "EVALUATE":
                    break
                await asyncio.sleep(0.1)
            assert components.training.get(job.job_id).phase == "EVALUATE"
            await asyncio.sleep(0.3)
            assert components.training.get(job.job_id).state not in TERMINAL
            assert await snapshots.get("ml:ack:"+item.command.command_id) is None
            set_terminal_write_barrier(components.database, enabled=False)
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Taskiq receiver exited; inspect worker.log")
            if components.training.get(job.job_id).state in TERMINAL:
                break
            await asyncio.sleep(0.1)
        assert components.training.get(job.job_id).state == "SUCCEEDED"
        for _ in range(100):
            snapshot = await snapshots.get("ml:ack:"+item.command.command_id)
            if snapshot:
                break
            await asyncio.sleep(0.05)
        assert json.loads(snapshot)["state"] == "SUCCEEDED"
        before = set(components.settings.execution_root.iterdir())
        await publisher.publish(item.command)
        await asyncio.sleep(0.5)
        assert set(components.settings.execution_root.iterdir()) == before
        assert len(components.training.attempts(job.job_id)) == 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        await snapshots.aclose()
        await publisher.close()


def set_terminal_write_barrier(database, *, enabled):
    """A real DB trigger rejects the final transaction until the test heals it."""
    with database.transaction() as connection:
        if connection.dialect.name == "sqlite":
            if enabled:
                connection.exec_driver_sql("""CREATE TRIGGER deny_terminal_write
                    BEFORE UPDATE OF state ON training_jobs WHEN NEW.state = 'SUCCEEDED'
                    BEGIN SELECT RAISE(ABORT, 'injected terminal persistence failure'); END""")
            else:
                connection.exec_driver_sql("DROP TRIGGER deny_terminal_write")
        elif enabled:
            connection.exec_driver_sql("""CREATE FUNCTION deny_terminal_write() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                IF NEW.state = 'SUCCEEDED' THEN RAISE EXCEPTION 'injected terminal persistence failure'; END IF;
                RETURN NEW; END $$""")
            connection.exec_driver_sql("""CREATE TRIGGER deny_terminal_write BEFORE UPDATE ON training_jobs
                FOR EACH ROW EXECUTE FUNCTION deny_terminal_write()""")
        else:
            connection.exec_driver_sql("DROP TRIGGER deny_terminal_write ON training_jobs")
            connection.exec_driver_sql("DROP FUNCTION deny_terminal_write()")
