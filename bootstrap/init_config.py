"""Offline setup contracts and renderers for the existing Compose profiles."""

from __future__ import annotations

import json
import os
import re
import secrets
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bootstrap.deployment_entrypoint import (
    PRIVATE_KEYS,
    READER_KEYS,
    REQUIRED,
    WRITER_KEYS,
    _credential_identity,
)
from bootstrap.init_files import (
    PrivateFiles,
    SetupError,
    absolute_path,
    compose_env_key,
    env_values,
    merge_env,
    read_regular,
)
from bootstrap.ml_config import MLSettings
from src.infrastructure.ingestion.adapters.databricks_contract import (
    parse_connections,
    parse_templates,
)

ROLES = tuple(role for role in REQUIRED if role != "serving")
LOCAL_PASSWORDS = (
    "ML_DB_PASSWORD",
    "MLFLOW_DB_PASSWORD",
    "ML_MQ_PASSWORD",
    "ML_MONGO_PASSWORD",
    "ML_S3_PASSWORD",
)
OAUTH_CONTAINER_PATH = "/run/secrets/databricks_client_secret"
DATABRICKS_ENV_KEYS = frozenset(
    {
        "ML_DATABRICKS_CONNECTIONS_CONFIG",
        "ML_DATABRICKS_SOURCES_CONFIG",
        "ML_DATABRICKS_POLICY_CONFIG",
        "ML_DATABRICKS_POLICY_FILE",
        "ML_DATABRICKS_CLIENT_SECRET_FILE",
        "DATABRICKS_TOKEN",
    }
)
INGESTION_ENV_KEYS = DATABRICKS_ENV_KEYS | {"ML_INGESTION_INPUT_DIR"}


def single_line(value: str) -> str:
    if not value or len(value.encode()) > 16384 or any(c in value for c in "\x00\r\n"):
        raise SetupError("A nonempty single-line value is required.")
    return value


def connection_uri(value: str, schemes: set[str]) -> str:
    single_line(value)
    if any(c.isspace() for c in value):
        raise SetupError("Connection URIs cannot contain unescaped whitespace.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in schemes
            or not parsed.hostname
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
    except ValueError:
        raise SetupError("Connection scheme, hostname or port is invalid.") from None
    return value


def https_uri(value: str) -> str:
    connection_uri(value, {"https"})
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SetupError("HTTPS endpoints cannot contain credentials, query or fragment.")
    return value


def credential(value: str) -> str:
    if not 1 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise SetupError("Use 1..4096 visible ASCII characters for this credential.")
    return value


def jwt_secret(value: str) -> str:
    single_line(value)
    if len(value.encode()) < 32:
        raise SetupError("JWT signing material must contain at least 32 bytes.")
    return value


def load_object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SetupError("Duplicate JSON keys are not allowed.")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise SetupError("Expected a bounded JSON object.") from None


def json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


def example_environment(project: Path) -> dict[str, str]:
    values = env_values(read_regular(project / ".env.ml.production.example").decode())
    # Invalid release/path placeholders are never usable defaults.
    return {
        k: v for k, v in values.items() if "REPLACE" not in v and not v.startswith("/absolute/")
    }


def validate_production_env(keys: Iterable[str]) -> None:
    if any(compose_env_key(key) in PRIVATE_KEYS for key in keys):
        raise SetupError(
            "Production credentials belong only in private role JSON. Remove the private "
            "keys from .env.ml.production before setup."
        )


@dataclass(repr=False)
class SetupSpec:
    project: Path
    private: Path
    profile: str
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    connections: dict[str, Any] | None = field(default=None, repr=False)
    templates: dict[str, Any] = field(default_factory=dict, repr=False)
    source_policy: dict[str, Any] = field(
        default_factory=lambda: {"contract": "databricks-access-v1", "grants": []}, repr=False
    )
    oauth_secret: str | None = field(default=None, repr=False)
    private_values: dict[str, str] = field(default_factory=dict, repr=False)
    existing_roles: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    changed_private: set[str] = field(default_factory=set, repr=False)


@dataclass(repr=False)
class SetupPlan:
    outputs: dict[Path, bytes] = field(repr=False)
    directories: tuple[Path, ...]
    compose_files: tuple[str, ...]
    env_files: tuple[str, ...]
    pending_sources: bool


def role_configurations(spec: SetupSpec) -> dict[str, dict[str, str]]:
    result = {}
    for role in ROLES:
        keys = set(REQUIRED[role])
        if role in {"supervisor", "control", "operator"}:
            keys.update(WRITER_KEYS)
        if role in {"control", "operator"}:
            keys.update(READER_KEYS)
        previous = spec.existing_roles.get(role, {})
        if set(previous) - keys:
            raise SetupError("Existing role file contains unsupported or overprivileged fields.")
        configuration = previous.copy()
        for group in (WRITER_KEYS, READER_KEYS):
            allowed = group & keys
            if allowed and (spec.changed_private & group or not configuration.keys() & group):
                configuration = {k: v for k, v in configuration.items() if k not in group}
                configuration.update({k: v for k, v in spec.private_values.items() if k in allowed})
        for key, value in spec.private_values.items():
            if key in REQUIRED[role] and (key not in configuration or key in spec.changed_private):
                configuration[key] = value
        if not REQUIRED[role] <= set(configuration) or not set(configuration) <= keys:
            raise SetupError("Missing or unsupported role configuration fields.")
        if keys & WRITER_KEYS:
            writer = _credential_identity(configuration, "MLFLOW_TRACKING_")
            if keys & READER_KEYS and writer == _credential_identity(
                configuration, "ML_SERVING_MLFLOW_"
            ):
                raise SetupError("MLflow writer and reader identities must be distinct.")
        for key, value in configuration.items():
            single_line(value)
            schemes = {
                "ML_DATABASE_URL": {"postgresql+psycopg"},
                "ML_RABBITMQ_URL": {"amqp", "amqps"},
                "ML_REDIS_URL": {"redis", "rediss"},
                "APP_MONGO_URI": {"mongodb", "mongodb+srv"},
            }
            if key in schemes:
                connection_uri(value, schemes[key])
            if key == "APP_JWT_SECRET":
                jwt_secret(value)
        if len(json_bytes(configuration)) > 65536:
            raise SetupError("Private role configuration exceeds 64 KiB.")
        result[role] = configuration
    return result


def _validate_external(spec: SetupSpec) -> None:
    values = spec.environment
    if (
        os.geteuid() == 0
        or values["ML_UID"] != str(os.geteuid())
        or values["ML_GID"] != str(os.getegid())
    ):
        raise SetupError(
            "Generate files as the non-root runtime UID/GID; setup does not sudo/chown."
        )
    release = values["ML_RELEASE_IMAGE"]
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", release):
        raise SetupError("A pinned repository@sha256 release is required.")
    if values["ML_IMAGE_DIGEST"] != release.split("@", 1)[1]:
        raise SetupError("Release image and manifest digest must match.")
    if not re.fullmatch(r"[a-f0-9]{40}", values["ML_CODE_REVISION"]):
        raise SetupError("A complete approved 40-hex source revision is required.")
    https_uri(values["ML_EXTERNAL_MLFLOW_URI"])
    ca = absolute_path(values["ML_MLFLOW_CA_FILE"])
    read_regular(ca)
    ssl.create_default_context(cafile=str(ca))
    for key in ("ML_MANAGEMENT_PORT", "ML_PREDICTION_PORT"):
        if not values[key].isdigit() or not 1 <= int(values[key]) <= 65535:
            raise SetupError("Local ports must be integers in 1..65535.")
    if values["ML_MANAGEMENT_PORT"] == values["ML_PREDICTION_PORT"]:
        raise SetupError("Management and prediction ports must differ.")
    for key, value in values.items():
        if key.endswith("_CPUS"):
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or float(value) <= 0:
                raise SetupError("CPU limits must be positive finite numbers.")
        if key.endswith("_MEMORY") and not re.fullmatch(
            r"[1-9][0-9]*(?:[bkmg]|[KMGT]i?B?)?", value
        ):
            raise SetupError("Memory limits must be positive Compose size values.")
    mapping = {
        "ML_TRAINING_TIMEOUT_SECONDS": "training_timeout",
        "ML_QUEUE_TIMEOUT_SECONDS": "queue_timeout",
        "ML_CANCEL_GRACE_SECONDS": "cancel_grace",
        "ML_HEARTBEAT_SECONDS": "heartbeat_seconds",
        "ML_STALE_AFTER_SECONDS": "stale_after",
        "ML_IDEMPOTENCY_RETENTION_SECONDS": "retention_seconds",
        "ML_MAX_PENDING_JOBS_PER_TENANT": "max_pending",
        "ML_ACK_TIMEOUT_MS": "ack_timeout_ms",
    }
    MLSettings(
        database_url=spec.private_values["ML_DATABASE_URL"],
        environment="prod",
        tracker_backend="mlflow",
        code_revision=values["ML_CODE_REVISION"],
        image_digest=values["ML_IMAGE_DIGEST"],
        **{attribute: int(values[key]) for key, attribute in mapping.items()},
    ).validate()
    if any(
        int(values[k]) <= 0
        for k in ("ML_RUNTIME_PIDS_LIMIT", "ML_API_CONCURRENCY", "ML_GATEWAY_CONCURRENCY")
    ):
        raise SetupError("PID and concurrency limits must be positive integers.")
    timeout = float(values["ML_PREDICTION_TIMEOUT_SECONDS"])
    if not 0 < timeout < float("inf"):
        raise SetupError("Prediction timeout must be positive and finite.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", values["APP_MONGO_DATABASE"]):
        raise SetupError("Invalid Identity database name.")
    if values.get("ML_EBM_QUALITY_POLICY_JSON"):
        from src.plugins.evaluation.ebm_v1 import EBMQualityPolicy

        policy = EBMQualityPolicy.from_dict(
            load_object(values["ML_EBM_QUALITY_POLICY_JSON"].encode())
        )
        if policy.policy_version == "ebm-unqualified-v1":
            raise SetupError("Reserved EBM quality policy version.")


def build_plan(spec: SetupSpec, files: PrivateFiles) -> SetupPlan:
    if spec.profile not in {"compose", "external"}:
        raise SetupError("Unsupported setup profile.")
    runtime = absolute_path(spec.environment.get("ML_RUNTIME_ROOT", spec.project / "var"))
    if spec.private.is_relative_to(runtime) or runtime.is_relative_to(spec.private):
        raise SetupError("Private configuration and runtime storage must be separate trees.")
    outputs: dict[Path, bytes] = {}
    directories = [runtime]
    compose = (
        ["compose.ml.yaml"] if spec.profile == "compose" else ["compose.ml.external-mlflow.yaml"]
    )
    env_paths = [".env.ml"] if spec.profile == "compose" else [".env.ml.production"]
    environment = dict(spec.environment)
    if (
        spec.profile == "compose"
        and spec.connections is None
        and "ML_INGESTION_INPUT_DIR" in environment
    ):
        input_path = absolute_path(environment["ML_INGESTION_INPUT_DIR"])
        if input_path.is_relative_to(runtime):
            raise SetupError("Approved ingestion inputs must be outside shared runtime storage.")
        directories.append(input_path)
        compose.append("compose.ml.ingestion.yaml")
        env_paths.append(".env.ml.ingestion")
    if spec.connections is not None:
        connections = parse_connections(spec.connections)
        templates = parse_templates(spec.templates)
        if any(t.connection_ref not in connections for t in templates.values()):
            raise SetupError("A source template references an unknown connection.")
        for connection in connections.values():
            if connection.access_token_env not in {None, "DATABRICKS_TOKEN"} or (
                connection.client_secret_file is not None
                and str(connection.client_secret_file) != OAUTH_CONTAINER_PATH
            ):
                raise SetupError(
                    "Connections must use the credential references mounted by this profile."
                )
        paths = {
            "ML_DATABRICKS_CONNECTIONS_CONFIG": spec.private / "databricks/connections.json",
            "ML_DATABRICKS_SOURCES_CONFIG": spec.private / "databricks/sources.json",
            "ML_DATABRICKS_POLICY_CONFIG": spec.private / "databricks/access-policy.json",
        }
        outputs[paths["ML_DATABRICKS_CONNECTIONS_CONFIG"]] = json_bytes(spec.connections)
        outputs[paths["ML_DATABRICKS_SOURCES_CONFIG"]] = json_bytes(spec.templates)
        outputs[paths["ML_DATABRICKS_POLICY_CONFIG"]] = json_bytes(spec.source_policy)
        environment["ML_DATABRICKS_POLICY_FILE"] = "/app/config/databricks-policy.json"
        environment.update({k: str(v) for k, v in paths.items()})
        if any(c.access_token_env for c in connections.values()):
            credential(environment["DATABRICKS_TOKEN"])
            if spec.profile == "external":
                environment["ML_DATABRICKS_CLIENT_SECRET_FILE"] = "/dev/null"
            else:
                environment.pop("ML_DATABRICKS_CLIENT_SECRET_FILE", None)
                compose.extend(["compose.ml.ingestion.yaml", "compose.ml.databricks-token.yaml"])
        else:
            credential(spec.oauth_secret or "")
            secret_path = spec.private / "databricks/oauth-secret"
            outputs[secret_path] = (spec.oauth_secret or "").encode()
            environment["ML_DATABRICKS_CLIENT_SECRET_FILE"] = str(secret_path)
            environment.pop("DATABRICKS_TOKEN", None)
            if spec.profile == "compose":
                compose.extend(["compose.ml.ingestion.yaml", "compose.ml.databricks.yaml"])
        if spec.profile == "compose":
            input_path = absolute_path(environment["ML_INGESTION_INPUT_DIR"])
            if input_path.is_relative_to(runtime):
                raise SetupError(
                    "Approved ingestion inputs must be outside shared runtime storage."
                )
            directories.append(input_path)
            env_paths.append(".env.ml.ingestion")
    if spec.profile == "external":
        if not spec.connections:
            raise SetupError(
                "The existing external-service profile requires Databricks configuration."
            )
        _validate_external(spec)
        definitions = absolute_path(environment["ML_OPERATOR_DEFINITIONS_DIR"])
        if definitions.is_relative_to(runtime):
            raise SetupError("Operator definitions must be outside shared runtime storage.")
        directories.append(definitions)
        for role, configuration in role_configurations(spec).items():
            outputs[spec.private / "roles" / (role + ".json")] = json_bytes(configuration)
        environment["ML_DEPLOYMENT_CONFIG_DIR"] = str(spec.private / "roles")
    if spec.profile == "compose":
        local = {
            k: environment[k] for k in (*LOCAL_PASSWORDS, "APP_JWT_SECRET", "ML_UID", "ML_GID")
        }
        if "OTEL_EXPORTER_OTLP_ENDPOINT" in environment:
            local["OTEL_EXPORTER_OTLP_ENDPOINT"] = environment["OTEL_EXPORTER_OTLP_ENDPOINT"]
        values = {".env.ml": local}
        if "ML_INGESTION_INPUT_DIR" in environment:
            values[".env.ml.ingestion"] = {
                k: v for k, v in environment.items() if k in INGESTION_ENV_KEYS
            }
    else:
        values = {".env.ml.production": environment}
    managed = {
        ".env.ml.ingestion": INGESTION_ENV_KEYS,
        ".env.ml.production": DATABRICKS_ENV_KEYS,
    }
    for name, updates in values.items():
        path = spec.project / name
        original = files.read(path)
        obsolete = managed.get(name, frozenset()).difference(updates)
        merged = merge_env(original.decode() if original else "", updates, remove=obsolete)
        if name == ".env.ml.production":
            validate_production_env(env_values(merged))
        outputs[path] = merged.encode()
    for path in outputs:
        files.read(path)
    return SetupPlan(
        outputs,
        tuple(directories),
        tuple(compose),
        tuple(env_paths),
        spec.connections is not None and not spec.templates,
    )


def local_defaults(existing: dict[str, str]) -> dict[str, str]:
    result = {k: existing.get(k) or secrets.token_hex(24) for k in LOCAL_PASSWORDS}
    # These interpolate into URIs as well as server passwords in the original Compose.
    if any(not re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", value) for value in result.values()):
        raise SetupError(
            "Local Compose passwords must be URL-safe; do not rewrite existing credentials."
        )
    result.update(
        APP_JWT_SECRET=jwt_secret(existing.get("APP_JWT_SECRET") or secrets.token_hex(32)),
        ML_UID=str(os.geteuid()),
        ML_GID=str(os.getegid()),
    )
    if os.geteuid() == 0:
        raise SetupError("Run setup as a non-root runtime user.")
    return result
