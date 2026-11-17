"""Pod-aware local compute with a durable guardian; remote observers never signal PIDs."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from src.domain.ml import Conflict, Unavailable, digest
from src.domain.training.execution import ExecutionHandle, ExecutionObservation
from src.infrastructure.execution.local_process import LocalProcessExecutor, process_identity
from src.infrastructure.execution.training_records import matches, publish, read


class PodProcessExecutor(LocalProcessExecutor):
    def __init__(
        self,
        root: Path,
        *,
        timeout: int,
        grace: int,
        dataset_root: Path,
        api,
        namespace: str,
        pod_name: str,
        pod_uid: str,
        container: str,
        worker: bool,
    ) -> None:
        super().__init__(root, timeout=timeout, grace=grace, dataset_root=dataset_root)
        self.api, self.namespace = api, namespace
        self.pod_name, self.pod_uid, self.container = pod_name, pod_uid, container
        self.worker = worker
        self.instance = str(uuid4())

    def pod_path(self, name: str) -> str:
        return f"/api/v1/namespaces/{self.namespace}/pods/{name}"

    def capacity_available(self) -> bool:
        marker = self.root / "active-training.json"
        try:
            identity = read(marker)
            UUID(identity["execution_id"])
            stopped = read(self.root / identity["execution_id"] / "stopped.json")
            return matches(stopped, identity) and stopped.get("confirmed") is True
        except FileNotFoundError:
            return not marker.exists()
        except (OSError, ValueError, KeyError):
            return False

    def prepare(self, spec_json: str, attempt_id: str, owner_token: str) -> ExecutionHandle:
        if not self.worker:
            raise Conflict("Control may observe training but cannot launch compute")
        pod = self.api.request("GET", self.pod_path(self.pod_name))
        if (
            not pod
            or pod["metadata"]["uid"] != self.pod_uid
            or pod["metadata"].get("deletionTimestamp")
        ):
            raise Unavailable("Supervisor Pod identity is unavailable")
        status = next(
            (
                c
                for c in pod.get("status", {}).get("containerStatuses", [])
                if c["name"] == self.container and "running" in c.get("state", {})
            ),
            None,
        )
        if not status or not status.get("containerID"):
            raise Unavailable("Supervisor container identity is unavailable")
        execution_id = str(uuid4())
        directory = self.root / execution_id
        directory.mkdir(mode=0o700)
        envelope = json.loads(spec_json)
        identity = {
            "schema_version": "pod-training-v1",
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "owner_token": owner_token,
            "spec_digest": digest(envelope),
            "namespace": self.namespace,
            "pod_name": self.pod_name,
            "pod_uid": self.pod_uid,
            "node": pod["spec"]["nodeName"],
            "container": self.container,
            "container_id": status["containerID"],
            "supervisor_instance": self.instance,
            "parent": process_identity(os.getpid()),
            "deadline": min(float(envelope["deadline"]), time.time() + self.timeout),
        }
        publish(directory / "spec.json", envelope, exclusive=True)
        publish(directory / "launch.json", identity, exclusive=True)
        return ExecutionHandle(execution_id, str(directory), json.dumps(identity, sort_keys=True))

    def identity(self, handle: ExecutionHandle) -> dict:
        UUID(handle.execution_id)
        expected = self.root / handle.execution_id
        if Path(handle.directory) != expected or expected.is_symlink():
            raise Conflict("Training execution directory identity mismatch")
        identity = json.loads(handle.identity_json)
        actual = read(expected / "launch.json")
        if (
            identity != actual
            or identity.get("schema_version") != "pod-training-v1"
            or identity.get("execution_id") != handle.execution_id
            or identity.get("namespace") != self.namespace
        ):
            raise Conflict("Training launch identity mismatch")
        return identity

    def launch(self, handle: ExecutionHandle) -> None:
        identity = self.identity(handle)
        if (
            not self.worker
            or identity["supervisor_instance"] != self.instance
            or identity["pod_uid"] != self.pod_uid
        ):
            raise Conflict("Only the original Worker may launch this execution")
        directory = Path(handle.directory)
        # Never repeat an uncertain side effect, even if the guardian is killed
        # between Popen and its own durable start record.
        publish(directory / "dispatch.json", identity, exclusive=True)
        environment = {
            key: os.environ[key]
            for key in ("PATH", "PYTHONPATH", "LANG", "LD_LIBRARY_PATH")
            if key in os.environ
        }
        environment.update(
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            ML_DATASET_ROOT=str(self.dataset_root),
        )
        try:
            with (directory / "guardian.log").open("wb") as stream:
                self._children[handle.execution_id] = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "bootstrap.ml_training_guardian",
                        "--directory",
                        str(directory),
                        "--cpu-seconds",
                        str(self.timeout),
                        "--grace",
                        str(self.grace),
                    ],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError:
            if handle.execution_id not in self._children:
                publish(
                    directory / "stopped.json",
                    {**identity, "confirmed": True, "exit_code": -1, "reason": "LAUNCH_FAILED"},
                )
            raise

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            identity = self.identity(handle)
            receipt = Path(handle.directory) / "stopped.json"
            if receipt.exists():
                value = read(receipt)
                if (
                    matches(value, identity)
                    and value.get("confirmed") is True
                    and type(value.get("exit_code")) is int
                ):
                    return ExecutionObservation("STOPPED", value["exit_code"], value.get("reason"))
                return ExecutionObservation("UNKNOWN")
            pod = self.api.request("GET", self.pod_path(identity["pod_name"]))
            if not pod or pod["metadata"]["uid"] != identity["pod_uid"]:
                return ExecutionObservation("UNKNOWN")
            # Status disappearance, restart, deletion or a different container
            # never constitutes proof of group termination. Retain uncertainty.
            status = next(
                (
                    c
                    for c in pod.get("status", {}).get("containerStatuses", [])
                    if c["name"] == identity["container"]
                ),
                {},
            )
            if (
                pod["metadata"].get("deletionTimestamp")
                or status.get("containerID") != identity["container_id"]
                or "running" not in status.get("state", {})
            ):
                return ExecutionObservation("UNKNOWN")
            marker = read(Path(handle.directory) / "guardian-start.json")
            if not matches(marker, identity):
                return ExecutionObservation("UNKNOWN")
            if identity["pod_uid"] == self.pod_uid:
                saved = marker["guardian"]
                actual = process_identity(saved["pid"])
                if (
                    actual is None
                    or actual.get("state") == "Z"
                    or any(
                        actual.get(k) != saved.get(k) for k in ("boot_id", "start_ticks", "host")
                    )
                ):
                    return ExecutionObservation("UNKNOWN")
            # Remote RUNNING means the original container still exists, not that
            # Control may touch its PID or infer a physical stop from a SQL lease.
            return ExecutionObservation("RUNNING")
        except (OSError, ValueError, KeyError, Unavailable):
            return ExecutionObservation("UNKNOWN")

    def stop(self, handle: ExecutionHandle) -> bool:
        identity = self.identity(handle)
        publish(Path(handle.directory) / "stop-request.json", identity)
        if not self.worker:
            return self.inspect(handle).state == "STOPPED"
        until = time.monotonic() + 2 * self.grace + 1
        while time.monotonic() < until:
            if self.inspect(handle).state == "STOPPED":
                return True
            time.sleep(0.05)
        return False

    def result(self, handle: ExecutionHandle):
        if self.inspect(handle).state != "STOPPED":
            return None
        return super().result(handle)
