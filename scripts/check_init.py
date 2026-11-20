"""Scoped formatting and strict typing gate for interactive setup."""

import subprocess
import sys

PRODUCTION = ["bootstrap/init_cli.py", "bootstrap/init_config.py", "bootstrap/init_files.py"]


def main() -> int:
    commands = [
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *PRODUCTION,
            "tests/ml/init_setup",
            __file__,
            "scripts/verify_init_compose.py",
        ],
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            *PRODUCTION,
            "tests/ml/init_setup",
            __file__,
            "scripts/verify_init_compose.py",
        ],
        [sys.executable, "-m", "mypy", "--follow-imports=skip", *PRODUCTION],
    ]
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
