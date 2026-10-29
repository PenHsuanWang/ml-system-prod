"""Build the production image and verify an isolated real Docker/RabbitMQ stack.

Run with a development Python environment containing pytest when --host-tests is
selected. No credentials or test fixtures are baked into the production image.
Only this run's Compose project and default-smoke container are removed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Verification:
    def __init__(self, output: Path, host_tests: bool) -> None:
        self.run_id = uuid4().hex[:12]
        self.project = f"backend-verify-{self.run_id}"
        self.image = f"local/backend-validation:{self.run_id}"
        self.output = output.resolve() / self.project
        self.output.mkdir(parents=True)
        self.host_tests = host_tests
        self.passwords = [secrets.token_hex(24) for _ in range(4)]
        rabbit, mongo, jwt, admin = self.passwords
        self.admin_password = admin
        self.environment = {
            **os.environ,
            "DOCKER_VALIDATION_IMAGE": self.image,
            "DOCKER_VALIDATION_QUEUE": f"validation.{self.run_id}",
            "DOCKER_VALIDATION_RABBIT_USER": "validation",
            "DOCKER_VALIDATION_RABBIT_PASSWORD": rabbit,
            "DOCKER_VALIDATION_MONGO_PASSWORD": mongo,
            "DOCKER_VALIDATION_JWT_SECRET": jwt,
        }
        self.env_file = self.output / "secrets.env"
        self.env_file.touch(mode=0o600)
        self.env_file.write_text(
            "\n".join(
                f"{key}={value}"
                for key, value in self.environment.items()
                if key.startswith("DOCKER_VALIDATION_")
            )
            + "\n"
        )
        self.compose_args = [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "--project-name",
            self.project,
            "-f",
            "compose.validation.yaml",
        ]
        self.smoke_name = self.project + "-default"
        self.stack_created = False
        self.smoke_created = False
        self.report: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "project": self.project,
            "image": self.image,
            "checks": [],
            "passed": False,
        }

    def redact(self, text: str) -> str:
        for secret in self.passwords:
            text = text.replace(secret, "[REDACTED]")
        return text

    def command(
        self,
        args: Sequence[str],
        *,
        timeout: float = 120,
        input_text: str | None = None,
        log: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        result = subprocess.run(
            list(args),
            cwd=ROOT,
            env=self.environment if environment is None else environment,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = self.redact(result.stdout + result.stderr)
        if log:
            (self.output / log).write_text(output)
        if result.returncode:
            raise RuntimeError(f"Command failed ({args[0]}):\n{output[-10000:]}")
        return result.stdout

    def compose(self, *args: str, **kwargs: Any) -> str:
        return self.command([*self.compose_args, *args], **kwargs)

    def passed(self, name: str, evidence: Any = True) -> None:
        self.report["checks"].append({"name": name, "evidence": evidence})
        print(f"PASS {name}", flush=True)

    def wait_for(self, check: Callable[[], bool], *, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.5)
        raise TimeoutError("Timed out waiting for the validation condition")

    def port(self, service: str, port: int) -> int:
        address = self.compose("port", service, str(port)).strip()
        return int(address.rsplit(":", 1)[1])

    def http(
        self,
        base: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any, Mapping[str, str]]:
        headers = {"X-Request-ID": f"validation-{uuid4().hex}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read()
            return response.status, json.loads(data) if data else None, dict(response.headers)

    def queues(self) -> list[dict[str, Any]]:
        output = self.compose(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--quiet",
            "--formatter",
            "json",
            "name",
            "type",
            "consumers",
            "messages_ready",
            "messages_unacknowledged",
        )
        return list(json.loads(output))

    def queue(self) -> dict[str, Any]:
        name = self.environment["DOCKER_VALIDATION_QUEUE"]
        return next((queue for queue in self.queues() if queue["name"] == name), {})

    def api_pids(self) -> set[int]:
        text = self.compose("logs", "--no-color", "--no-log-prefix", "api")
        result = set()
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("logger") == "src.api.http" and record.get("message", "").startswith(
                "GET /api/v1/jobs/"
            ):
                result.add(int(record["process_id"]))
        return result

    def execute(self) -> None:
        print(f"Validation project: {self.project}", flush=True)
        versions = json.loads(
            self.command(["docker", "version", "--format", "{{json .}}"], timeout=15)
        )
        self.report["docker"] = {
            "client": versions["Client"]["Version"],
            "server": versions["Server"]["Version"],
        }
        print("Building the production Dockerfile (see build.log after completion)...", flush=True)
        self.command(
            ["docker", "build", "--progress", "plain", "--tag", self.image, "."],
            timeout=900,
            log="build.log",
        )
        info = json.loads(self.command(["docker", "image", "inspect", self.image]))[0]
        self.report["image_id"] = info["Id"]
        self.report["platform"] = f"{info['Os']}/{info['Architecture']}"
        assert info["Config"]["User"] not in {"", "0", "root"}
        self.passed("production image builds and declares a non-root user")
        # The image's unmodified CMD must work without infrastructure or fixtures.
        self.smoke_created = True
        self.command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.smoke_name,
                "--label",
                f"io.backend.validation.project={self.project}",
                "--network",
                "none",
                self.image,
            ]
        )
        self.smoke_created = True
        probe = (
            "import json,os,urllib.request; "
            "data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json')); "
            "assert set(data['paths'])=={'/health/live','/health/ready'}; "
            "assert os.getuid()!=0; "
            "assert urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status==200; "
            "import importlib.util; assert importlib.util.find_spec('tests') is None; "
            "assert importlib.util.find_spec('pip') is None; "
            "assert importlib.util.find_spec('ensurepip') is None; "
            "print('default-ready-non-root-no-fixtures-no-installers')"
        )

        def default_ready() -> bool:
            try:
                self.command(["docker", "exec", self.smoke_name, "python", "-c", probe])
                return True
            except RuntimeError:
                return False

        self.wait_for(default_ready, timeout=45)
        self.command(["docker", "stop", "--time", "15", self.smoke_name])
        self.command(["docker", "logs", self.smoke_name], log="default.log")
        log = (self.output / "default.log").read_text()
        assert "Application shutdown complete" in log
        self.command(["docker", "rm", self.smoke_name])
        self.smoke_created = False
        self.passed("unmodified image CMD, readiness, clean OpenAPI and graceful shutdown")
        self.stack_created = True
        self.compose(
            "up", "--detach", "--wait", "--wait-timeout", "180", timeout=240, log="startup.log"
        )
        status = self.compose("ps", "--format", "json")
        (self.output / "containers.jsonl").write_text(self.redact(status))
        base = f"http://127.0.0.1:{self.port('api', 8000)}"
        self.wait_for(lambda: int(self.queue().get("consumers", 0)) == 2)
        queue = self.queue()
        assert queue["type"] == "quorum", queue
        self.passed("RabbitMQ quorum queue has two live image-based Taskiq consumers", queue)
        self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "manage.py",
            "create-admin",
            "--tenant-id",
            "validation",
            "--username",
            "validation_admin",
            "--password-stdin",
            input_text=self.admin_password + "\n",
        )
        status_code, data, _ = self.http(
            base,
            "/api/v1/identity/login",
            body={
                "tenant_id": "validation",
                "username": "validation_admin",
                "password": self.admin_password,
            },
        )
        assert status_code == 200, data
        token = data["access_token"]
        # Tokens can appear in error responses from future code; redact them too.
        self.passwords.append(token)
        assert self.http(base, "/api/v1/identity/me", token=token)[0] == 200
        assert self.http(base, "/api/v1/kernels")[0] == 401
        status_code, data, _ = self.http(base, "/api/v1/kernels", token=token)
        assert status_code == 200 and any(x["name"] == "probe.echo" for x in data), data
        status_code, user, _ = self.http(
            base,
            "/api/v1/identity/register",
            token=token,
            body={
                "username": "validation_reader",
                "password": self.admin_password,
            },
        )
        assert status_code == 201, user
        status_code, reader, _ = self.http(
            base,
            "/api/v1/identity/login",
            body={
                "tenant_id": "validation",
                "username": "validation_reader",
                "password": self.admin_password,
            },
        )
        assert status_code == 200, reader
        reader_token = reader["access_token"]
        self.passwords.append(reader_token)
        assert (
            self.http(
                base,
                "/api/v1/kernels/probe.echo/execute",
                token=reader_token,
                body={"version": "1", "input": {"value": 0}},
            )[0]
            == 403
        )
        self.passed("MongoDB-backed admin/login/registration/JWT and kernel authorization")

        def submit(value: int) -> tuple[str, int]:
            code, job, _ = self.http(
                base,
                "/api/v1/kernels/probe.echo/runs",
                token=token,
                body={"version": "1", "input": {"value": value}},
            )
            assert code == 202, job
            return f"/api/v1/jobs/{job['job_id']}", value

        with ThreadPoolExecutor(max_workers=12) as pool:
            pending = dict(pool.map(submit, range(32)))
        locations = list(pending)
        workers: set[int] = set()

        def completed() -> bool:
            for path in list(pending):
                code, snapshot, _ = self.http(base, path, token=token)
                assert code == 200 and snapshot["status"] not in {"failed", "cancelled"}, snapshot
                if snapshot["status"] == "succeeded":
                    output = snapshot["result"]["output"]
                    assert output["value"] == pending.pop(path)
                    workers.add(int(output["worker_pid"]))
            return not pending

        self.wait_for(completed)
        assert len(workers) == 2, workers

        def both_api_workers() -> bool:
            with ThreadPoolExecutor(max_workers=12) as pool:
                responses = list(
                    pool.map(lambda path: self.http(base, path, token=token)[0], locations)
                )
            assert all(code == 200 for code in responses)
            return len(self.api_pids()) == 2

        self.wait_for(both_api_workers)
        self.passed(
            "32 authenticated kernel jobs complete across two API and two task workers",
            {
                "jobs": len(locations),
                "api_pids": sorted(self.api_pids()),
                "task_pids": sorted(workers),
            },
        )
        self.wait_for(
            lambda: (
                self.queue().get("messages_ready") == 0
                and self.queue().get("messages_unacknowledged") == 0
            )
        )
        self.passed("RabbitMQ queue drains with no ready or unacknowledged messages", self.queue())
        self.compose("restart", "api", timeout=60)
        # An ephemeral published host port can change when Docker restarts a
        # container. Internal service addressing remains stable.
        base = f"http://127.0.0.1:{self.port('api', 8000)}"

        def restarted() -> bool:
            try:
                return self.http(base, "/health/ready")[0] == 200
            except (URLError, OSError):
                return False

        self.wait_for(restarted)
        assert self.http(base, locations[0], token=token)[0] == 200
        self.passed("API container restart preserves Mongo session and Redis job snapshots")
        if self.host_tests:
            host_env = {
                key: value for key, value in os.environ.items() if not key.startswith("APP_")
            }
            rabbit_password, mongo_password = self.passwords[:2]
            host_env.update(
                {
                    "RUN_SYSTEM_INTEGRATION": "1",
                    "APP_RABBITMQ_URL": (
                        f"amqp://validation:{rabbit_password}@127.0.0.1:"
                        f"{self.port('rabbitmq', 5672)}/"
                    ),
                    "APP_REDIS_URL": f"redis://127.0.0.1:{self.port('redis', 6379)}/0",
                    "MONGO_TEST_URI": (
                        f"mongodb://backend_root:{mongo_password}@127.0.0.1:"
                        f"{self.port('mongodb', 27017)}/?authSource=admin"
                    ),
                }
            )
            self.command(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-rs",
                    "--basetemp",
                    str(self.output / "pytest"),
                ],
                timeout=180,
                environment=host_env,
                log="pytest.log",
            )
            self.passed(
                "full pytest suite including real two-process broker and MongoDB integration"
            )
        assert self.http(base, "/api/v1/identity/logout", body={}, token=token)[0] == 204
        assert self.http(base, "/api/v1/identity/me", token=token)[0] == 401
        self.passed("logout revokes the persisted MongoDB session")
        self.report["versions"] = {
            "rabbitmq": self.compose("exec", "-T", "rabbitmq", "rabbitmqctl", "version").strip(),
            "redis": self.compose("exec", "-T", "redis", "redis-server", "--version").strip(),
            "mongodb": self.compose("exec", "-T", "mongodb", "mongod", "--version").splitlines()[0],
        }
        self.report["passed"] = True

    def cleanup(self) -> None:
        errors = []
        if self.smoke_created:
            try:
                self.command(["docker", "logs", self.smoke_name], log="default.log")
            except Exception as exc:
                self.report["default_log_warning"] = self.redact(str(exc))
            try:
                self.command(["docker", "rm", "--force", self.smoke_name])
            except Exception as exc:
                if "no such container" not in str(exc).lower():
                    errors.append(self.redact(str(exc)))
        if self.stack_created:
            try:
                self.compose("logs", "--no-color", "--no-log-prefix", log="stack.log")
            except Exception as exc:
                self.report["stack_log_warning"] = self.redact(str(exc))
            try:
                self.compose(
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "15",
                    timeout=120,
                    log="cleanup.log",
                )
            except Exception as exc:
                errors.append(self.redact(str(exc)))
        self.report["cleanup_complete"] = not errors
        if errors:
            raise RuntimeError("; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "work" / "docker-validation")
    parser.add_argument(
        "--host-tests", action="store_true", help="Run full pytest using container services"
    )
    args = parser.parse_args()
    run = Verification(args.output_dir, args.host_tests)
    try:
        run.execute()
    except Exception as exc:
        run.report["error"] = run.redact(f"{type(exc).__name__}: {exc}")
        print(run.report["error"], file=sys.stderr, flush=True)
    finally:
        try:
            run.cleanup()
        except Exception as exc:
            run.report["cleanup_error"] = run.redact(str(exc))
            run.report["passed"] = False
            print(f"Cleanup failed: {run.report['cleanup_error']}", file=sys.stderr)
        run.report["finished_at"] = datetime.now(UTC).isoformat()
        if run.report.get("cleanup_complete"):
            run.env_file.unlink(missing_ok=True)
        else:
            run.report["credentials_retained_for_cleanup"] = str(run.env_file)
        (run.output / "report.json").write_text(run.redact(json.dumps(run.report, indent=2)))
    print(f"Sanitized evidence: {run.output}", flush=True)
    return 0 if run.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
