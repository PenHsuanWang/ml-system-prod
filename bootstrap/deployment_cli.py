"""Release configuration checks and explicit, retained MLflow dependency qualification.

These commands do not approve a model, deploy an endpoint, or qualify Databricks.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from bootstrap.ml_config import MLSettings

REQUIRED_ROLES = frozenset(
    {
        "migrate",
        "management",
        "relay",
        "supervisor",
        "control",
        "gateway",
        "ingestion-worker",
        "operator",
    }
)
INTERNAL_TRACKING = frozenset({"mlflow", "mlflow-db", "minio", "minio-init"})
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
CODE = re.compile(r"[a-f0-9]{40}\Z")
SECRET_ENV = frozenset(
    {
        "ML_DATABASE_URL",
        "ML_RABBITMQ_URL",
        "ML_REDIS_URL",
        "APP_MONGO_URI",
        "APP_JWT_SECRET",
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
        "ML_SERVING_MLFLOW_USERNAME",
        "ML_SERVING_MLFLOW_PASSWORD",
        "ML_SERVING_MLFLOW_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "DATABRICKS_CLIENT_SECRET",
    }
)
ClientFactory = Callable[[str], Any]
ReaderRunner = Callable[[Path, Path, Mapping[str, str], float], None]
READER_FAILURE_CODES = frozenset(
    {
        "INVALID_QUALIFICATION_REFERENCE",
        "QUALIFICATION_ORIGIN_MISMATCH",
        "QUALIFICATION_MODEL_SOURCE_MISMATCH",
        "MLFLOW_ARTIFACT_PROXY_REQUIRED",
        "QUALIFICATION_MODEL_NOT_READY",
        "SERVING_ARTIFACT_DIGEST_MISMATCH",
        "EXPLICIT_WRITE_DENIAL_REQUIRED",
        "SERVING_IDENTITY_CAN_WRITE",
        "SDK_TRACKING_ENDPOINT_MISMATCH",
        "INVALID_OR_FAILED_CHECK",
    }
)


class QualificationError(ValueError):
    """A safe operator-facing code; never includes SDK exception text."""


@contextmanager
def quiet_sdk() -> Iterator[None]:
    """The one-shot CLI reports safe JSON; SDK URLs and exception text stay private."""
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(previous)


def load_object(path: Path, *, limit: int = 2 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise QualificationError("INVALID_INPUT_FILE")
    data = path.read_bytes()
    if len(data) > limit:
        raise QualificationError("INVALID_INPUT_FILE")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise QualificationError("DUPLICATE_INPUT_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (TypeError, ValueError, RecursionError) as error:
        raise QualificationError("INVALID_INPUT_JSON") from error
    if not isinstance(value, dict):
        raise QualificationError("INVALID_INPUT_JSON")
    return value


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        raise QualificationError("REPORT_ALREADY_EXISTS")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def validate_endpoint(uri: str) -> None:
    try:
        value = urlsplit(uri)
        port = value.port
    except ValueError as error:
        raise QualificationError("APPROVED_HTTPS_MLFLOW_ENDPOINT_REQUIRED") from error
    if (
        value.scheme != "https"
        or not value.hostname
        or value.username
        or value.password
        or value.query
        or value.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise QualificationError("APPROVED_HTTPS_MLFLOW_ENDPOINT_REQUIRED")


def environment_values(service: dict[str, Any]) -> dict[str, str]:
    values = service.get("environment", {})
    if not isinstance(values, dict) or any(not isinstance(k, str) for k in values):
        raise QualificationError("INVALID_ROLE_ENVIRONMENT")
    return {key: str(value) for key, value in values.items() if value is not None}


def host_path(value: Any) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise QualificationError("ABSOLUTE_APPROVED_HOST_PATH_REQUIRED")
    return Path(value).resolve()


def preflight(model: dict[str, Any], inspection: dict[str, Any]) -> dict[str, Any]:
    """Compare the captured Compose model and Docker image inspection, without network I/O."""
    services = model.get("services")
    if not isinstance(services, dict) or not REQUIRED_ROLES <= set(services):
        raise QualificationError("REQUIRED_RUNTIME_ROLES_MISSING")
    if INTERNAL_TRACKING & set(services):
        raise QualificationError("INTERNAL_TRACKING_SERVICES_PRESENT")
    if set(services) != REQUIRED_ROLES:
        raise QualificationError("UNAPPROVED_SERVICES_PRESENT")
    image_refs: set[str] = set()
    code_revisions: set[str] = set()
    declared_digests: set[str] = set()
    private_sources: set[str] = set()
    private_files: set[str] = set()
    runtime_roots: set[Path] = set()
    protected_inputs: set[Path] = set()
    runtime_users: set[str] = set()
    for section in ("secrets", "configs"):
        definitions = model.get(section, {})
        if not isinstance(definitions, dict):
            raise QualificationError("INVALID_FILE_MOUNT_DEFINITIONS")
        for definition in definitions.values():
            if isinstance(definition, dict) and "file" in definition:
                protected_inputs.add(host_path(definition["file"]))
    for role in sorted(REQUIRED_ROLES):
        service = services[role]
        if not isinstance(service, dict) or service.get("build"):
            raise QualificationError("PREBUILT_RELEASE_IMAGES_REQUIRED")
        if service.get("entrypoint") != ["python", "-m", "bootstrap.deployment_entrypoint"]:
            raise QualificationError("APPROVED_RUNTIME_ENTRYPOINT_REQUIRED")
        image = service.get("image", "")
        if not isinstance(image, str) or "@" not in image:
            raise QualificationError("PINNED_RELEASE_IMAGE_REQUIRED")
        digest = image.rsplit("@", 1)[1]
        if not DIGEST.fullmatch(digest):
            raise QualificationError("PINNED_RELEASE_IMAGE_REQUIRED")
        environment = environment_values(service)
        if (
            environment.get("ML_MODEL_ENVIRONMENT") != "prod"
            or environment.get("ML_TRACKER_BACKEND") != "mlflow"
            or environment.get("ML_IMAGE_DIGEST") != digest
        ):
            raise QualificationError("INVALID_PRODUCTION_ROLE_CONFIGURATION")
        code = environment.get("ML_CODE_REVISION", "")
        if not CODE.fullmatch(code):
            raise QualificationError("PINNED_CODE_REVISION_REQUIRED")
        app_uri = environment.get("ML_TRACKING_URI", "")
        if environment.get("MLFLOW_TRACKING_URI") != app_uri:
            raise QualificationError("SDK_TRACKING_ENDPOINT_MISMATCH")
        validate_endpoint(app_uri)
        if environment.get("MLFLOW_TRACKING_INSECURE_TLS", "false").lower() not in {
            "false",
            "0",
            "",
        }:
            raise QualificationError("TLS_VERIFICATION_REQUIRED")
        if any(environment.get(key) for key in SECRET_ENV):
            raise QualificationError("FILE_MOUNTED_SECRETS_REQUIRED")
        if not re.fullmatch(r"[0-9]+:[0-9]+", str(service.get("user", ""))):
            raise QualificationError("EXPLICIT_RUNTIME_UID_REQUIRED")
        if str(service["user"]).split(":", 1)[0] == "0":
            raise QualificationError("NONROOT_RUNTIME_REQUIRED")
        runtime_users.add(str(service["user"]))
        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            raise QualificationError("CONTROLLED_SHARED_RUNTIME_MOUNT_REQUIRED")
        role_roots = []
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                raise QualificationError("APPROVED_BIND_MOUNTS_REQUIRED")
            source_path = host_path(volume.get("source"))
            if volume.get("read_only") is True:
                protected_inputs.add(source_path)
            elif volume.get("target") == "/app/var":
                if volume.get("bind", {}).get("create_host_path", False) is not False:
                    raise QualificationError("RUNTIME_HOST_PATH_MUST_ALREADY_EXIST")
                role_roots.append(source_path)
            else:
                raise QualificationError("UNAPPROVED_WRITABLE_MOUNT")
        if len(role_roots) != 1:
            raise QualificationError("CONTROLLED_SHARED_RUNTIME_MOUNT_REQUIRED")
        runtime_roots.update(role_roots)
        try:
            cpus = float(service.get("cpus", 0))
        except (TypeError, ValueError) as error:
            raise QualificationError("APPROVED_ROLE_CPU_LIMIT_REQUIRED") from error
        if not math.isfinite(cpus) or cpus <= 0:
            raise QualificationError("APPROVED_ROLE_CPU_LIMIT_REQUIRED")
        if type(service.get("pids_limit")) is not int or service["pids_limit"] <= 0:
            raise QualificationError("APPROVED_ROLE_PROCESS_LIMIT_REQUIRED")
        dependencies = service.get("depends_on", {})
        if isinstance(dependencies, dict) and INTERNAL_TRACKING & set(dependencies):
            raise QualificationError("INTERNAL_TRACKING_DEPENDENCY_PRESENT")
        memory = service.get("mem_limit")
        if (type(memory) is not int and not (isinstance(memory, str) and memory.isdecimal())) or (
            int(memory) <= 0
        ):
            raise QualificationError("APPROVED_ROLE_MEMORY_LIMIT_REQUIRED")
        if environment.get("ML_RUNTIME_ROLE") != role:
            raise QualificationError("RUNTIME_ROLE_MISMATCH")
        config_file = environment.get("ML_RUNTIME_CONFIG_FILE", "")
        mounted_secrets = service.get("secrets", [])
        matching = (
            [
                secret
                for secret in mounted_secrets
                if isinstance(secret, dict)
                and str(Path("/run/secrets") / str(secret.get("target", ""))) == config_file
                and secret.get("source") in model.get("secrets", {})
            ]
            if isinstance(mounted_secrets, list)
            else []
        )
        if len(matching) != 1:
            raise QualificationError("PRIVATE_RUNTIME_CONFIG_MOUNT_REQUIRED")
        source = str(matching[0]["source"])
        definition = model["secrets"][source]
        private_file = definition.get("file") if isinstance(definition, dict) else None
        if (
            not isinstance(private_file, str)
            or not Path(private_file).is_absolute()
            or ".." in Path(private_file).parts
        ):
            raise QualificationError("PRIVATE_RUNTIME_CONFIG_FILE_REQUIRED")
        normalized_file = str(Path(private_file))
        if source in private_sources or normalized_file in private_files:
            raise QualificationError("ROLE_CONFIGURATIONS_MUST_BE_SEPARATE")
        private_sources.add(source)
        private_files.add(normalized_file)
        ca_file = environment.get("MLFLOW_TRACKING_SERVER_CERT_PATH", "")
        mounted_configs = service.get("configs", [])
        if not isinstance(mounted_configs, list) or not any(
            isinstance(config, dict)
            and config.get("target") == ca_file
            and config.get("source") in model.get("configs", {})
            for config in mounted_configs
        ):
            raise QualificationError("APPROVED_CA_MOUNT_REQUIRED")
        image_refs.add(image)
        code_revisions.add(code)
        declared_digests.add(digest)
    if len(image_refs) != 1 or len(code_revisions) != 1 or len(declared_digests) != 1:
        raise QualificationError("ONE_RELEASE_IMAGE_CONTRACT_REQUIRED")
    if len(runtime_roots) != 1 or len(runtime_users) != 1:
        raise QualificationError("ONE_RUNTIME_STORAGE_OWNER_REQUIRED")
    runtime_root = next(iter(runtime_roots))
    if any(path == runtime_root or runtime_root in path.parents for path in protected_inputs):
        raise QualificationError("PRIVATE_INPUT_HAS_WRITABLE_RUNTIME_ALIAS")
    image_ref = next(iter(image_refs))
    code_revision = next(iter(code_revisions))
    if inspection.get("Os") != "linux" or inspection.get("Architecture") != "amd64":
        raise QualificationError("QUALIFIED_LINUX_AMD64_IMAGE_REQUIRED")
    digests = inspection.get("RepoDigests", [])
    labels = inspection.get("Config", {}).get("Labels", {})
    if not isinstance(digests, list) or image_ref not in digests:
        raise QualificationError("PULLED_IMAGE_DIGEST_MISMATCH")
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != code_revision
    ):
        raise QualificationError("IMAGE_SOURCE_REVISION_MISMATCH")
    return {
        "status": "CONFIGURATION_PREFLIGHT_PASSED",
        "scope": "offline-release-configuration",
        "roles": sorted(REQUIRED_ROLES),
        "image_ref": image_ref,
        "code_revision": code_revision,
        "connectivity_qualified": False,
        "production_accepted": False,
    }


def client_factory(uri: str) -> Any:
    from mlflow import MlflowClient

    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def reader_environment(parent: Mapping[str, str], private_home: Path) -> dict[str, str]:
    """Mirror serving's explicit read identity, without parent/cloud/DB credentials."""
    environment = {
        key: parent[key]
        for key in (
            "PATH",
            "PYTHONPATH",
            "LANG",
            "LD_LIBRARY_PATH",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "MLFLOW_TRACKING_SERVER_CERT_PATH",
        )
        if key in parent
    }
    uri = parent.get("ML_TRACKING_URI", "")
    validate_endpoint(uri)
    if parent.get("MLFLOW_TRACKING_INSECURE_TLS", "false").lower() not in {"false", "0", ""}:
        raise QualificationError("TLS_VERIFICATION_REQUIRED")
    environment.update(
        HOME=str(private_home),
        ML_TRACKING_URI=uri,
        MLFLOW_TRACKING_URI=uri,
        MLFLOW_HTTP_REQUEST_TIMEOUT="5",
        MLFLOW_HTTP_REQUEST_MAX_RETRIES="1",
    )
    scoped = {
        "ML_SERVING_MLFLOW_USERNAME": "MLFLOW_TRACKING_USERNAME",
        "ML_SERVING_MLFLOW_PASSWORD": "MLFLOW_TRACKING_PASSWORD",
        "ML_SERVING_MLFLOW_TOKEN": "MLFLOW_TRACKING_TOKEN",
    }
    for source, target in scoped.items():
        if parent.get(source):
            environment[target] = parent[source]
    basic = all(
        environment.get(key)
        for key in (
            "MLFLOW_TRACKING_USERNAME",
            "MLFLOW_TRACKING_PASSWORD",
        )
    )
    token = bool(environment.get("MLFLOW_TRACKING_TOKEN"))
    partial_basic = bool(environment.get("MLFLOW_TRACKING_USERNAME")) != bool(
        environment.get("MLFLOW_TRACKING_PASSWORD")
    )
    if basic == token or partial_basic:
        raise QualificationError("ONE_SCOPED_SERVING_IDENTITY_REQUIRED")
    return environment


def run_reader(
    request: Path, response: Path, environment: Mapping[str, str], timeout: float
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bootstrap.deployment_cli",
            "_verify-reader",
            "--request",
            str(request),
            "--output",
            str(response),
        ],
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        if response.is_file():
            failure = load_object(response)
            if failure.get("error_code") in READER_FAILURE_CODES:
                raise QualificationError(failure["error_code"])
        raise QualificationError("SERVING_IDENTITY_QUALIFICATION_FAILED")


def require_denied(operation: Callable[[], Any]) -> None:
    try:
        operation()
    except Exception as error:
        if getattr(error, "error_code", None) == "PERMISSION_DENIED":
            return
        raise QualificationError("EXPLICIT_WRITE_DENIAL_REQUIRED") from error
    raise QualificationError("SERVING_IDENTITY_CAN_WRITE")


def verify_reader(
    request: dict[str, Any], factory: ClientFactory = client_factory
) -> dict[str, Any]:
    uri = os.environ.get("ML_TRACKING_URI", "")
    validate_endpoint(uri)
    if os.environ.get("MLFLOW_TRACKING_URI") != uri:
        raise QualificationError("SDK_TRACKING_ENDPOINT_MISMATCH")
    qualification_id = request.get("qualification_id", "")
    if (
        not isinstance(qualification_id, str)
        or not re.fullmatch(r"[a-f0-9]{32}", qualification_id)
        or not str(request.get("registered_model_name", "")).endswith(
            "-qualification-" + qualification_id
        )
    ):
        raise QualificationError("INVALID_QUALIFICATION_REFERENCE")
    client = factory(uri)
    run = client.get_run(request["run_id"])
    version = client.get_model_version(request["registered_model_name"], request["model_version"])
    if (
        run.data.tags.get("qualification_id") != qualification_id
        or version.tags.get("qualification_id") != qualification_id
        or run.data.tags.get("purpose") != "day1-dependency-qualification"
        or version.tags.get("purpose") != "day1-dependency-qualification"
    ):
        raise QualificationError("QUALIFICATION_ORIGIN_MISMATCH")
    if not str(run.info.artifact_uri).startswith("mlflow-artifacts:/"):
        raise QualificationError("MLFLOW_ARTIFACT_PROXY_REQUIRED")
    approved_sources = {
        f"runs:/{request['run_id']}/day1",
        str(run.info.artifact_uri).rstrip("/") + "/day1",
    }
    if str(version.run_id) != request["run_id"] or str(version.source) not in approved_sources:
        raise QualificationError("QUALIFICATION_MODEL_SOURCE_MISMATCH")
    if str(version.status) != "READY":
        raise QualificationError("QUALIFICATION_MODEL_NOT_READY")
    with tempfile.TemporaryDirectory(prefix="day1-reader-") as directory:
        downloaded = Path(
            client.download_artifacts(request["run_id"], "day1/proof.json", directory)
        )
        if hashlib.sha256(downloaded.read_bytes()).hexdigest() != request["artifact_sha256"]:
            raise QualificationError("SERVING_ARTIFACT_DIGEST_MISMATCH")
    require_denied(lambda: client.log_metric(request["run_id"], "unexpected_serving_write", 1.0))
    require_denied(
        lambda: client.set_model_version_tag(
            request["registered_model_name"],
            request["model_version"],
            "unexpected_serving_write",
            "true",
        )
    )
    return {
        "status": "SERVING_READ_IDENTITY_PASSED",
        "artifact_sha256": request["artifact_sha256"],
        "tracking_write_denied": True,
        "registry_write_denied": True,
    }


def qualify_mlflow(
    experiment_name: str,
    registered_model_prefix: str,
    *,
    factory: ClientFactory = client_factory,
    reader: ReaderRunner = run_reader,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    parent = dict(os.environ if environment is None else environment)
    uri = parent.get("ML_TRACKING_URI", "")
    validate_endpoint(uri)
    if parent.get("MLFLOW_TRACKING_URI") != uri:
        raise QualificationError("SDK_TRACKING_ENDPOINT_MISMATCH")
    writer_basic = bool(parent.get("MLFLOW_TRACKING_USERNAME")) and bool(
        parent.get("MLFLOW_TRACKING_PASSWORD")
    )
    writer_token = bool(parent.get("MLFLOW_TRACKING_TOKEN"))
    writer_partial = bool(parent.get("MLFLOW_TRACKING_USERNAME")) != bool(
        parent.get("MLFLOW_TRACKING_PASSWORD")
    )
    if writer_basic == writer_token or writer_partial:
        raise QualificationError("ONE_WRITER_IDENTITY_REQUIRED")
    if (
        not experiment_name
        or len(experiment_name) > 256
        or any(ord(character) < 32 for character in experiment_name)
    ):
        raise QualificationError("APPROVED_QUALIFICATION_EXPERIMENT_REQUIRED")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", registered_model_prefix):
        raise QualificationError("APPROVED_REGISTERED_MODEL_PREFIX_REQUIRED")
    code_revision = parent.get("ML_CODE_REVISION", "")
    image_digest = parent.get("ML_IMAGE_DIGEST", "")
    if not CODE.fullmatch(code_revision) or not DIGEST.fullmatch(image_digest):
        raise QualificationError("PINNED_RELEASE_REQUIRED")
    qualification_id = uuid4().hex
    model_name = registered_model_prefix + "-qualification-" + qualification_id
    report: dict[str, Any] = {
        "scope": "external-mlflow-dependency-only",
        "status": "MLFLOW_NOT_QUALIFIED",
        "qualification_id": qualification_id,
        "production_accepted": False,
        "code_revision": code_revision,
        "image_digest": image_digest,
    }
    stage = "configuration"
    client = None
    run_id = None
    try:
        with tempfile.TemporaryDirectory(prefix="day1-qualification-") as directory:
            root = Path(directory)
            home = root / "reader-home"
            home.mkdir(mode=0o700)
            reader_env = reader_environment(parent, home)
            client = factory(uri)
            stage = "experiment"
            experiment = client.get_experiment_by_name(experiment_name)
            experiment_id = (
                str(experiment.experiment_id)
                if experiment
                else str(client.create_experiment(experiment_name))
            )
            tags = {
                "purpose": "day1-dependency-qualification",
                "qualification_id": qualification_id,
                "code_revision": code_revision,
                "image_digest": image_digest,
            }
            stage = "run"
            run = client.create_run(experiment_id, tags=tags)
            run_id = str(run.info.run_id)
            report["run_id"] = run_id
            if not str(run.info.artifact_uri).startswith("mlflow-artifacts:/"):
                raise QualificationError("MLFLOW_ARTIFACT_PROXY_REQUIRED")
            client.log_metric(run_id, "qualification_probe", 1.0, timestamp=int(time.time() * 1000))
            payload = {
                "purpose": "day1-dependency-qualification",
                "qualification_id": qualification_id,
                "code_revision": code_revision,
                "image_digest": image_digest,
            }
            proof = root / "proof.json"
            proof.write_text(json.dumps(payload, sort_keys=True) + "\n")
            digest = hashlib.sha256(proof.read_bytes()).hexdigest()
            stage = "artifact"
            client.log_artifact(run_id, str(proof), artifact_path="day1")
            downloaded = root / "writer-download"
            downloaded.mkdir()
            actual = Path(client.download_artifacts(run_id, "day1/proof.json", str(downloaded)))
            if hashlib.sha256(actual.read_bytes()).hexdigest() != digest:
                raise QualificationError("WRITER_ARTIFACT_DIGEST_MISMATCH")
            stage = "registry"
            client.create_registered_model(model_name, tags=tags)
            version = client.create_model_version(
                model_name,
                f"runs:/{run_id}/day1",
                run_id,
                tags=tags,
                await_creation_for=30,
            )
            if str(version.status) != "READY":
                raise QualificationError("QUALIFICATION_MODEL_NOT_READY")
            request = {
                "qualification_id": qualification_id,
                "run_id": run_id,
                "registered_model_name": model_name,
                "model_version": str(version.version),
                "artifact_sha256": digest,
            }
            report.update(request)
            request_path, response_path = (
                root / "reader-request.json",
                root / "reader-response.json",
            )
            write_report(request_path, request)
            stage = "serving-reader"
            reader(request_path, response_path, reader_env, 60.0)
            response = load_object(response_path)
            if (
                response.get("status") != "SERVING_READ_IDENTITY_PASSED"
                or response.get("artifact_sha256") != digest
                or response.get("tracking_write_denied") is not True
                or response.get("registry_write_denied") is not True
            ):
                raise QualificationError("SERVING_IDENTITY_QUALIFICATION_FAILED")
            client.set_terminated(run_id, status="FINISHED")
            report.update(
                status="MLFLOW_DEPENDENCY_QUALIFIED", stage="complete", serving_identity=response
            )
    except Exception as error:
        report.update(error_code="MLFLOW_QUALIFICATION_FAILED", stage=stage)
        if isinstance(error, QualificationError) and str(error) in READER_FAILURE_CODES:
            report["serving_error_code"] = str(error)
        if client is not None and run_id is not None:
            try:
                client.set_terminated(run_id, status="FAILED")
            except Exception:
                pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    check = commands.add_parser("preflight", help="Offline Compose/image metadata checks")
    check.add_argument("--compose-config", type=Path, required=True)
    check.add_argument("--image-inspect", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    qualify = commands.add_parser("qualify-mlflow", help="Retained writer/reader MLflow smoke")
    qualify.add_argument("--experiment-name", required=True)
    qualify.add_argument("--registered-model-prefix", required=True)
    qualify.add_argument("--output", type=Path, required=True)
    child = commands.add_parser("_verify-reader", help=argparse.SUPPRESS)
    child.add_argument("--request", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.is_symlink() or args.output.exists():
            raise QualificationError("REPORT_ALREADY_EXISTS")
        with quiet_sdk():
            if args.operation == "preflight":
                model = load_object(args.compose_config)
                # `docker image inspect --format '{{json .}}' IMAGE` emits one object.
                report = preflight(model, load_object(args.image_inspect))
            elif args.operation == "qualify-mlflow":
                MLSettings.from_env()
                report = qualify_mlflow(args.experiment_name, args.registered_model_prefix)
            else:
                report = verify_reader(load_object(args.request))
        write_report(args.output, report)
        print(json.dumps(report, sort_keys=True))
        return (
            0
            if report["status"]
            in {
                "CONFIGURATION_PREFLIGHT_PASSED",
                "MLFLOW_DEPENDENCY_QUALIFIED",
                "SERVING_READ_IDENTITY_PASSED",
            }
            else 1
        )
    except Exception as error:
        # Input/SDK errors may carry URLs, credentials or filesystem paths.
        code = (
            str(error)
            if isinstance(error, QualificationError) and re.fullmatch(r"[A-Z_]{1,80}", str(error))
            else "INVALID_OR_FAILED_CHECK"
        )
        failed = {
            "status": "QUALIFICATION_FAILED",
            "error_code": code,
            "production_accepted": False,
        }
        try:
            write_report(args.output, failed)
        except Exception:
            pass
        print(json.dumps(failed))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
