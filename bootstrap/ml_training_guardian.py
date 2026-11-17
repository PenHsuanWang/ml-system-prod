"""Observe one compute process group until physical stop; never connects to business stores."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from src.domain.ml import digest
from src.infrastructure.execution.local_process import process_identity
from src.infrastructure.execution.training_records import matches, publish, read


def group_running(group: int) -> bool:
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def supervise(root: Path, *, grace: float, runner: list[str]) -> int:
    identity = read(root / "launch.json")
    if digest(read(root / "spec.json")) != identity["spec_digest"]:
        raise ValueError("Guardian envelope digest mismatch")
    deadline = float(identity["deadline"])
    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    # Exclusive immutable gate precedes fork/exec, including guardian replay.
    publish(
        root / "guardian-start.json",
        {**identity, "guardian": process_identity(os.getpid())},
        exclusive=True,
    )
    child = None
    reason = "LAUNCH_FAILED"
    try:
        # The shared, durable slot survives a Worker/guardian crash. A mutex alone
        # would release on guardian death while an orphaned compute group lives.
        with (root.parent / "training-slot.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                previous = read(root.parent / "active-training.json")
                stopped = read(root.parent / previous["execution_id"] / "stopped.json")
                available = matches(stopped, previous) and stopped.get("confirmed") is True
            except FileNotFoundError:
                available = not (root.parent / "active-training.json").exists()
            except (OSError, ValueError, KeyError):
                available = False
            if not available:
                reason = "CAPACITY_UNCERTAIN"
                return 1
            publish(root.parent / "active-training.json", identity)
        if time.time() >= deadline or (root / "stop-request.json").exists():
            reason = "STOPPED_BEFORE_LAUNCH"
            return 1
        child = subprocess.Popen(runner, stdin=subprocess.DEVNULL, start_new_session=True)
        publish(root / "compute-start.json", {**identity, "process": process_identity(child.pid)})
        reason = "COMPLETED"
        while child.poll() is None:
            requested = False
            try:
                requested = matches(read(root / "stop-request.json"), identity)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                # Corrupted stop authority cannot authorize continued compute.
                requested = True
            if interrupted or requested or time.time() >= deadline:
                reason = "DEADLINE" if time.time() >= deadline else "STOP_REQUESTED"
                break
            time.sleep(0.05)
    finally:
        if child is not None:
            for action in (signal.SIGTERM, signal.SIGKILL):
                if not group_running(child.pid):
                    break
                try:
                    os.killpg(child.pid, action)
                except ProcessLookupError:
                    pass
                until = time.monotonic() + grace
                while time.monotonic() < until and group_running(child.pid):
                    child.poll()
                    time.sleep(0.05)
            child.wait(timeout=grace)
            if group_running(child.pid):
                raise RuntimeError("Compute group stop remains uncertain")
        publish(
            root / "stopped.json",
            {
                **identity,
                "confirmed": True,
                "reason": reason,
                "exit_code": child.returncode if child is not None else -1,
            },
        )
    return child.returncode if child is not None else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--grace", type=float, required=True)
    args = parser.parse_args()
    if args.grace <= 0 or args.cpu_seconds <= 0:
        raise ValueError("Training limits must be positive")
    raise SystemExit(
        supervise(
            args.directory.resolve(strict=True),
            grace=args.grace,
            runner=[
                sys.executable,
                "-m",
                "bootstrap.ml_training_runner",
                "--directory",
                str(args.directory),
                "--cpu-seconds",
                str(args.cpu_seconds),
            ],
        )
    )


if __name__ == "__main__":
    main()
