"""Stable per-execution records and process-group termination with PID reuse checks."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from src.domain.ml import canonical_json
from src.domain.training.execution import ExecutionHandle, ExecutionObservation, MetricSnapshot, TrainingOutput


def process_identity(pid: int) -> dict[str, object] | None:
    try:
        # comm can contain spaces or parentheses; fields after the final ')' are stable.
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": tail[19], "state": tail[0],
                "host": socket.gethostname(),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (FileNotFoundError, ProcessLookupError):
        return None


class LocalProcessExecutor:
    def __init__(self, root: Path, *, timeout: int, grace: int, dataset_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.grace = grace
        self.dataset_root = (dataset_root or root.parent/"datasets").resolve()
        self._children: dict[str, subprocess.Popen[bytes]] = {}

    def prepare(self, spec_json: str, attempt_id: str, owner_token: str) -> ExecutionHandle:
        execution_id = str(uuid4())
        directory = self.root/execution_id
        directory.mkdir(mode=0o700)
        (directory/"spec.json").write_text(spec_json)
        identity = {"execution_id": execution_id, "attempt_id": attempt_id,
                    "owner_token": owner_token, "parent": process_identity(os.getpid())}
        (directory/"launch.json").write_text(canonical_json(identity))
        return ExecutionHandle(execution_id, str(directory), canonical_json(identity))

    def launch(self, handle: ExecutionHandle) -> None:
        directory = Path(handle.directory)
        # No secrets/DB credentials are inherited by the compute child.
        environment = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "LANG", "LD_LIBRARY_PATH")
                       if key in os.environ}
        environment.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
        environment["ML_DATASET_ROOT"] = str(self.dataset_root)
        with (directory/"runner.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "bootstrap.ml_training_runner",
                                        "--directory", str(directory), "--cpu-seconds", str(self.timeout)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
        self._children[handle.execution_id] = process

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation:
        process = self._children.get(handle.execution_id)
        if process is not None and process.poll() is not None:
            return ExecutionObservation("STOPPED", process.returncode)
        directory = Path(handle.directory)
        marker = directory/"process.json"
        if not marker.exists():
            # Parent death does not prove that a child between fork/exec and its
            # identity write never existed. Preserve uncertainty, never relaunch.
            return ExecutionObservation("UNKNOWN")
        saved = json.loads(marker.read_text())
        if saved.get("host") != socket.gethostname():
            return ExecutionObservation("UNKNOWN")
        actual = process_identity(saved["pid"])
        if actual is None or actual["boot_id"] != saved["boot_id"] or (
            actual["start_ticks"] != saved["start_ticks"] or actual["state"] == "Z"
        ):
            exit_path = directory/"exit.json"
            return ExecutionObservation("STOPPED", json.loads(exit_path.read_text())["code"]
                                        if exit_path.exists() else -1)
        return ExecutionObservation("RUNNING")

    def stop(self, handle: ExecutionHandle) -> bool:
        marker = Path(handle.directory)/"process.json"
        process = self._children.get(handle.execution_id)
        if not marker.exists() and process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=self.grace)
            return True
        if self.inspect(handle).state == "STOPPED":
            return True
        if not marker.exists() or self.inspect(handle).state != "RUNNING":
            return False
        saved = json.loads(marker.read_text())
        for action in (signal.SIGTERM, signal.SIGKILL):
            if self.inspect(handle).state != "RUNNING":
                return self.inspect(handle).state == "STOPPED"
            try:
                os.killpg(saved["pid"], action)
            except ProcessLookupError:
                return True
            deadline = time.monotonic()+self.grace
            while time.monotonic() < deadline:
                if self.inspect(handle).state == "STOPPED":
                    return True
                time.sleep(0.05)
        return self.inspect(handle).state == "STOPPED"

    def result(self, handle: ExecutionHandle) -> TrainingOutput | None:
        path = Path(handle.directory)/"result.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        metrics = tuple(MetricSnapshot(item["step"], tuple(
            (key, float(number)) for key, number in item.items() if key != "step"))
            for item in value["metrics"])
        return TrainingOutput(str(Path(handle.directory)/"bundle"), value["manifest_digest"],
                              metrics, value["dataset_digest"])
