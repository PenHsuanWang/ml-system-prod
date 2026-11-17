"""Lint the new Serving adapters without changing legacy formatting boundaries."""

import subprocess
import sys

FILES = [
    "src/domain/deployments/identity.py",
    "src/infrastructure/deployment/endpoints.py",
    "src/infrastructure/deployment/kubernetes_serving.py",
    "bootstrap/ml_serving_wiring.py",
    "tests/ml/test_serving_runtime.py",
    "scripts/check_serving_runtime.py",
    "scripts/k8s_serving_fixture.py",
    "scripts/verify_k8s_serving.py",
]


def main() -> int:
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
