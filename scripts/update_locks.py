"""Regenerate hash-locked runtime, development and build dependencies.

Run from the project with Docker available: python scripts/update_locks.py
The resolver runs on the lowest supported Python (3.11) in Linux, using pinned
pip/pip-tools. Existing pins are retained unless --upgrade is requested. Review
the diff, run the Python CI matrix, package smoke and vulnerability scans before
merging an update. This script only replaces requirements*.lock, never installs
packages into the developer's environment. Registry/network access is required.

The locks target Linux containers and the CI Python 3.11/3.13 matrix. Artifact
hashes cover the available upstream distributions; they are not a substitute for
trusted indexes, image digest pinning, or verification on another OS/platform.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER_IMAGE = (
    "python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377"
)


def compile_locks(upgrade: bool) -> None:
    if sys.platform != "linux" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("Lock resolution requires Linux Python 3.11; use the Docker entry point")
    environment = {**os.environ, "CUSTOM_COMPILE_COMMAND": "python scripts/update_locks.py"}
    base = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "--resolver=backtracking",
        "--generate-hashes",
        "--allow-unsafe",
        "--reuse-hashes",
        "--strip-extras",
        "--no-emit-index-url",
        "--no-emit-trusted-host",
        "--no-emit-find-links",
    ]
    if upgrade:
        base.append("--upgrade")
    retention = [] if upgrade else ["--constraint=requirements.lock"]
    jobs = [
        ["--output-file=requirements-build.lock", "requirements-build.in"],
        [
            "--output-file=requirements-runtime.lock",
            *retention,
            "pyproject.toml",
            "requirements-installer.in",
        ],
        [
            "--output-file=requirements-dev.lock",
            "--extra=dev",
            "--constraint=requirements-build.lock",
            *retention,
            "pyproject.toml",
        ],
    ]
    for args in jobs:
        subprocess.run([*base, *args], cwd=ROOT, env=environment, check=True, timeout=1800)
    pins: dict[str, str] = {}
    for name in ("requirements-dev.lock", "requirements-build.lock"):
        for line in (ROOT / name).read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line)
            if match:
                key = re.sub(r"[-_.]+", "-", match[1]).lower()
                value = f"{match[1]}=={match[2]}"
                if key in pins and pins[key] != value:
                    raise RuntimeError(f"Conflicting build/development pin: {key}")
                pins[key] = value
    (ROOT / "requirements.lock").write_text(
        "# Generated compatibility constraints; install the hash locks for checked artifacts.\n"
        "# Update: python scripts/update_locks.py [--upgrade]\n"
        + "\n".join(pins[key] for key in sorted(pins))
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="Resolve newer allowed versions")
    parser.add_argument("--inside-resolver", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside_resolver:
        compile_locks(args.upgrade)
        return 0
    bootstrap = (
        "import subprocess,sys; "
        "subprocess.run([sys.executable,'-m','venv','/tmp/resolver'],check=True); "
        "p='/tmp/resolver/bin/python'; "
        "subprocess.run([p,'-m','pip','install','pip==26.2.2','pip-tools==7.6.2'],check=True); "
        "subprocess.run([p,'scripts/update_locks.py','--inside-resolver'"
        + (",'--upgrade'" if args.upgrade else "")
        + "],check=True)"
    )
    command = ["docker", "run", "--rm"]
    if hasattr(os, "getuid"):
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp"])
    command.extend(
        [
            "--mount",
            f"type=bind,source={ROOT},target=/project",
            "--workdir",
            "/project",
            RESOLVER_IMAGE,
            "python",
            "-c",
            bootstrap,
        ]
    )
    subprocess.run(command, check=True, timeout=5400)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
