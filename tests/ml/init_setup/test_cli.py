import getpass
import hashlib
import json
import os
import pty
import select
import subprocess
import sys
import time
import warnings

import pytest

from bootstrap.init_cli import Cancelled, Prompt, configure, main
from bootstrap.init_config import build_plan
from bootstrap.init_files import SetupError, env_values


def answers(monkeypatch, mapping):
    calls = []

    def answer(label):
        calls.append(label)
        for prefix, value in mapping.items():
            if label.startswith(prefix):
                return value.pop(0) if isinstance(value, list) else value
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(getpass, "getpass", answer)
    return calls


def test_local_wizard_keeps_secrets_out_of_terminal_and_retains_existing(
    project, monkeypatch, capsys
):
    answers(monkeypatch, {"Write these files": "y"})
    plan = configure(project, project / ".local/ml-system", "compose", Prompt())
    path = project / ".env.ml"
    data = path.read_bytes()
    output = capsys.readouterr().out
    for key, value in env_values(data.decode()).items():
        if key.endswith("PASSWORD") or key == "APP_JWT_SECRET":
            assert value not in output
    assert plan.env_files == (".env.ml",) and (project / "var").is_dir()
    answers(monkeypatch, {"Existing file": "backup", "Write these files": "y"})
    configure(project, project / ".local/ml-system", "compose", Prompt())
    assert path.read_bytes() == data
    assert len(list(project.glob(".env.ml.bak-*"))) == 1


def test_existing_cancel_does_not_modify_or_create_other_outputs(project, monkeypatch):
    path = project / ".env.ml"
    data = (
        "ML_DB_PASSWORD=safeold\nMLFLOW_DB_PASSWORD=safeold2\n"
        "ML_MQ_PASSWORD=safeold3\nML_MONGO_PASSWORD=safeold4\nML_S3_PASSWORD=safeold5\n"
        "APP_JWT_SECRET=" + "j" * 64 + "\n"
    ).encode()
    path.write_bytes(data)
    path.chmod(0o600)
    answers(monkeypatch, {})
    with pytest.raises(Cancelled):
        configure(project, project / ".local/ml-system", "compose", Prompt())
    assert path.read_bytes() == data
    assert not (project / "var").exists() and not (project / ".local").exists()


@pytest.mark.parametrize("mode", ["token", "oauth"])
def test_databricks_wizard_outputs_compatible_secret_references(project, monkeypatch, capsys, mode):
    token = "dummy$TOKEN#'\\\\end"
    values = {
        "Configure Databricks": "y",
        "Databricks authentication": mode,
        "Workspace hostname": ["", "invalid.example", "dbc-approved.cloud.databricks.com"],
        "Warehouse HTTP path": "/sql/1.0/warehouses/0123abcd",
        "Databricks access token": token,
        "OAuth client ID": "principal",
        "OAuth client secret": "oauth-test-only",
        "Write these files": "y",
    }
    answers(monkeypatch, values)
    plan = configure(project, project / ".local/ml-system", "compose", Prompt())
    env = env_values((project / ".env.ml.ingestion").read_text())
    connection = json.loads((project / ".local/ml-system/databricks/connections.json").read_text())[
        "warehouse-main"
    ]
    captured = capsys.readouterr()
    assert token not in captured.out and "oauth-test-only" not in captured.out
    assert "Invalid value" in captured.err
    assert plan.pending_sources
    assert json.loads((project / ".local/ml-system/databricks/sources.json").read_text()) == {}
    assert json.loads((project / ".local/ml-system/databricks/access-policy.json").read_text()) == {
        "contract": "databricks-access-v1",
        "grants": [],
    }
    assert env["ML_DATABRICKS_POLICY_FILE"] == "/app/config/databricks-policy.json"
    if mode == "token":
        assert env["DATABRICKS_TOKEN"] == token
        assert connection["access_token_env"] == "DATABRICKS_TOKEN"
    else:
        assert connection["client_secret_file"] == "/run/secrets/databricks_client_secret"
        assert (
            project / ".local/ml-system/databricks/oauth-secret"
        ).read_text() == "oauth-test-only"


def test_switch_databricks_token_to_file_batch(project, monkeypatch):
    private = project / ".local/ml-system"
    answers(
        monkeypatch,
        {
            "Configure Databricks": "y",
            "Databricks authentication": "token",
            "Workspace hostname": "dbc-approved.cloud.databricks.com",
            "Warehouse HTTP path": "/sql/1.0/warehouses/0123abcd",
            "Databricks access token": "unused-after-switch",
            "Write these files": "y",
        },
    )
    configure(project, private, "compose", Prompt())
    path = project / ".env.ml.ingestion"
    original = path.read_text()
    assert env_values(original)["DATABRICKS_TOKEN"] == "unused-after-switch"
    unmanaged = '# Operator notes\nML_DATABRICKS_OPERATOR_HINT="first\nsecond"\n'
    path.write_text(original + unmanaged)

    answers(
        monkeypatch,
        {
            "Configure Databricks": "n",
            "Configure File Batch": "y",
            "Existing file": "overwrite",
            "Write these files": "y",
        },
    )
    plan = configure(project, private, "compose", Prompt())
    assert plan.compose_files == ("compose.ml.yaml", "compose.ml.ingestion.yaml")
    contents = path.read_text()
    assert contents.endswith(unmanaged)
    assert env_values(contents) == {
        "ML_INGESTION_INPUT_DIR": str(private / "ingestion-input"),
        "ML_DATABRICKS_OPERATOR_HINT": "first\nsecond",
    }


@pytest.mark.parametrize("profile", ["compose", "external"])
def test_switch_oauth_to_token_removes_stale_references(
    project, spec, external, files, monkeypatch, profile
):
    setup = external if profile == "external" else spec
    setup.connections = {
        "warehouse-main": {
            "server_hostname": "dbc-approved.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/0123abcd",
            "client_id": "oauth-principal",
            "client_secret_file": "/run/secrets/databricks_client_secret",
        }
    }
    setup.oauth_secret = "previous-oauth-secret"
    if profile == "compose":
        setup.environment["ML_INGESTION_INPUT_DIR"] = str(setup.private / "ingestion-input")
    original = build_plan(setup, files)
    files.write(
        original.outputs, dict.fromkeys(original.outputs, "overwrite"), original.directories
    )
    path = project / original.env_files[-1]
    oauth_path = setup.private / "databricks/oauth-secret"
    assert env_values(path.read_text())["ML_DATABRICKS_CLIENT_SECRET_FILE"] == str(oauth_path)

    answers(
        monkeypatch,
        {
            "Databricks authentication": "token",
            "Databricks access token": "replacement-token",
            "Existing file": "overwrite",
            "Write these files": "y",
        },
    )
    plan = configure(project, setup.private, profile, Prompt())
    env = env_values(path.read_text())
    assert env["DATABRICKS_TOKEN"] == "replacement-token"
    if profile == "external":
        assert env["ML_DATABRICKS_CLIENT_SECRET_FILE"] == "/dev/null"
    else:
        assert "ML_DATABRICKS_CLIENT_SECRET_FILE" not in env
        assert plan.compose_files[-1] == "compose.ml.databricks-token.yaml"
    connection = json.loads((setup.private / "databricks/connections.json").read_text())[
        "warehouse-main"
    ]
    assert connection == {
        "server_hostname": "dbc-approved.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/0123abcd",
        "access_token_env": "DATABRICKS_TOKEN",
    }
    assert oauth_path.read_text() == "previous-oauth-secret"


def test_final_cancel_leaves_new_setup_unwritten(project, monkeypatch):
    answers(monkeypatch, {})
    with pytest.raises(Cancelled):
        configure(project, project / ".local/ml-system", "compose", Prompt())
    assert not (project / ".env.ml").exists() and not (project / "var").exists()


def test_managed_env_references_require_resolution_but_unknown_lines_survive(project, monkeypatch):
    path = project / ".env.ml"
    original = "APP_JWT_SECRET=${LONG_SIGNING_KEY_REFERENCE_FROM_ENV}\n"
    path.write_text(original)
    path.chmod(0o600)
    answers(monkeypatch, {"Write these files": "y", "Existing file": "overwrite"})
    with pytest.raises(SetupError, match="variable references"):
        configure(project, project / ".local/ml-system", "compose", Prompt())
    assert path.read_text() == original
    path.write_text("UNMANAGED=${KEEP_ME}\n")
    configure(project, project / ".local/ml-system", "compose", Prompt())
    assert "UNMANAGED=${KEEP_ME}\n" in path.read_text()


def test_existing_private_directory_must_be_owner_only(project, monkeypatch):
    private = project / ".local/ml-system"
    private.mkdir(parents=True)
    private.chmod(0o755)
    with pytest.raises(SetupError, match="0700"):
        configure(project, private, "compose", Prompt())


def test_hidden_defaults_and_getpass_failure(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(getpass, "getpass", lambda label: seen.append(label) or "")
    assert Prompt().ask("Token", "private-existing", secret=True) == "private-existing"
    assert "private-existing" not in "".join(seen)

    def cannot_hide(label):
        warnings.warn("terminal cannot hide input", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("No plaintext fallback is permitted")

    monkeypatch.setattr(getpass, "getpass", cannot_hide)
    with pytest.raises(getpass.GetPassWarning):
        Prompt().ask("Token", secret=True)
    assert "private-existing" not in capsys.readouterr().out


def test_nonterminal_and_interrupt_are_safe(monkeypatch, capsys, project):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["--project-dir", str(project)]) == 2
    assert "terminal" in capsys.readouterr().err
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    assert main(["--project-dir", str(project)]) == 130
    assert not (project / ".env.ml").exists()


def test_entrypoint_init_help_ignores_runtime_settings_and_heavy_sdks(project):
    code = """import importlib.abc,sys,runpy
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,*args):
        if fullname.split('.')[0] in {'torch','numpy','scipy','mlflow','databricks'}:
            raise AssertionError('Setup imported optional compute/connector SDK')
sys.meta_path.insert(0,Block())
sys.argv=['ml-system','init','--help']
runpy.run_module('bootstrap.ml_cli',run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "ML_MODEL_ENVIRONMENT": "invalid", "ML_ACK_TIMEOUT_MS": "invalid"},
    )
    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout
    standalone = subprocess.run(
        [sys.executable, "-m", "bootstrap.init_cli", "--help"], capture_output=True, text=True
    )
    assert standalone.returncode == 0 and "--private-dir" in standalone.stdout


def test_credentials_are_not_cli_arguments(project):
    result = subprocess.run(
        [sys.executable, "-m", "bootstrap.init_cli", "--token", "do-not-pass-secrets"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "do-not-pass-secrets" not in result.stdout + result.stderr


def test_real_terminal_hides_secret_echo():
    secret = b"pty-test-only$token"
    expected = hashlib.sha256(secret).hexdigest().encode()
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from bootstrap.init_cli import Prompt;import hashlib;"
            "value=Prompt().ask('Token',secret=True);print(hashlib.sha256(value.encode()).hexdigest())",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = b""
    sent = False
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                output += chunk
                if b"Token: " in output and not sent:
                    os.write(master, secret + b"\n")
                    sent = True
                if expected in output:
                    break
        assert child.wait(timeout=5) == 0
        assert expected in output and secret not in output
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)


def test_external_wizard_keeps_credentials_hidden(project, external, monkeypatch, capsys):
    values = {
        "Approved release image": external.environment["ML_RELEASE_IMAGE"],
        "Approved image source": external.environment["ML_CODE_REVISION"],
        "MLflow HTTPS endpoint": "https://tracking.example",
        "Approved MLflow CA PEM": external.environment["ML_MLFLOW_CA_FILE"],
        "App PostgreSQL URI": external.private_values["ML_DATABASE_URL"],
        "RabbitMQ URI": external.private_values["ML_RABBITMQ_URL"],
        "Redis URI": external.private_values["ML_REDIS_URL"],
        "Mongo Identity URI": external.private_values["APP_MONGO_URI"],
        "MLflow writer username": "writer",
        "MLflow writer password": "private-test-only-writer",
        "MLflow reader authentication": "token",
        "MLflow reader token": "private-test-only-reader",
        "Databricks authentication": "token",
        "Workspace hostname": "dbc-approved.cloud.databricks.com",
        "Warehouse HTTP path": "/sql/1.0/warehouses/0123abcd",
        "Databricks access token": "private-test-only-databricks",
        "Write these files": "y",
    }
    answers(monkeypatch, values)
    plan = configure(project, project / ".local/ml-system", "external", Prompt())
    assert plan.env_files == (".env.ml.production",)
    rendered = (project / ".env.ml.production").read_text()
    captured = capsys.readouterr()
    for value in [
        *(v for k, v in external.private_values.items() if k != "MLFLOW_TRACKING_USERNAME"),
        "private-test-only-writer",
        "private-test-only-reader",
        "private-test-only-databricks",
    ]:
        assert value not in captured.out + captured.err
    assert "ML_DATABASE_URL" not in rendered


@pytest.mark.parametrize(
    "assignment",
    [
        "APP_JWT_SECRET=existing-private-value",
        "export 'MLFLOW_TRACKING_TOKEN'=existing-private-value",
        "APP_JWT_SECRET:existing-private-value",
    ],
)
def test_external_rejects_private_keys_in_public_env(
    external, files, monkeypatch, capsys, assignment
):
    plan = build_plan(external, files)
    files.write(plan.outputs, dict.fromkeys(plan.outputs, "overwrite"), plan.directories)
    path = external.project / ".env.ml.production"
    path.write_text(path.read_text() + assignment + "\n")
    original = {output: output.read_bytes() for output in plan.outputs}
    calls = answers(monkeypatch, {"Existing file": "backup", "Write these files": "y"})

    with pytest.raises(SetupError, match="Production credentials belong only in private role JSON"):
        configure(external.project, external.private, "external", Prompt())

    assert not calls
    assert {output: output.read_bytes() for output in original} == original
    assert not list(external.project.rglob("*.bak-*"))
    captured = capsys.readouterr()
    assert "existing-private-value" not in captured.out + captured.err


@pytest.mark.parametrize("writer_mode", ["", "token"])
def test_external_wizard_retains_mixed_role_auth_unless_explicitly_replaced(
    external, files, monkeypatch, writer_mode
):
    plan = build_plan(external, files)
    files.write(plan.outputs, dict.fromkeys(plan.outputs, "overwrite"), plan.directories)
    roles = external.private / "roles"
    writer = "MLFLOW_TRACKING_"
    reader = "ML_SERVING_MLFLOW_"
    expected = {}
    for role in ("supervisor", "control", "operator"):
        path = roles / (role + ".json")
        config = json.loads(path.read_text())
        if role == "supervisor":
            del config[writer + "USERNAME"], config[writer + "PASSWORD"]
            config[writer + "TOKEN"] = "supervisor-test-token"
        if role == "operator":
            del config[reader + "TOKEN"]
            config.update(
                ML_SERVING_MLFLOW_USERNAME="operator-reader",
                ML_SERVING_MLFLOW_PASSWORD="operator-reader-test-password",
            )
        path.write_text(json.dumps(config))
        if writer_mode == "token":
            config.pop(writer + "USERNAME", None)
            config.pop(writer + "PASSWORD", None)
            config[writer + "TOKEN"] = "supervisor-test-token"
        expected[path] = config

    answers(
        monkeypatch,
        {
            "MLflow writer authentication": writer_mode,
            "Existing file": "overwrite",
            "Write these files": "y",
        },
    )
    configure(external.project, external.private, "external", Prompt())
    assert {path: json.loads(path.read_text()) for path in expected} == expected
