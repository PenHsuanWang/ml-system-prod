"""Check Pod Training adapter boundaries and formatting."""

import subprocess
import sys

FILES = [
    "bootstrap/ml_training_guardian.py",
    "bootstrap/ml_training_wiring.py",
    "src/infrastructure/execution/pod_process.py",
    "src/infrastructure/execution/training_records.py",
    "src/infrastructure/messaging/training_ack.py",
    "src/application/training/delivery.py",
    "src/application/training/reconciliation.py",
    "tests/ml/test_pod_training_runtime.py",
    "scripts/check_training_runtime.py",
]


def main() -> int:
    for command in ("check", "format"):
        args = [sys.executable, "-m", "ruff", command]
        if command == "format":
            args.append("--check")
        result = subprocess.run([*args, *FILES], check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
