"""Deployment checks keep offline configuration and external qualification distinct."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap import deployment_cli as cli

DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.example/ml-runtime@" + DIGEST
REVISION = "b" * 40
URI = "https://tracking.example:8443"


def model_and_inspection():
    model = {
        "services": {
            role: {
                "image": IMAGE,
                "user": "1001:1001",
                "mem_limit": 512 * 1024 * 1024,
                "cpus": 1.0,
                "pids_limit": 128,
                "entrypoint": ["python", "-m", "bootstrap.deployment_entrypoint"],
                "secrets": [{"source": "runtime_" + role, "target": "runtime.json"}],
                "configs": [{"source": "ca", "target": "/run/config/ca.pem"}],
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/private-runtime",
                        "target": "/app/var",
                        "bind": {"create_host_path": False},
                    }
                ],
                "environment": {
                    "ML_MODEL_ENVIRONMENT": "prod",
                    "ML_TRACKER_BACKEND": "mlflow",
                    "ML_IMAGE_DIGEST": DIGEST,
                    "ML_CODE_REVISION": REVISION,
                    "ML_TRACKING_URI": URI,
                    "MLFLOW_TRACKING_URI": URI,
                    "ML_RUNTIME_ROLE": role,
                    "ML_RUNTIME_CONFIG_FILE": "/run/secrets/runtime.json",
                    "MLFLOW_TRACKING_SERVER_CERT_PATH": "/run/config/ca.pem",
                },
            }
            for role in cli.REQUIRED_ROLES
        }
    }
    model.update(
        secrets={
            "runtime_" + role: {"file": "/private/" + role + ".json"} for role in cli.REQUIRED_ROLES
        },
        configs={"ca": {"file": "/approved/ca.pem"}},
    )
    inspection = {
        "Os": "linux",
        "Architecture": "amd64",
        "Id": "sha256:" + "c" * 64,
        "RepoDigests": [IMAGE],
        "Config": {"Labels": {"org.opencontainers.image.revision": REVISION}},
    }
    return model, inspection


def test_preflight_checks_repository_digest_not_image_config_id():
    model, inspection = model_and_inspection()
    report = cli.preflight(model, inspection)
    assert report["status"] == "CONFIGURATION_PREFLIGHT_PASSED"
    assert report["connectivity_qualified"] is False
    assert report["production_accepted"] is False
    assert report["image_ref"] == IMAGE


@pytest.mark.parametrize(
    "kind",
    [
        "role-secret",
        "oauth",
        "ca",
        "definitions",
        "different-root",
        "different-owner",
        "extra-writable",
        "auto-create",
    ],
)
def test_preflight_rejects_secret_aliases_and_inconsistent_shared_storage(kind):
    model, inspection = model_and_inspection()
    service = model["services"]["control"]
    if kind == "role-secret":
        model["secrets"]["runtime_control"]["file"] = "/private-runtime/secret.json"
    elif kind == "oauth":
        model["secrets"]["oauth"] = {"file": "/private-runtime/oauth-secret"}
    elif kind == "ca":
        model["configs"]["ca"]["file"] = "/private-runtime/ca.pem"
    elif kind == "definitions":
        service["volumes"].append(
            {
                "type": "bind",
                "source": "/private-runtime/definitions",
                "target": "/run/operator-inputs",
                "read_only": True,
            }
        )
    elif kind == "different-root":
        service["volumes"][0]["source"] = "/different-runtime"
    elif kind == "different-owner":
        service["user"] = "2000:2000"
    elif kind == "extra-writable":
        service["volumes"].append(
            {"type": "bind", "source": "/private", "target": "/unapproved-writable-alias"}
        )
    else:
        service["volumes"][0]["bind"]["create_host_path"] = True
    with pytest.raises(cli.QualificationError):
        cli.preflight(model, inspection)


def test_preflight_detects_host_symlink_alias_to_private_runtime(tmp_path):
    model, inspection = model_and_inspection()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    alias = tmp_path / "apparently-private-input"
    alias.symlink_to(runtime, target_is_directory=True)
    for service in model["services"].values():
        service["volumes"][0]["source"] = str(runtime)
    model["configs"]["ca"]["file"] = str(alias / "ca.pem")
    with pytest.raises(cli.QualificationError, match="WRITABLE_RUNTIME_ALIAS"):
        cli.preflight(model, inspection)


@pytest.mark.parametrize(
    "case",
    [
        "missing_role",
        "internal_tracking",
        "build",
        "tag_only",
        "declared_digest",
        "dev",
        "unqualified_code",
        "different_image",
        "insecure_uri",
        "uri_credentials",
        "sdk_mismatch",
        "tls_disabled",
        "plaintext_secret",
        "root",
        "missing_limits",
        "internal_dependency",
        "wrong_pulled_image",
        "wrong_revision_label",
        "unexpected_service",
        "unbounded_cpu",
        "unbounded_processes",
        "missing_config",
        "missing_ca",
        "wrong_runtime_role",
        "code_digest",
        "wrong_os",
        "wrong_architecture",
        "wrong_entrypoint",
        "shared_config_source",
        "shared_config_file",
    ],
)
def test_preflight_rejects_unsafe_or_inconsistent_release(case):
    model, inspection = model_and_inspection()
    service = model["services"]["control"]
    environment = service["environment"]
    if case == "missing_role":
        del model["services"]["operator"]
    elif case == "internal_tracking":
        model["services"]["mlflow"] = {}
    elif case == "build":
        service["build"] = {"context": "."}
    elif case == "tag_only":
        service["image"] = "registry.example/ml-runtime:latest"
    elif case == "declared_digest":
        environment["ML_IMAGE_DIGEST"] = "sha256:" + "d" * 64
    elif case == "dev":
        environment["ML_MODEL_ENVIRONMENT"] = "dev"
    elif case == "unqualified_code":
        environment["ML_CODE_REVISION"] = "development"
    elif case == "different_image":
        service["image"] = "other.example/runtime@" + DIGEST
    elif case == "insecure_uri":
        environment.update(
            ML_TRACKING_URI="http://tracking.example", MLFLOW_TRACKING_URI="http://tracking.example"
        )
    elif case == "uri_credentials":
        environment.update(
            ML_TRACKING_URI="https://user:secret@tracking.example",
            MLFLOW_TRACKING_URI="https://user:secret@tracking.example",
        )
    elif case == "sdk_mismatch":
        environment["MLFLOW_TRACKING_URI"] = "https://different.example"
    elif case == "tls_disabled":
        environment["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    elif case == "plaintext_secret":
        environment["MLFLOW_TRACKING_PASSWORD"] = "never-print-me"
    elif case == "root":
        service["user"] = "0:0"
    elif case == "missing_limits":
        del service["mem_limit"]
    elif case == "internal_dependency":
        service["depends_on"] = {"mlflow": {"condition": "service_healthy"}}
    elif case == "wrong_pulled_image":
        inspection["RepoDigests"] = ["other.example/runtime@" + DIGEST]
    elif case == "wrong_revision_label":
        inspection["Config"]["Labels"]["org.opencontainers.image.revision"] = "d" * 40
    elif case == "unexpected_service":
        model["services"]["postgres"] = {}
    elif case == "unbounded_cpu":
        service["cpus"] = "nan"
    elif case == "unbounded_processes":
        service["pids_limit"] = 0
    elif case == "missing_config":
        service["secrets"] = []
    elif case == "missing_ca":
        service["configs"] = []
    elif case == "wrong_runtime_role":
        environment["ML_RUNTIME_ROLE"] = "management"
    elif case == "code_digest":
        environment["ML_CODE_REVISION"] = DIGEST
    elif case == "wrong_os":
        inspection["Os"] = "windows"
    elif case == "wrong_architecture":
        inspection["Architecture"] = "arm64"
    elif case == "wrong_entrypoint":
        service["entrypoint"] = ["python", "-m", "bootstrap.ml_cli"]
    elif case == "shared_config_source":
        service["secrets"][0]["source"] = "runtime_management"
    else:
        model["secrets"]["runtime_control"]["file"] = "/private/management.json"
    with pytest.raises(cli.QualificationError):
        cli.preflight(model, inspection)


def writer_environment():
    return {
        "ML_TRACKING_URI": URI,
        "MLFLOW_TRACKING_URI": URI,
        "ML_CODE_REVISION": REVISION,
        "ML_IMAGE_DIGEST": DIGEST,
        "MLFLOW_TRACKING_USERNAME": "writer",
        "MLFLOW_TRACKING_PASSWORD": "writer-secret",
        "ML_SERVING_MLFLOW_USERNAME": "reader",
        "ML_SERVING_MLFLOW_PASSWORD": "reader-secret",
        "ML_DATABASE_URL": "postgresql://private-db",
        "APP_JWT_SECRET": "private-jwt",
        "AWS_SECRET_ACCESS_KEY": "private-cloud",
        "DATABRICKS_CLIENT_SECRET": "private-db-x",
        "HOME": "/parent-private-home",
        "PATH": os.environ.get("PATH", ""),
    }


def test_reader_environment_uses_only_scoped_identity_and_private_home(tmp_path):
    parent = writer_environment()
    result = cli.reader_environment(parent, tmp_path)
    assert result["HOME"] == str(tmp_path)
    assert result["MLFLOW_TRACKING_PASSWORD"] == "reader-secret"
    assert result["MLFLOW_TRACKING_USERNAME"] == "reader"
    assert result["MLFLOW_TRACKING_URI"] == URI
    assert not any(
        key in result
        for key in (
            "ML_DATABASE_URL",
            "APP_JWT_SECRET",
            "AWS_SECRET_ACCESS_KEY",
            "DATABRICKS_CLIENT_SECRET",
            "ML_SERVING_MLFLOW_PASSWORD",
        )
    )
    assert "writer-secret" not in result.values()


@pytest.mark.parametrize("case", ["missing", "both", "partial", "partial_with_token", "tls"])
def test_reader_requires_one_complete_scoped_identity(tmp_path, case):
    parent = writer_environment()
    if case == "missing":
        del parent["ML_SERVING_MLFLOW_USERNAME"]
        del parent["ML_SERVING_MLFLOW_PASSWORD"]
    elif case == "both":
        parent["ML_SERVING_MLFLOW_TOKEN"] = "reader-token"
    elif case in {"partial", "partial_with_token"}:
        del parent["ML_SERVING_MLFLOW_PASSWORD"]
        if case == "partial_with_token":
            parent["ML_SERVING_MLFLOW_TOKEN"] = "reader-token"
    else:
        parent["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    with pytest.raises(cli.QualificationError):
        cli.reader_environment(parent, tmp_path)


class Client:
    def __init__(self, *, readonly=False, deny_code="PERMISSION_DENIED"):
        self.readonly = readonly
        self.deny_code = deny_code
        self.tags = {}
        self.data = b""
        self.finished = []
        self.writes = []

    def get_experiment_by_name(self, name):
        return SimpleNamespace(experiment_id="1")

    def create_run(self, experiment_id, tags):
        self.tags = dict(tags)
        return SimpleNamespace(
            info=SimpleNamespace(run_id="f" * 32, artifact_uri="mlflow-artifacts:/1/run/artifacts")
        )

    def log_metric(self, *args, **kwargs):
        self.writes.append("metric")
        if self.readonly:
            error = RuntimeError("SDK exception with never-print-me")
            error.error_code = self.deny_code
            raise error

    def log_artifact(self, run_id, path, **kwargs):
        self.data = Path(path).read_bytes()

    def download_artifacts(self, run_id, artifact_path, directory):
        destination = Path(directory) / "proof.json"
        destination.write_bytes(self.data)
        return str(destination)

    def create_registered_model(self, name, tags):
        self.model_name = name

    def create_model_version(self, name, source, run_id, **kwargs):
        assert source == "runs:/" + "f" * 32 + "/day1"
        return SimpleNamespace(version="1", status="READY")

    def get_run(self, run_id):
        return SimpleNamespace(
            data=SimpleNamespace(tags=self.tags),
            info=SimpleNamespace(artifact_uri="mlflow-artifacts:/1/run/artifacts"),
        )

    def get_model_version(self, name, version):
        return SimpleNamespace(
            tags=self.tags, status="READY", run_id="f" * 32, source="runs:/" + "f" * 32 + "/day1"
        )

    def set_model_version_tag(self, *args):
        self.writes.append("model-tag")
        if self.readonly:
            error = RuntimeError("SDK exception with never-print-me")
            error.error_code = self.deny_code
            raise error

    def set_terminated(self, run_id, status):
        self.finished.append(status)


def test_mlflow_qualification_retains_records_and_proves_reader_denials(tmp_path, monkeypatch):
    writer = Client()
    reader_client = Client(readonly=True)
    seen_environment = {}

    def reader(request_path, response_path, environment, timeout):
        seen_environment.update(environment)
        reader_client.tags = writer.tags
        reader_client.data = writer.data
        monkeypatch.setenv("ML_TRACKING_URI", environment["ML_TRACKING_URI"])
        monkeypatch.setenv("MLFLOW_TRACKING_URI", environment["MLFLOW_TRACKING_URI"])
        response = cli.verify_reader(cli.load_object(request_path), lambda uri: reader_client)
        cli.write_report(response_path, response)

    report = cli.qualify_mlflow(
        "approved-qualification",
        "approved",
        factory=lambda uri: writer,
        reader=reader,
        environment=writer_environment(),
    )
    assert report["status"] == "MLFLOW_DEPENDENCY_QUALIFIED"
    assert report["production_accepted"] is False
    assert report["artifact_sha256"] == hashlib.sha256(writer.data).hexdigest()
    assert writer.finished == ["FINISHED"]
    assert reader_client.writes == ["metric", "model-tag"]
    assert "AWS_SECRET_ACCESS_KEY" not in seen_environment
    assert "writer-secret" not in json.dumps(report)
    assert report["registered_model_name"].startswith("approved-qualification-")


@pytest.mark.parametrize("denial", [None, "UNAUTHENTICATED", "INTERNAL_ERROR"])
def test_reader_requires_explicit_permission_denial_not_generic_failure(monkeypatch, denial):
    client = Client(readonly=denial is not None, deny_code=denial)
    identifier = "a" * 32
    client.tags = {"qualification_id": identifier, "purpose": "day1-dependency-qualification"}
    client.data = b"proof"
    request = {
        "qualification_id": identifier,
        "run_id": "f" * 32,
        "registered_model_name": "approved-qualification-" + identifier,
        "model_version": "1",
        "artifact_sha256": hashlib.sha256(client.data).hexdigest(),
    }
    monkeypatch.setenv("ML_TRACKING_URI", URI)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", URI)
    with pytest.raises(cli.QualificationError):
        cli.verify_reader(request, lambda uri: client)


def test_reader_refuses_to_probe_nonqualification_model(monkeypatch):
    monkeypatch.setenv("ML_TRACKING_URI", URI)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", URI)
    with pytest.raises(cli.QualificationError):
        cli.verify_reader({"qualification_id": "a" * 32, "registered_model_name": "production"})


def test_failed_sdk_qualification_is_safe_and_keeps_failed_run():
    writer = Client()

    def broken_reader(*args):
        raise RuntimeError("https://writer:never-print-me@tracking.example")

    report = cli.qualify_mlflow(
        "approved",
        "approved",
        factory=lambda uri: writer,
        reader=broken_reader,
        environment=writer_environment(),
    )
    assert report["status"] == "MLFLOW_NOT_QUALIFIED"
    assert report["error_code"] == "MLFLOW_QUALIFICATION_FAILED"
    assert report["stage"] == "serving-reader"
    assert writer.finished == ["FAILED"]
    assert "never-print-me" not in json.dumps(report)
    assert report["run_id"] == "f" * 32


@pytest.mark.parametrize("case", ["tls_bypass", "missing_writer", "mixed_writer", "partial_writer"])
def test_transport_and_writer_identity_fail_before_any_remote_mutation(case):
    parent = writer_environment()
    if case == "tls_bypass":
        parent["MLFLOW_TRACKING_INSECURE_TLS"] = "true"
    elif case == "missing_writer":
        del parent["MLFLOW_TRACKING_USERNAME"]
        del parent["MLFLOW_TRACKING_PASSWORD"]
    elif case == "mixed_writer":
        parent["MLFLOW_TRACKING_TOKEN"] = "writer-token"
    else:
        del parent["MLFLOW_TRACKING_PASSWORD"]

    def forbidden_client(uri):
        pytest.fail("Invalid configuration must not reach MLflow")

    if case == "tls_bypass":
        report = cli.qualify_mlflow(
            "approved", "approved", factory=forbidden_client, environment=parent
        )
        assert report["status"] == "MLFLOW_NOT_QUALIFIED"
        assert report["stage"] == "configuration"
    else:
        with pytest.raises(cli.QualificationError):
            cli.qualify_mlflow("approved", "approved", factory=forbidden_client, environment=parent)


@pytest.mark.parametrize("case", ["different_run", "different_source", "different_proxy"])
def test_reader_does_not_probe_writes_for_a_different_model_origin(monkeypatch, case):
    identifier = "a" * 32
    client = Client(readonly=True)
    client.tags = {"qualification_id": identifier, "purpose": "day1-dependency-qualification"}
    version = client.get_model_version("unused", "1")
    if case == "different_run":
        version.run_id = "b" * 32
    elif case == "different_source":
        version.source = "runs:/" + "f" * 32 + "/different-artifact"
    else:
        version.source = "mlflow-artifacts:/1/another-run/artifacts/day1"
    monkeypatch.setattr(client, "get_model_version", lambda *args: version)
    monkeypatch.setenv("ML_TRACKING_URI", URI)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", URI)
    request = {
        "qualification_id": identifier,
        "run_id": "f" * 32,
        "registered_model_name": "approved-qualification-" + identifier,
        "model_version": "1",
        "artifact_sha256": "a" * 64,
    }
    with pytest.raises(cli.QualificationError, match="QUALIFICATION_MODEL_SOURCE_MISMATCH"):
        cli.verify_reader(request, lambda uri: client)
    assert client.writes == []


def test_reader_accepts_only_proxy_source_derived_from_same_authenticated_run(monkeypatch):
    identifier = "a" * 32
    client = Client(readonly=True)
    client.tags = {"qualification_id": identifier, "purpose": "day1-dependency-qualification"}
    client.data = b"proof"
    version = client.get_model_version("unused", "1")
    version.source = client.get_run("f" * 32).info.artifact_uri + "/day1"
    monkeypatch.setattr(client, "get_model_version", lambda *args: version)
    monkeypatch.setenv("ML_TRACKING_URI", URI)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", URI)
    request = {
        "qualification_id": identifier,
        "run_id": "f" * 32,
        "registered_model_name": "approved-qualification-" + identifier,
        "model_version": "1",
        "artifact_sha256": hashlib.sha256(client.data).hexdigest(),
    }
    report = cli.verify_reader(request, lambda uri: client)
    assert report["status"] == "SERVING_READ_IDENTITY_PASSED"
    assert client.writes == ["metric", "model-tag"]


def test_preflight_failure_writes_safe_no_go_report(tmp_path, capsys):
    model, inspection = model_and_inspection()
    model["services"]["control"]["environment"]["MLFLOW_TRACKING_PASSWORD"] = "never-print-me"
    model_path, image_path, report_path = (
        tmp_path / name for name in ("model.json", "image.json", "report.json")
    )
    model_path.write_text(json.dumps(model))
    image_path.write_text(json.dumps(inspection))
    assert (
        cli.main(
            [
                "preflight",
                "--compose-config",
                str(model_path),
                "--image-inspect",
                str(image_path),
                "--output",
                str(report_path),
            ]
        )
        == 1
    )
    assert "never-print-me" not in capsys.readouterr().out
    assert cli.load_object(report_path)["error_code"] == "FILE_MOUNTED_SECRETS_REQUIRED"


def test_existing_report_does_not_trigger_external_writes(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text('{"keep":"original"}')
    monkeypatch.setattr(cli, "qualify_mlflow", lambda *args: pytest.fail("Must not mutate MLflow"))
    assert (
        cli.main(
            [
                "qualify-mlflow",
                "--experiment-name",
                "approved",
                "--registered-model-prefix",
                "approved",
                "--output",
                str(report),
            ]
        )
        == 1
    )
    assert json.loads(report.read_text()) == {"keep": "original"}
