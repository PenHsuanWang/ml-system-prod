"""Local/VM rollout adapter; every generation gets a separate serving process."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from src.domain.deployments.models import Deployment
from src.domain.ml import canonical_json, Conflict
from src.infrastructure.execution.local_process import process_identity


class LocalServingExecutor:
    def __init__(self, root: Path, *, startup_timeout: int = 60) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.startup_timeout = startup_timeout
        self._processes = {}

    def prepare(self, deployment: Deployment) -> None:
        directory = self.root/deployment.deployment_id
        directory.mkdir(mode=0o700, exist_ok=True)
        spec = directory/"deployment.json"
        if spec.exists():
            return
        # Exclusive intent spans controllers sharing the local/VM execution store.
        # An interrupted intent stays uncertain; recovery must never spawn twice.
        try:
            with (directory/"launch.json").open("x") as stream:
                stream.write(canonical_json({"created_at": time.time(),
                                             "parent": process_identity(os.getpid()), "spec_digest": deployment.spec_digest}))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return
        try:
            self._launch(deployment, directory, spec)
        except OSError:
            if deployment.deployment_id not in self._processes:
                self._persist(directory/"launch-failed", "No child was created")
            raise Conflict("Serving process could not be launched") from None

    @staticmethod
    def _persist(path: Path, data: str) -> None:
        temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
        try:
            with temporary.open("x") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _launch(self, deployment: Deployment, directory: Path, spec: Path) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        # Launch intent precedes the side effect. Recovery never blindly creates a second server.
        self._persist(directory/"instance.json", canonical_json({"address": f"http://127.0.0.1:{port}",
                                                                 "created_at": time.time(), "parent": process_identity(os.getpid())}))
        self._persist(spec, deployment.spec_json)
        permitted = {"PATH", "PYTHONPATH", "LANG", "LD_LIBRARY_PATH", "ML_ARTIFACT_ROOT",
                     "ML_TRACKING_URI", "ML_TRACKER_BACKEND", "ML_MODEL_ENVIRONMENT", "ML_IMAGE_DIGEST",
                     "ML_CODE_REVISION", "APP_MONGO_URI", "APP_MONGO_DATABASE", "APP_JWT_SECRET",
                     "APP_JWT_ISSUER", "APP_JWT_AUDIENCE", "ML_PREDICTION_TIMEOUT_SECONDS",
                     "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"}
        environment = {key: value for key, value in os.environ.items() if key in permitted}
        for source, target in (("ML_SERVING_MLFLOW_USERNAME", "MLFLOW_TRACKING_USERNAME"),
                               ("ML_SERVING_MLFLOW_PASSWORD", "MLFLOW_TRACKING_PASSWORD")):
            if source in os.environ:
                environment[target] = os.environ[source]
        environment.update(ML_DEPLOYMENT_SPEC_PATH=str(spec),
                           ML_DEPLOYMENT_SPEC_DIGEST=deployment.spec_digest,
                           ML_DEPLOYMENT_ROLE="serving",
                           ML_SERVING_EXECUTION_DIRECTORY=str(directory),
                           OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
        with (directory/"serving.log").open("wb") as stream:
            self._processes[deployment.deployment_id] = subprocess.Popen(
                [sys.executable, "-m", "bootstrap.ml_serving", "--port", str(port)],
                env=environment, stdin=subprocess.DEVNULL, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=True)

    def ready(self, deployment: Deployment) -> bool:
        directory = self.root/deployment.deployment_id
        instance = directory/"instance.json"
        if (directory/"launch-failed").exists():
            raise Conflict("Serving launch failed before readiness")
        if not instance.exists():
            intent = directory/"launch.json"
            if intent.exists() and time.time()-intent.stat().st_mtime > self.startup_timeout:
                raise Conflict("Serving launch intent requires reconciliation")
            return False
        metadata = json.loads(instance.read_text())
        try:
            response = httpx.get(metadata["address"]+"/health/ready", timeout=2)
            actual = response.json()
            if response.status_code == 200 and actual.get("ready") is True and actual.get("deployment_id") == deployment.deployment_id and (
                actual.get("generation") == deployment.generation and
                actual.get("manifest_digest") == json.loads(deployment.spec_json)["manifest_digest"]
            ):
                return True
        except (httpx.HTTPError, ValueError):
            pass
        if time.time()-metadata["created_at"] > self.startup_timeout:
            raise Conflict("Serving startup did not become ready")
        return False

    def address(self, deployment_id: str) -> str:
        return json.loads((self.root/deployment_id/"instance.json").read_text())["address"]

    def retire(self, deployment: Deployment) -> bool:
        directory = self.root/deployment.deployment_id
        marker = directory/"process.json"
        if not marker.exists():
            if (directory/"launch-failed").exists():
                return True
            process = self._processes.get(deployment.deployment_id)
            if process is None:
                return False
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            return True
        try:
            saved = json.loads(marker.read_text())
            if not isinstance(saved, dict) or not {"host", "pid", "boot_id", "start_ticks"} <= saved.keys():
                return False
        except (OSError, ValueError):
            return False
        if saved.get("host") != socket.gethostname():
            return False
        actual = process_identity(saved["pid"])
        if actual is None or actual.get("state") == "Z" or any(actual.get(k) != saved.get(k) for k in ("host", "boot_id", "start_ticks")):
            return True
        try:
            os.killpg(saved["pid"], signal.SIGTERM)
        except ProcessLookupError:
            return True
        # Uvicorn drains in-flight requests before releasing the model.
        process = self._processes.get(deployment.deployment_id)
        if process is not None:
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            return True
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            actual = process_identity(saved["pid"])
            if actual is None or actual.get("start_ticks") != saved["start_ticks"] or actual["state"] == "Z":
                return True
            time.sleep(0.1)
        return False
