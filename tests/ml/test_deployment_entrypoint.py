"""Production private configuration, TLS and per-role credential boundaries."""

import json
import os
import ssl
import stat
import sys
from pathlib import Path

import certifi
import pytest

from bootstrap import deployment_entrypoint as entrypoint


@pytest.fixture
def deployment_environment(tmp_path):
    certificate = tmp_path / "mlflow-ca.pem"
    certificate.write_bytes(Path(certifi.where()).read_bytes())
    private = tmp_path / "runtime.json"
    private.write_text(
        json.dumps(
            {
                "ML_DATABASE_URL": "postgresql+psycopg://operator:test@database.example/release",
                "APP_MONGO_URI": "mongodb://identity:test@mongo.example/identity",
                "APP_JWT_SECRET": "test-signing-material-" + "a" * 32,
                "MLFLOW_TRACKING_USERNAME": "release-writer",
                "MLFLOW_TRACKING_PASSWORD": "private-writer-password",
                "ML_SERVING_MLFLOW_TOKEN": "private-read-only-token",
            }
        )
    )
    private.chmod(0o600)
    return {
        "PATH": os.environ["PATH"],
        "ML_RUNTIME_ROLE": "operator",
        "ML_RUNTIME_CONFIG_FILE": str(private),
        "ML_MODEL_ENVIRONMENT": "prod",
        "ML_TRACKER_BACKEND": "mlflow",
        "ML_IMAGE_DIGEST": "sha256:" + "a" * 64,
        "ML_CODE_REVISION": "b" * 40,
        "ML_TRACKING_URI": "https://tracking.example/mlflow",
        "MLFLOW_TRACKING_URI": "https://tracking.example/mlflow",
        "MLFLOW_TRACKING_INSECURE_TLS": "false",
        "MLFLOW_TRACKING_SERVER_CERT_PATH": str(certificate),
        "ML_EXECUTION_ROOT": str(tmp_path / "runtime/executions"),
        "HOME": str(tmp_path / "parent-home"),
    }


def update_private(environment, **changes):
    path = Path(environment["ML_RUNTIME_CONFIG_FILE"])
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value))


def test_private_configuration_resolves_without_mutating_or_printing_parent(
    deployment_environment, capsys
):
    original = dict(deployment_environment)
    result = entrypoint.load_runtime_environment(original)
    assert original == deployment_environment
    assert "ML_DATABASE_URL" not in original
    assert result["MLFLOW_TRACKING_PASSWORD"] == "private-writer-password"
    assert result["ML_DEPLOYMENT_ROLE"] == "management"
    entrypoint.configure_runtime_home(result)
    home = Path(result["HOME"])
    assert home != Path(original["HOME"])
    assert home.parent.parent == Path(original["ML_EXECUTION_ROOT"])
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    (home / "cache").write_text("private")
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666, 0o700])
def test_role_secret_file_rejects_nonprivate_or_executable_modes(deployment_environment, mode):
    Path(deployment_environment["ML_RUNTIME_CONFIG_FILE"]).chmod(mode)
    with pytest.raises(ValueError, match="ownership, mode or size"):
        entrypoint.load_runtime_environment(deployment_environment)


def test_role_secret_file_rejects_different_owner(deployment_environment, monkeypatch):
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(ValueError, match="ownership, mode or size"):
        entrypoint.load_runtime_environment(deployment_environment)


@pytest.mark.parametrize("kind", ["link", "fifo", "directory", "oversize"])
def test_role_secret_special_files_fail_before_parsing(deployment_environment, tmp_path, kind):
    target = tmp_path / "invalid-config"
    if kind == "link":
        target.symlink_to(deployment_environment["ML_RUNTIME_CONFIG_FILE"])
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    elif kind == "directory":
        target.mkdir(mode=0o700)
    else:
        target.write_bytes(b"x" * 65537)
        target.chmod(0o600)
    deployment_environment["ML_RUNTIME_CONFIG_FILE"] = str(target)
    with pytest.raises((OSError, ValueError)):
        entrypoint.load_runtime_environment(deployment_environment)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ML_MODEL_ENVIRONMENT", "dev"),
        ("ML_CODE_REVISION", "development"),
        ("PATH", "/unapproved/bin"),
        ("AWS_SECRET_ACCESS_KEY", "cloud-secret"),
        ("MLFLOW_TRACKING_PASSWORD", {"nested": "not-a-scalar"}),
    ],
)
def test_private_json_cannot_override_release_or_inject_unapproved_values(
    deployment_environment, key, value
):
    update_private(deployment_environment, **{key: value})
    with pytest.raises(ValueError):
        entrypoint.load_runtime_environment(deployment_environment)


def test_private_json_rejects_duplicate_keys(deployment_environment):
    path = Path(deployment_environment["ML_RUNTIME_CONFIG_FILE"])
    path.write_text('{"ML_DATABASE_URL":"first","ML_DATABASE_URL":"second"}')
    with pytest.raises(ValueError, match="Duplicate"):
        entrypoint.load_runtime_environment(deployment_environment)


@pytest.mark.parametrize("role", ["migrate", "management", "relay", "gateway", "ingestion-worker"])
def test_writer_or_serving_secrets_are_rejected_for_nonwriter_roles(deployment_environment, role):
    deployment_environment["ML_RUNTIME_ROLE"] = role
    with pytest.raises(ValueError, match="overprivileged"):
        entrypoint.load_runtime_environment(deployment_environment)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ML_MODEL_ENVIRONMENT", "dev"),
        ("ML_TRACKER_BACKEND", "local"),
        ("ML_IMAGE_DIGEST", "latest"),
        ("ML_CODE_REVISION", "unqualified"),
        ("ML_TRACKING_URI", "http://tracking.example"),
        ("MLFLOW_TRACKING_URI", "https://other.example"),
        ("MLFLOW_TRACKING_INSECURE_TLS", "true"),
        ("MLFLOW_TRACKING_SERVER_CERT_PATH", "/does/not/exist.pem"),
        ("ML_RUNTIME_ROLE", "serving-unapproved"),
    ],
)
def test_invalid_production_selection_fails_closed(deployment_environment, key, value):
    deployment_environment[key] = value
    with pytest.raises((OSError, ValueError, ssl.SSLError)):
        entrypoint.load_runtime_environment(deployment_environment)


def test_credential_mechanisms_and_read_identity_must_be_distinct(deployment_environment):
    update_private(deployment_environment, MLFLOW_TRACKING_TOKEN="mixed-mechanism")
    with pytest.raises(ValueError, match="credential mechanism"):
        entrypoint.load_runtime_environment(deployment_environment)
    path = Path(deployment_environment["ML_RUNTIME_CONFIG_FILE"])
    values = json.loads(path.read_text())
    del values["MLFLOW_TRACKING_TOKEN"]
    del values["ML_SERVING_MLFLOW_TOKEN"]
    values.update(
        ML_SERVING_MLFLOW_USERNAME=values["MLFLOW_TRACKING_USERNAME"],
        ML_SERVING_MLFLOW_PASSWORD=values["MLFLOW_TRACKING_PASSWORD"],
    )
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="distinct"):
        entrypoint.load_runtime_environment(deployment_environment)


def test_plaintext_container_environment_cannot_bypass_private_file(deployment_environment):
    deployment_environment["MLFLOW_TRACKING_TOKEN"] = "never-print-this-token"
    with pytest.raises(ValueError, match="configuration file"):
        entrypoint.load_runtime_environment(deployment_environment)


def test_password_rotation_does_not_make_the_same_basic_principal_readonly(deployment_environment):
    path = Path(deployment_environment["ML_RUNTIME_CONFIG_FILE"])
    values = json.loads(path.read_text())
    del values["ML_SERVING_MLFLOW_TOKEN"]
    values.update(
        ML_SERVING_MLFLOW_USERNAME=values["MLFLOW_TRACKING_USERNAME"],
        ML_SERVING_MLFLOW_PASSWORD="different-password-same-principal",
    )
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="distinct"):
        entrypoint.load_runtime_environment(deployment_environment)


def test_entrypoint_error_never_prints_secret_payload(deployment_environment, monkeypatch, capsys):
    update_private(deployment_environment, APP_JWT_SECRET="never-print-this-signing-key")
    monkeypatch.setattr(entrypoint.os, "environ", deployment_environment)
    monkeypatch.setattr(sys, "argv", ["entrypoint", "python", "-c", "pass"])
    assert entrypoint.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"DEPLOYMENT_CONFIGURATION_REJECTED"}\n'


def test_entrypoint_exec_preserves_command_and_only_resolves_approved_configuration(
    deployment_environment, monkeypatch
):
    observed = []
    monkeypatch.setattr(entrypoint.os, "environ", deployment_environment)
    monkeypatch.setattr(sys, "argv", ["entrypoint", "python", "-m", "bootstrap.ml_cli", "migrate"])
    monkeypatch.setattr(
        entrypoint.os, "execvpe", lambda command, args, env: observed.append((command, args, env))
    )
    assert entrypoint.main() == 0
    command, args, env = observed[0]
    assert command == "python" and args == sys.argv[1:]
    assert env["ML_IMAGE_DIGEST"] == deployment_environment["ML_IMAGE_DIGEST"]
    assert env["ML_SERVING_MLFLOW_TOKEN"] == "private-read-only-token"
