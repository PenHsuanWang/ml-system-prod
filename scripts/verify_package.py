"""Build sdist + wheel and smoke-test a non-editable installation outside the checkout.

A fresh build venv installs requirements-build.lock with hash checking. A separate
fresh runtime venv installs requirements-runtime.lock with hash checking and then
only the generated wheel (--no-deps). Network access to the package index is
required. No source checkout or development dependencies are on the smoke path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SMOKE = """
import asyncio
import importlib
import importlib.metadata
import json
import os
import pkgutil
import socket
import sys
from pathlib import Path

# Import and lifecycle construction must not open an external connection.
def deny_network(*args, **kwargs):
    raise AssertionError("Package smoke attempted an external connection")
socket.create_connection = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
os.environ["APP_TASK_BACKEND"] = "distributed"
for name in ("server", "worker", "manage", "src", "bootstrap"):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix)), module.__file__
for name in ("src", "bootstrap"):
    package = importlib.import_module(name)
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(info.name)
from httpx import ASGITransport, AsyncClient
from server import create_app
from src.config.settings import Settings

async def smoke():
    config = Settings(_env_file=None, task_backend="disabled", identity_enabled=False)
    app = create_app(config)
    assert app.state.runtime is None
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://installed") as client:
            for path in ("/health/live", "/health/ready"):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.status_code, response.text)
    assert app.state.runtime is None
asyncio.run(smoke())
print(json.dumps({"version": importlib.metadata.version("modular-fastapi-backend"),
                  "python": sys.version.split()[0], "non_editable": True,
                  "external_network_blocked": True, "lifespan_and_health": True}))
"""


def python_in(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "work" / "package-validation")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "passed": False}
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("APP_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    def command(args: list[str], *, cwd: Path, log: str, timeout: int = 300) -> str:
        result = subprocess.run(
            args, cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout
        )
        text = result.stdout + result.stderr
        (output / log).write_text(text)
        if result.returncode:
            raise RuntimeError(f"{log} failed (exit {result.returncode}):\n{text[-8000:]}")
        return result.stdout

    try:
        with tempfile.TemporaryDirectory(prefix="backend-package-") as temporary:
            outside = Path(temporary)
            build_env, runtime_env = outside / "build-venv", outside / "runtime-venv"
            venv.EnvBuilder(with_pip=True).create(build_env)
            venv.EnvBuilder(with_pip=True).create(runtime_env)
            build_python, runtime_python = str(python_in(build_env)), str(python_in(runtime_env))
            command(
                [
                    build_python,
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(ROOT / "requirements-build.lock"),
                ],
                cwd=outside,
                log="build-dependencies.log",
            )
            # build's default creates the wheel from the newly built sdist.
            command(
                [build_python, "-m", "build", "--no-isolation", "--outdir", str(output), str(ROOT)],
                cwd=outside,
                log="build.log",
            )
            wheels = list(output.glob("*.whl"))
            sdists = list(output.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise RuntimeError(
                    "Expected exactly one wheel and one sdist in the output directory"
                )
            command(
                [
                    runtime_python,
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(ROOT / "requirements-runtime.lock"),
                ],
                cwd=outside,
                log="runtime-dependencies.log",
            )
            command(
                [runtime_python, "-m", "pip", "install", "--no-deps", str(wheels[0])],
                cwd=outside,
                log="wheel-install.log",
            )
            command([runtime_python, "-m", "pip", "check"], cwd=outside, log="pip-check.log")
            text = command(
                [runtime_python, "-I", "-c", SMOKE], cwd=outside, log="installed-smoke.log"
            )
            report["smoke"] = json.loads(text.strip().splitlines()[-1])
            report["artifacts"] = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [*wheels, *sdists]
            }
            report["passed"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(report["error"], file=sys.stderr)
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Package validation evidence: {output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
