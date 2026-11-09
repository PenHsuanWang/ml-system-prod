"""Fault injection and concurrency checks for the Backend acceptance boundary."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from bootstrap.ml_api import create_app
from src.domain.ml import Conflict, MLError
from src.infrastructure.deployment.local_serving import LocalServingExecutor
from src.infrastructure.persistence.ml_database import attempts, jobs, keys, outbox
from src.infrastructure.persistence.ml_outbox import SQLOutboxRepository
from src.infrastructure.serving.bounded_prediction import BoundedPredictionExecutor
from src.infrastructure.serving.evaluation import BundleEvaluator
from tests.ml.test_api import Identity
from tests.ml.test_model_governance import evidence


@pytest.fixture
def deployment(components, actor, intent):
    binding, _, approval = evidence(components, actor, intent)
    return components.services.deployments.create(actor, "quality", binding.binding_id, approval.approval_id)


def test_concurrent_serving_prepare_creates_only_one_process(tmp_path, deployment, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    launches = []

    class Reservation:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            entered.set()
            assert release.wait(5)

        def getsockname(self):
            return ("127.0.0.1", 18001)

    def launch(*args, **kwargs):
        launches.append(args)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr("src.infrastructure.deployment.local_serving.socket.socket", Reservation)
    monkeypatch.setattr("src.infrastructure.deployment.local_serving.subprocess.Popen", launch)
    first, second = LocalServingExecutor(tmp_path), LocalServingExecutor(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.prepare, deployment)
        assert entered.wait(5)
        two = pool.submit(second.prepare, deployment)
        # First controller is paused before spawning; a second controller must not launch.
        time.sleep(0.05)
        release.set()
        one.result(5)
        two.result(5)
    assert len(launches) == 1


@pytest.mark.parametrize("scoped_credentials", [False, True])
def test_serving_child_preserves_tracking_uri_without_inheriting_parent_secrets(
        tmp_path, deployment, monkeypatch, scoped_credentials):
    import stat
    import subprocess
    import sys
    from pathlib import Path

    tracking_uri = "http://approved-tracker:5000"
    monkeypatch.setenv("ML_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    parent_home = tmp_path / "parent-home"
    (parent_home / ".aws").mkdir(parents=True)
    (parent_home / ".aws" / "credentials").write_text("private-parent-credentials")
    monkeypatch.setenv("HOME", str(parent_home))
    sensitive = {
        "MLFLOW_TRACKING_USERNAME": "parent-registry-user",
        "MLFLOW_TRACKING_PASSWORD": "parent-registry-password",
        "ML_DATABASE_URL": "private-management-database",
        "ML_RABBITMQ_URL": "private-training-broker",
        "AWS_ACCESS_KEY_ID": "private-artifact-writer",
        "AWS_SECRET_ACCESS_KEY": "private-artifact-writer-secret",
        "DATABRICKS_CLIENT_SECRET": "private-ingestion-secret",
    }
    for name, value in sensitive.items():
        monkeypatch.setenv(name, value)
    if scoped_credentials:
        monkeypatch.setenv("ML_SERVING_MLFLOW_USERNAME", "readonly-serving-user")
        monkeypatch.setenv("ML_SERVING_MLFLOW_PASSWORD", "readonly-serving-password")
    else:
        monkeypatch.delenv("ML_SERVING_MLFLOW_USERNAME", raising=False)
        monkeypatch.delenv("ML_SERVING_MLFLOW_PASSWORD", raising=False)
    launched = {}

    class Reservation:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            pass

        def getsockname(self):
            return ("127.0.0.1", 18001)

    def launch(command, **options):
        launched.update(options["env"])
        return SimpleNamespace(pid=123)

    with monkeypatch.context() as launch_patch:
        launch_patch.setattr("src.infrastructure.deployment.local_serving.socket.socket", Reservation)
        launch_patch.setattr("src.infrastructure.deployment.local_serving.subprocess.Popen", launch)
        LocalServingExecutor(tmp_path).prepare(deployment)

    assert launched["ML_TRACKING_URI"] == launched["MLFLOW_TRACKING_URI"] == tracking_uri
    assert not set(sensitive.values()) & set(launched.values())
    assert not {"ML_DATABASE_URL", "ML_RABBITMQ_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                "DATABRICKS_CLIENT_SECRET", "ML_SERVING_MLFLOW_USERNAME",
                "ML_SERVING_MLFLOW_PASSWORD"} & launched.keys()
    if scoped_credentials:
        assert launched["MLFLOW_TRACKING_USERNAME"] == "readonly-serving-user"
        assert launched["MLFLOW_TRACKING_PASSWORD"] == "readonly-serving-password"
    else:
        assert "MLFLOW_TRACKING_USERNAME" not in launched
        assert "MLFLOW_TRACKING_PASSWORD" not in launched
    runtime_home = Path(launched["HOME"])
    assert runtime_home == tmp_path / deployment.deployment_id / "home"
    assert stat.S_IMODE(runtime_home.stat().st_mode) == 0o700
    assert not (runtime_home / ".aws").exists()
    # Reproduce passwd lookup failure for an unknown container UID. Path.home
    # and SDK-style cache writes must work from the actual child environment.
    code = """
import json, pwd
from pathlib import Path
def unknown_uid(uid):
    raise KeyError(uid)
pwd.getpwuid = unknown_uid
runtime_home = Path.home()
cache = runtime_home / '.cache'
cache.mkdir()
(cache / 'probe').write_text('ready')
print(json.dumps({'home': str(runtime_home)}))
"""
    result = subprocess.run([sys.executable, "-c", code], env=launched,
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["home"] == str(runtime_home)
    assert (runtime_home / ".cache" / "probe").read_text() == "ready"


def test_serving_private_home_cannot_redirect_to_an_existing_directory(tmp_path, deployment, monkeypatch):
    import stat

    outside = tmp_path / "outside-home"
    outside.mkdir(mode=0o755)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    directory = tmp_path / deployment.deployment_id
    directory.mkdir()
    (directory / "home").symlink_to(outside, target_is_directory=True)

    def forbidden_launch(*args, **kwargs):
        pytest.fail("An existing home link must never launch a serving child")

    monkeypatch.setattr("src.infrastructure.deployment.local_serving.subprocess.Popen", forbidden_launch)
    with pytest.raises(Conflict):
        LocalServingExecutor(tmp_path).prepare(deployment)
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode
    assert not list(outside.iterdir())


def test_serving_requires_positive_readiness(tmp_path, deployment, monkeypatch):
    directory = tmp_path/deployment.deployment_id
    directory.mkdir()
    (directory /
     "instance.json").write_text(json.dumps({"address": "http://controlled", "created_at": time.time()}))
    monkeypatch.setattr("src.infrastructure.deployment.local_serving.httpx.get", lambda *args, **kwargs:
                        httpx.Response(200, json={"ready": False, "deployment_id": deployment.deployment_id,
                                                  "generation": deployment.generation, "manifest_digest": json.loads(deployment.spec_json)["manifest_digest"]}))
    assert not LocalServingExecutor(tmp_path).ready(deployment)


def test_failed_port_allocation_is_a_safe_closed_launch(tmp_path, deployment, monkeypatch):
    class DeniedSocket:
        def __enter__(self):
            raise PermissionError("injected port allocation failure")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("src.infrastructure.deployment.local_serving.socket.socket", DeniedSocket)
    executor = LocalServingExecutor(tmp_path)
    with pytest.raises(Conflict):
        executor.prepare(deployment)
    assert executor.retire(deployment)


def test_incomplete_serving_launch_times_out_without_claiming_stop(tmp_path, deployment):
    directory = tmp_path/deployment.deployment_id
    directory.mkdir()
    (directory/"launch.json").write_text('{}')
    executor = LocalServingExecutor(tmp_path, startup_timeout=0)
    with pytest.raises(Conflict):
        executor.ready(deployment)
    assert not executor.retire(deployment)


def test_dataset_failure_closes_the_evaluation_runtime(tmp_path):
    runtime = SimpleNamespace(close=lambda: closed.append(True))
    closed = []

    class BrokenDataset:
        def load(self, reference):
            raise MLError("Dataset digest mismatch")

    evaluator = BundleEvaluator(SimpleNamespace(materialize=lambda *args: tmp_path),
                                lambda *args: runtime, BrokenDataset())
    with pytest.raises(MLError):
        evaluator.evaluate(SimpleNamespace(artifact_ref="controlled", manifest_digest="a"*64),
                           '{"dataset":{"snapshot_ref":"controlled","content_digest":"invalid"}}')
    assert closed == [True]


@pytest.mark.asyncio
async def test_failed_acceptance_commit_rolls_back_and_same_key_can_recover(components, actor, intent):
    app = create_app(services=components.services, identity=Identity(actor))

    def fail_outbox(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO outbox_events"):
            raise OperationalError("injected outbox failure", {}, OSError("unavailable"))

    event.listen(components.database.engine, "before_cursor_execute", fail_outbox)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer owner", "Idempotency-Key": "recover-commit"}
        try:
            response = await client.post("/api/v1/training/jobs", headers=headers, json={"spec": json.loads(intent)})
            assert response.status_code == 503
            assert "outbox" not in response.text and "INSERT" not in response.text
        finally:
            event.remove(components.database.engine, "before_cursor_execute", fail_outbox)
        with components.database.transaction() as connection:
            for table in (jobs, attempts, keys, outbox):
                assert connection.scalar(select(func.count()).select_from(table)) == 0
        accepted = await client.post("/api/v1/training/jobs", headers=headers, json={"spec": json.loads(intent)})
        assert accepted.status_code == 202
        replay = await client.post("/api/v1/training/jobs", headers=headers, json={"spec": json.loads(intent)})
        assert replay.json()["job_id"] == accepted.json()["job_id"]


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED", "SUCCEEDED"])
def test_one_hundred_late_results_cannot_reopen_a_terminal_job(components, actor, intent, terminal):
    binding, evaluation, _ = evidence(components, actor, intent)
    command = SQLOutboxRepository(components.database).lease(1)[0].command
    claim = components.training.claim(command)
    if terminal == "SUCCEEDED":
        components.training.finish(claim, binding.binding_id, evaluation.evaluation_id)
    else:
        if terminal == "CANCELLED":
            components.services.training.cancel(actor, claim.job.job_id)
        components.training.stop(claim, terminal, confirmed=True)
    revision = components.training.get(claim.job.job_id).revision
    for _ in range(100):
        assert components.training.claim(command).already_complete
        with pytest.raises(Conflict):
            components.training.execution_started(claim, "stale-execution")
    assert components.training.get(claim.job.job_id).state == terminal
    assert components.training.get(claim.job.job_id).revision == revision


def test_repeated_prediction_failures_release_physical_slots():
    class FailingRuntime:
        def predict(self, values):
            raise ValueError("injected model failure")

        def close(self):
            pass

    executor = BoundedPredictionExecutor(FailingRuntime(), capacity=1)
    try:
        for _ in range(100):
            with pytest.raises(ValueError):
                executor.predict(((1.0, 1.0),))
    finally:
        executor.close()


@pytest.mark.parametrize("require_stack", [False, True])
def test_acceptance_collection_keeps_backend_services_and_separates_other_gates(tmp_path, require_stack):
    import os
    import subprocess

    from scripts import verify_ml_acceptance as runner

    result = subprocess.run(
        [*runner.pytest_command(tmp_path, require_stack=require_stack), "--collect-only"],
        cwd=runner.ROOT, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = result.stdout
    assert "test_live_backend_train_approve_predict_rollout_rollback_and_revoke" in collected
    assert "test_real_receiver_persists_before_ack_and_deduplicates" in collected
    assert "test_json_contract_rejects_nonfinite_duplicate_and_deep_values" in collected
    assert ("test_live_warehouse_postgres_and_independent_worker" in collected) is not require_stack
    assert ("test_worker_and_management_share_raw_storage_with_least_privilege_mounts" in collected) is not require_stack


def test_stack_acceptance_rejects_skipped_required_case_and_compiles_readonly_source(tmp_path, monkeypatch):
    import subprocess
    import sys
    from types import SimpleNamespace

    from scripts import verify_ml_acceptance as runner

    output = tmp_path / "acceptance"
    for name in ("ML_TEST_DATABASE_URL", "ML_TEST_AMQP_URL", "ML_TEST_REDIS_URL",
                 "ML_TEST_MANAGEMENT_URL", "ML_TEST_PREDICTION_URL"):
        monkeypatch.setenv(name, "isolated-test-endpoint")
    monkeypatch.setattr(sys, "argv", ["verify_ml_acceptance.py", "--require-stack",
                                      "--output-dir", str(output)])
    actual_run = subprocess.run
    environments = {}

    def successful_checks_with_skipped_required_case(command, **options):
        if command[1:3] == ["-m", "compileall"]:
            environments["compile"] = options["env"]
        if command[1:3] == ["-m", "pytest"]:
            (output / "junit.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="1">'
                '<testcase name="required_backend"><skipped message="service unavailable"/>'
                '</testcase></testsuite>'
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", successful_checks_with_skipped_required_case)
    assert runner.main() == 1
    report = json.loads((output / "result.json").read_text())
    assert report["functional_backend_accepted"] is False
    assert "Required suite contains skipped cases" in report["acceptance_blockers"]
    assert report["separate_qualifications"]

    source = tmp_path / "readonly-source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n")
    source.chmod(0o555)
    try:
        result = actual_run(
            [sys.executable, "-m", "compileall", "-q", str(source)],
            env=environments["compile"], capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (source / "__pycache__").exists()
        assert list((output / "pycache").rglob("module.*.pyc"))
    finally:
        source.chmod(0o755)
