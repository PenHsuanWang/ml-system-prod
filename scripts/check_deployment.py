"""Check production bootstrap and qualification boundaries without relaxing other gates."""

import subprocess
import sys
from pathlib import Path

PRODUCTION = ["bootstrap/deployment_entrypoint.py", "bootstrap/deployment_cli.py"]
TESTS = ["tests/ml/test_deployment_entrypoint.py", "tests/ml/test_deployment_qualification.py"]


def main() -> int:
    commands = [
        [sys.executable, "-m", "ruff", "check", *PRODUCTION, *TESTS],
        [sys.executable, "-m", "ruff", "format", "--check", *PRODUCTION, *TESTS],
        [sys.executable, "-m", "mypy", "--follow-imports=skip", *PRODUCTION],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
