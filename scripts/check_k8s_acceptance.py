"""Lint only acceptance orchestration; retain the existing Compose/runtime gates."""

import subprocess
import sys

FILES = [
    "scripts/k8s_acceptance_contract.py",
    "scripts/k8s_acceptance_fixture.py",
    "scripts/verify_k8s_acceptance.py",
    "scripts/check_k8s_acceptance.py",
    "tests/ml/k8s/test_acceptance.py",
]


def main():
    for command in ("check", "format"):
        arguments = [sys.executable, "-m", "ruff", command]
        if command == "format":
            arguments.append("--check")
        result = subprocess.run([*arguments, *FILES], check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
