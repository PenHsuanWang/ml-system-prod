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
    "src/domain/prediction/catalog.py",
    "src/application/prediction/catalog.py",
    "src/api/prediction_catalog.py",
    "src/infrastructure/persistence/prediction_catalog.py",
    "src/infrastructure/persistence/prediction_contract_schema.py",
    "bootstrap/prediction_contracts.py",
    "bootstrap/prediction_contract_cli.py",
    "migrations/versions/0007_prediction_contracts.py",
    "tests/ml/test_prediction_catalog.py",
    "tests/ml/test_prediction_catalog_e2e.py",
]


def main() -> int:
    for command in ("check", "format"):
        arguments = [sys.executable, "-m", "ruff", command]
        if command == "format":
            arguments.append("--check")
        result = subprocess.run([*arguments, *FILES], check=False)
        if result.returncode:
            return result.returncode
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=skip",
            "src/domain/ml.py",
            "src/domain/prediction/catalog.py",
            "src/application/prediction/catalog.py",
            "src/api/prediction_catalog.py",
            "src/infrastructure/persistence/prediction_catalog.py",
            "src/infrastructure/persistence/prediction_contract_schema.py",
            "bootstrap/prediction_contracts.py",
            "bootstrap/prediction_contract_cli.py",
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
