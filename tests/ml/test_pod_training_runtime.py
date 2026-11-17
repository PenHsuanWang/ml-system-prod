"""Physical guardian, cross-Pod ownership and manual acknowledgement contracts."""

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import update

from src.application.training.delivery import acknowledge_terminal
from src.application.training.reconciliation import TrainingReconciler
from src.domain.ml import Conflict, Unavailable
from src.infrastructure.execution.pod_process import PodProcessExecutor
from src.infrastructure.execution.training_records import publish, read
from src.infrastructure.messaging.training_ack import RedisAcknowledgementStore
from src.infrastructure.persistence.ml_database import attempts
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository


class PodAPI:
    def __init__(self):
        self.uid = str(uuid4())
        self.unavailable = False
        self.deleted = False
        self.container_id = "containerd://original"
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        assert method == "GET" and "/pods/" in path
        if self.unavailable:
            raise Unavailable("API outage")
        if self.deleted:
            return None
        return {
            "metadata": {"uid": self.uid},
            "spec": {"nodeName": "node-one"},
            "status": {
                "containerStatuses": [
                    {
                        "name": "supervisor",
                        "containerID": self.container_id,
                        "state": {"running": {}},
                    }
                ]
            },
        }


@pytest.fixture
def native(tmp_path):
    api = PodAPI()
    return PodProcessExecutor(
        tmp_path / "executions",
        timeout=20,
        grace=1,
        dataset_root=tmp_path / "datasets",
        api=api,
        namespace="training-test",
        pod_name="worker",
        pod_uid=api.uid,
        container="supervisor",
        worker=True,
    )


def observer(native):
    return PodProcessExecutor(
        native.root,
        timeout=20,
        grace=1,
        dataset_root=native.dataset_root,
        api=native.api,
        namespace=native.namespace,
        pod_name="control",
        pod_uid=str(uuid4()),
        container="control",
        worker=False,
    )


def handle(native, deadline=None):
    return native.prepare(
        json.dumps({"deadline": deadline or time.time() + 20}), "attempt", "owner"
    )


def wait_for(check, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Runtime did not satisfy the required physical observation")


def guardian(native, execution, code):
    root = Path(execution.directory)
    command = [
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; "
        "from bootstrap.ml_training_guardian import supervise; "
        "raise SystemExit(supervise(Path(sys.argv[1]), grace=.2, "
        "runner=[sys.executable,'-c',sys.argv[2]]))",
        str(root),
        code,
    ]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@pytest.mark.parametrize("loss", ["deleted", "pod-uid", "container-id", "api"])
def test_pod_or_container_disappearance_never_confirms_death(native, loss):
    execution = handle(native)
    publish(
        Path(execution.directory) / "guardian-start.json",
        {**json.loads(execution.identity_json), "guardian": {"pid": 123}},
    )
    if loss == "deleted":
        native.api.deleted = True
    elif loss == "pod-uid":
        native.api.uid = str(uuid4())
    elif loss == "container-id":
        native.api.container_id = "containerd://restarted"
    else:
        native.api.unavailable = True
    assert observer(native).inspect(execution).state == "UNKNOWN"
    assert observer(native).stop(execution) is False


def test_remote_control_stops_same_process_group_without_signalling_remote_pid(native):
    execution = handle(native)
    process = guardian(native, execution, "import time; time.sleep(30)")
    try:
        wait_for(lambda: (Path(execution.directory) / "compute-start.json").exists())
        remote = observer(native)
        assert remote.inspect(execution).state == "RUNNING"
        assert remote.stop(execution) is False  # Bounded Control tick only requests a stop.
        wait_for(lambda: remote.inspect(execution).state == "STOPPED")
        process.wait(timeout=5)
        receipt = read(Path(execution.directory) / "stopped.json")
        assert receipt["confirmed"] is True and receipt["exit_code"] != 0
        assert receipt["execution_id"] == execution.execution_id
        assert all(method == "GET" for method, _ in native.api.calls)
    finally:
        process.kill() if process.poll() is None else None
        process.wait(timeout=5)


def test_guardian_deadline_is_wall_clock_and_independent_of_supervisor(native):
    execution = handle(native, time.time() + 0.4)
    process = guardian(native, execution, "import time; time.sleep(30)")
    process.wait(timeout=8)
    value = observer(native).inspect(execution)
    assert value.state == "STOPPED" and value.reason == "DEADLINE"
    assert value.exit_code != 0


def test_guardian_kill_retains_unknown_and_never_relaunches(native):
    execution = handle(native)
    # The immutable dispatch gate remains uncertain even before guardian startup.
    publish(
        Path(execution.directory) / "dispatch.json",
        json.loads(execution.identity_json),
        exclusive=True,
    )
    with pytest.raises(FileExistsError):
        native.launch(execution)
    assert observer(native).inspect(execution).state == "UNKNOWN"
    with pytest.raises(Conflict, match="original Worker"):
        observer(native).launch(execution)


def test_remote_pid_reuse_cannot_turn_missing_receipt_into_stopped(native, monkeypatch):
    execution = handle(native)
    publish(
        Path(execution.directory) / "guardian-start.json",
        {**json.loads(execution.identity_json), "guardian": {"pid": 1}},
    )
    monkeypatch.setattr(
        "src.infrastructure.execution.pod_process.process_identity",
        lambda pid: (_ for _ in ()).throw(AssertionError("Remote PID access")),
    )
    assert observer(native).inspect(execution).state == "RUNNING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pod_uid", "foreign"),
        ("attempt_id", "other"),
        ("owner_token", "stale"),
        ("spec_digest", "wrong"),
        ("confirmed", False),
        ("exit_code", True),
    ],
)
def test_stop_receipt_must_match_exact_physical_launch(native, field, value):
    execution = handle(native)
    record = {**json.loads(execution.identity_json), "confirmed": True, "exit_code": 0}
    record[field] = value
    publish(Path(execution.directory) / "stopped.json", record)
    assert observer(native).inspect(execution).state == "UNKNOWN"


def test_envelope_corruption_never_launches_compute(native):
    execution = handle(native)
    publish(Path(execution.directory) / "spec.json", {"deadline": 0, "foreign": True})
    process = guardian(native, execution, "raise AssertionError('must not execute')")
    process.wait(timeout=5)
    assert process.returncode != 0
    assert not (Path(execution.directory) / "compute-start.json").exists()
    assert observer(native).inspect(execution).state == "UNKNOWN"


@pytest.mark.torch
def test_real_pod_adapter_trains_once_and_terminal_redelivery_never_refits(
    components, actor, intent, native
):
    job = components.services.training.submit(actor, "pod-real", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    native.dataset_root = components.settings.dataset_root
    service = components.supervisor()
    service.executor = native
    service.execute(command)
    assert components.training.get(job.job_id).state == "SUCCEEDED"
    before = set(native.root.iterdir())
    service.execute(command)
    assert set(native.root.iterdir()) == before
    reference = components.training.attempts(job.job_id)[0].execution_ref
    identity = json.loads(json.loads(reference)["identity_json"])
    assert (
        identity["pod_uid"] == native.pod_uid
        and identity["container_id"] == native.api.container_id
    )
    directory = Path(json.loads(reference)["directory"])
    assert read(directory / "stopped.json")["confirmed"]


def test_control_keeps_one_fenced_claim_across_unknown_observations(
    components, actor, intent, native
):
    job = components.services.training.submit(actor, "unknown-control", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    old = components.training.claim(command)
    execution = handle(native)
    components.training.prepare_execution(old, json.dumps(asdict(execution)))
    with components.database.transaction() as connection:
        connection.execute(
            update(attempts).where(attempts.c.id == job.attempt_id).values(heartbeat_at=0)
        )
    service = components.supervisor()
    service.executor = observer(native)
    control = TrainingReconciler(components.training, service, 4)
    for _ in range(7):
        control.tick()
    assert len(control.claims) == 1
    assert components.training.get(job.job_id).state == "NEEDS_RECONCILIATION"
    assert components.training.get(job.job_id).error_code == "EXECUTION_UNCERTAIN"
    with pytest.raises(Conflict):
        components.training.heartbeat(old)
    with pytest.raises(Conflict):
        components.training.claim(command)


async def test_ack_requires_terminal_sql_then_redis_then_transport(components, actor, intent):
    job = components.services.training.submit(actor, "ack-order", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    events = []

    class Store:
        async def persist(self, snapshot):
            assert components.training.get(job.job_id).state == "CANCELLED"
            events.append("redis")

    async def ack():
        events.append("ack")

    assert await acknowledge_terminal(components.training, command, Store(), ack) is None
    assert events == []
    components.services.training.cancel(actor, job.job_id)
    result = await acknowledge_terminal(components.training, command, Store(), ack)
    assert events == ["redis", "ack"] and result["attempt_id"] == job.attempt_id
    with pytest.raises(Conflict):
        await acknowledge_terminal(
            components.training, replace(command, spec_digest="foreign"), Store(), ack
        )


@pytest.mark.parametrize("outcome", [False, "unavailable"])
async def test_redis_failure_never_acks_durable_terminal_result(components, actor, intent, outcome):
    job = components.services.training.submit(actor, "ack-failure", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    components.services.training.cancel(actor, job.job_id)

    async def set_value(*args, **kwargs):
        if outcome == "unavailable":
            raise OSError("test outage")
        return False

    async def ack():
        raise AssertionError("ACK before technical evidence")

    with pytest.raises(Unavailable):
        await acknowledge_terminal(
            components.training,
            command,
            RedisAcknowledgementStore(SimpleNamespace(set=set_value), 10),
            ack,
        )


def test_drain_before_claim_never_creates_attempt_or_compute(components, actor, intent):
    job = components.services.training.submit(actor, "draining", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    with pytest.raises(Unavailable, match="draining"):
        components.supervisor(draining=lambda: True).execute(command)
    assert components.training.get(job.job_id).state == "ACCEPTED"
    assert components.training.attempts(job.job_id)[0].state == "PENDING"


@pytest.mark.parametrize("action", ["cancel", "drain"])
def test_active_compute_stops_before_cancel_or_drain_becomes_terminal(
    components, actor, intent, native, action
):
    import signal
    import threading
    from concurrent.futures import ThreadPoolExecutor

    job = components.services.training.submit(actor, "stop-" + action, intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    draining = threading.Event()
    service = components.supervisor(draining=draining.is_set)
    service.executor = native
    native.dataset_root = components.settings.dataset_root
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.execute, command)
        marker = wait_for(lambda: next(iter(native.root.glob("*/compute-start.json")), None))
        process = read(marker)["process"]
        os.killpg(process["pid"], signal.SIGSTOP)
        if action == "cancel":
            components.services.training.cancel(actor, job.job_id)
        else:
            draining.set()
        future.result(timeout=12)
    final = components.training.get(job.job_id)
    assert final.state == ("CANCELLED" if action == "cancel" else "FAILED")
    assert final.error_code == ("CANCELLED" if action == "cancel" else "SUPERVISOR_DRAIN")
    assert read(marker.parent / "stopped.json")["confirmed"] is True


def test_launch_exception_without_stop_confirmation_retains_reconciliation(
    components, actor, intent
):
    from src.domain.training.execution import ExecutionHandle

    job = components.services.training.submit(actor, "uncertain-launch", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    service = components.supervisor()

    class UncertainExecutor:
        def prepare(self, *args):
            return ExecutionHandle("execution", "/uncertain", "{}")

        def launch(self, handle):
            raise OSError("Interrupted dispatch")

        def stop(self, handle):
            return False

    service.executor = UncertainExecutor()
    with pytest.raises(Unavailable):
        service.execute(command)
    assert components.training.get(job.job_id).state == "NEEDS_RECONCILIATION"


def test_durable_training_slot_never_admits_a_second_fit(native):
    first = handle(native)
    process = guardian(native, first, "import time; time.sleep(30)")
    try:
        wait_for(lambda: (Path(first.directory) / "compute-start.json").exists())
        assert native.capacity_available() is False
        second = handle(native)
        denied = guardian(native, second, "raise AssertionError('second fit')")
        denied.wait(timeout=5)
        assert read(Path(second.directory) / "stopped.json")["reason"] == "CAPACITY_UNCERTAIN"
        assert not (Path(second.directory) / "compute-start.json").exists()
        observer(native).stop(first)
        wait_for(native.capacity_available)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("collision", [True, False])
def test_tracker_startup_reconciles_only_named_experiment_collision(monkeypatch, collision):
    import mlflow
    from mlflow.exceptions import MlflowException
    from mlflow.protos.databricks_pb2 import PERMISSION_DENIED, RESOURCE_ALREADY_EXISTS

    from src.infrastructure.mlflow.tracking import MLflowExperimentTracker

    reads = []

    def get(name):
        reads.append(name)
        return None if len(reads) == 1 else SimpleNamespace(experiment_id="winner")

    def create(name):
        raise MlflowException(
            "safe test failure",
            error_code=(RESOURCE_ALREADY_EXISTS if collision else PERMISSION_DENIED),
        )

    client = SimpleNamespace(get_experiment_by_name=get, create_experiment=create)
    monkeypatch.setattr(mlflow, "MlflowClient", lambda **kwargs: client)
    if collision:
        assert MLflowExperimentTracker("https://tracking.example").experiment_id == "winner"
        assert reads == ["ml-training", "ml-training"]
    else:
        with pytest.raises(MlflowException):
            MLflowExperimentTracker("https://tracking.example")
        assert reads == ["ml-training"]


def test_expired_launch_records_deadline_without_starting_compute(native):
    execution = handle(native, time.time() - 1)
    process = guardian(native, execution, "raise AssertionError('expired compute')")
    process.wait(timeout=8)
    value = observer(native).inspect(execution)
    assert value.state == "STOPPED" and value.reason == "DEADLINE"
    assert not (Path(execution.directory) / "compute-start.json").exists()
    assert native.capacity_available()


def test_slow_postprocessing_renews_ownership_without_another_fit(
    components, actor, intent, monkeypatch
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    clock = [1000.0]
    monkeypatch.setattr("src.infrastructure.persistence.ml_training.time.time", lambda: clock[0])
    job = components.services.training.submit(actor, "postprocess-lease", intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    claim = components.training.claim(command)
    service = components.supervisor()
    service.heartbeat = 0.05
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    original_heartbeat = components.training.heartbeat

    def heartbeat(claim):
        value = original_heartbeat(claim)
        if clock[0] >= 1002 and threading.current_thread().name == "training-finalization-lease":
            renewed.set()
        return value

    monkeypatch.setattr(components.training, "heartbeat", heartbeat)

    def postprocess(claim, result):
        entered.set()
        assert release.wait(8)

    service._finalize = postprocess
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(service.finalize, claim, None)
        assert entered.wait(3)
        clock[0] += 2
        try:
            assert renewed.wait(3)
            assert components.training.recoverable(1) == ()
            assert components.training.attempts(job.job_id)[0].owner_token == claim.owner_token
        finally:
            release.set()
        task.result(timeout=5)


def test_own_guardian_observation_does_not_poll_api_or_depend_on_api_outage(native):
    execution = handle(native)
    process = guardian(native, execution, "import time; time.sleep(30)")
    try:
        wait_for(lambda: (Path(execution.directory) / "compute-start.json").exists())
        before = len(native.api.calls)
        native.api.unavailable = True
        assert native.inspect(execution).state == "RUNNING"
        assert len(native.api.calls) == before
        assert observer(native).inspect(execution).state == "UNKNOWN"
        assert native.stop(execution) is True
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
