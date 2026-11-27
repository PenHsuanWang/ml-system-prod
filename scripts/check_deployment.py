"""Check production bootstrap and qualification boundaries without relaxing other gates."""

import subprocess
import sys
from pathlib import Path

PRODUCTION = [
    "bootstrap/deployment_entrypoint.py",
    "bootstrap/deployment_cli.py",
    "src/api/governance_discovery.py",
    "src/application/governance.py",
    "src/infrastructure/persistence/governance_reads.py",
]
TESTS = [
    "tests/ml/test_deployment_entrypoint.py",
    "tests/ml/test_deployment_qualification.py",
    "tests/ml/test_governance_discovery.py",
]
TYPING_DEPENDENCIES = [
    "src/api/collection_schemas.py",
    "src/api/ml_schemas.py",
    "src/api/prediction_catalog.py",
    "src/application/ml_services.py",
    "src/domain/ml.py",
    "src/domain/pagination.py",
    "src/domain/prediction/catalog.py",
]


def main() -> int:
    commands = [
        [sys.executable, "-m", "ruff", "check", *PRODUCTION, *TESTS],
        [sys.executable, "-m", "ruff", "format", "--check", *PRODUCTION, *TESTS],
        [sys.executable, "-m", "mypy", "--follow-imports=skip", *PRODUCTION, *TYPING_DEPENDENCIES],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
