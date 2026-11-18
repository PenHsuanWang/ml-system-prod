"""Arm a bounded compute pause in the original Supervisor PID namespace only."""

import json
import os
import signal
import sys
import time
from pathlib import Path

from src.infrastructure.execution.local_process import process_identity
from src.infrastructure.execution.training_records import read


def main():
    if os.environ.get("ML_TRAINING_NAMESPACE") not in {
        "ml-k8s-training-runtime",
        "ml-k8s-acceptance",
    }:
        raise ValueError("Fault injection is restricted to the disposable namespace")
    root = Path(os.environ["ML_EXECUTION_ROOT"])
    if sys.argv[1] == "freeze-next":
        existing = set(root.glob("*/compute-start.json"))
        print("ARMED", flush=True)
        until = time.monotonic() + 90
        while time.monotonic() < until:
            for path in root.glob("*/compute-start.json"):
                if path in existing:
                    continue
                identity = read(path.parent / "launch.json")
                if identity["pod_uid"] != os.environ["ML_TRAINING_POD_UID"]:
                    continue
                saved = read(path)["process"]
                actual = process_identity(saved["pid"])
                if actual and all(
                    actual[k] == saved[k] for k in ("host", "boot_id", "start_ticks")
                ):
                    os.killpg(saved["pid"], signal.SIGSTOP)
                    print(
                        json.dumps({"frozen": True, "execution_id": identity["execution_id"]}),
                        flush=True,
                    )
                    return
            time.sleep(0.01)
        raise TimeoutError("No new compute was available for the bounded fault")
    directory = root / sys.argv[2]
    identity = read(directory / "launch.json")
    if identity["pod_uid"] != os.environ["ML_TRAINING_POD_UID"]:
        raise ValueError("Never signal a PID from another Pod")
    if sys.argv[1] == "kill-worker":
        saved = identity["parent"]
        actual = process_identity(saved["pid"])
        if actual is None or any(actual[k] != saved[k] for k in ("host", "boot_id", "start_ticks")):
            raise ValueError("Supervisor process identity changed")
        os.kill(saved["pid"], signal.SIGKILL)
    elif sys.argv[1] == "resume":
        saved = read(directory / "compute-start.json")["process"]
        actual = process_identity(saved["pid"])
        if actual is None or any(actual[k] != saved[k] for k in ("host", "boot_id", "start_ticks")):
            raise ValueError("Compute process identity changed")
        os.killpg(saved["pid"], signal.SIGCONT)
    else:
        raise ValueError("Unknown bounded fault")
    print(json.dumps({"completed": sys.argv[1]}))


if __name__ == "__main__":
    main()
