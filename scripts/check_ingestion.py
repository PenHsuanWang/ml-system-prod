"""Check the complete ingress slice without weakening the repository's typing rules."""

import subprocess
import sys
from pathlib import Path

PRODUCTION = [
    "server.py",
    "src/api/routers.py",
    "src/domain/ml.py",
    "src/domain/ingestion",
    "src/application/ingestion",
    "src/infrastructure/ingestion",
    "src/api/ingestion",
    "bootstrap/ingestion_wiring.py",
    "bootstrap/ingestion_cli.py",
    "bootstrap/ml_api.py",
    "migrations/env.py",
    "migrations/versions/0006_ingestion.py",
]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    checks = [
        [sys.executable, "-m", "ruff", "check", *PRODUCTION, "tests/ml/ingestion"],
        [sys.executable, "-m", "ruff", "format", "--check", *PRODUCTION, "tests/ml/ingestion"],
        [sys.executable, "-m", "mypy", "--follow-imports=skip", *PRODUCTION],
    ]
    for command in checks:
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
