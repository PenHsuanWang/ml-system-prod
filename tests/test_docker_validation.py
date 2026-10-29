"""Safety boundaries for the opt-in Docker runner; Docker is never invoked here."""

import json
import stat
import subprocess
import sys

import pytest

from scripts import verify_docker


def test_command_errors_and_saved_logs_redact_credentials_and_tokens(tmp_path, monkeypatch):
    run = verify_docker.Verification(tmp_path, host_tests=False)
    token = "test-session-token-that-must-not-be-published"
    run.passwords.append(token)
    sensitive = " ".join(run.passwords)

    def failed_command(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, sensitive, f"Bearer {token}")

    monkeypatch.setattr(subprocess, "run", failed_command)
    with pytest.raises(RuntimeError) as caught:
        run.command(["docker", "version"], log="failed.log")

    for evidence in (str(caught.value), (run.output / "failed.log").read_text()):
        assert "[REDACTED]" in evidence
        assert all(secret not in evidence for secret in run.passwords)
    assert stat.S_IMODE(run.env_file.stat().st_mode) == 0o600


def test_failed_docker_preflight_does_not_remove_any_container_or_stack(tmp_path, monkeypatch):
    calls = []

    def unavailable_docker(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "Docker daemon is unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable_docker)
    monkeypatch.setattr(sys, "argv", ["verify_docker.py", "--output-dir", str(tmp_path)])

    assert verify_docker.main() == 1
    # Preflight failure grants no ownership of runtime resources to clean up.
    assert len(calls) == 1
    assert calls[0][:2] == ["docker", "version"]
    report_path = next(tmp_path.glob("*/report.json"))
    report = json.loads(report_path.read_text())
    assert not report["passed"]
    assert report["cleanup_complete"]
    assert not (report_path.parent / "secrets.env").exists()


def test_log_collection_failure_still_removes_only_the_owned_resources(tmp_path, monkeypatch):
    run = verify_docker.Verification(tmp_path, host_tests=False)
    run.smoke_created = run.stack_created = True
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if "logs" in args:
            raise RuntimeError(f"Log collection failed {run.admin_password}")
        return ""

    monkeypatch.setattr(run, "command", command)
    run.cleanup()

    removals = [args for args in calls if "rm" in args or "down" in args]
    assert len(removals) == 2
    for args in removals:
        if "rm" in args:
            assert args[-1] == run.smoke_name
            assert "--all" not in args
        else:
            assert args[args.index("--project-name") + 1] == run.project
            assert args[args.index("-f") + 1] == "compose.validation.yaml"
    assert run.report["cleanup_complete"]
    assert run.admin_password not in json.dumps(run.report)


def test_failed_cleanup_keeps_private_credentials_for_targeted_recovery(tmp_path, monkeypatch):
    runs = []

    def execute(run):
        runs.append(run)
        run.stack_created = True
        run.report["passed"] = True

    def command(run, args, **kwargs):
        if "down" in args:
            raise RuntimeError(f"Stack removal failed {run.admin_password}")
        return ""

    monkeypatch.setattr(verify_docker.Verification, "execute", execute)
    monkeypatch.setattr(verify_docker.Verification, "command", command)
    monkeypatch.setattr(sys, "argv", ["verify_docker.py", "--output-dir", str(tmp_path)])

    assert verify_docker.main() == 1
    run = runs[0]
    report_text = (run.output / "report.json").read_text()
    report = json.loads(report_text)
    assert not report["passed"] and not report["cleanup_complete"]
    assert report["credentials_retained_for_cleanup"] == str(run.env_file)
    assert run.env_file.exists()
    assert stat.S_IMODE(run.env_file.stat().st_mode) == 0o600
    assert all(secret not in report_text for secret in run.passwords)
