"""Load one private role configuration before starting a pinned production runtime."""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

ROLES = {
    "migrate": "management",
    "management": "management",
    "relay": "relay",
    "supervisor": "supervisor",
    "control": "control",
    "gateway": "gateway",
    "ingestion-worker": "management",
    "operator": "management",
}
CONNECTION_KEYS = frozenset(
    {
        "ML_DATABASE_URL",
        "ML_RABBITMQ_URL",
        "ML_REDIS_URL",
        "APP_MONGO_URI",
        "APP_JWT_SECRET",
    }
)
WRITER_KEYS = frozenset(
    {
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
    }
)
READER_KEYS = frozenset(
    {
        "ML_SERVING_MLFLOW_USERNAME",
        "ML_SERVING_MLFLOW_PASSWORD",
        "ML_SERVING_MLFLOW_TOKEN",
    }
)
PRIVATE_KEYS = CONNECTION_KEYS | WRITER_KEYS | READER_KEYS
REQUIRED = {
    "migrate": {"ML_DATABASE_URL"},
    "management": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
    "relay": {"ML_DATABASE_URL", "ML_RABBITMQ_URL"},
    "supervisor": {"ML_DATABASE_URL", "ML_RABBITMQ_URL", "ML_REDIS_URL"},
    "control": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
    "gateway": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
    "ingestion-worker": {"ML_DATABASE_URL"},
    "operator": {"ML_DATABASE_URL", "APP_MONGO_URI", "APP_JWT_SECRET"},
}


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate role configuration key")
        value[key] = item
    return value


def _read_configuration(path: Path) -> dict[str, str]:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Role configuration must be an absolute regular file")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Role configuration links are forbidden")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_uid != os.geteuid()
            or info.st_size > 65536
        ):
            raise ValueError("Role configuration ownership, mode or size is invalid")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Role configuration exceeds its limit")
    value = json.loads(data, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or not value:
        raise ValueError("Expected a nonempty role configuration object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (
            key not in PRIVATE_KEYS
            or not isinstance(item, str)
            or not item
            or len(item.encode()) > 16384
            or any(c in item for c in "\x00\r\n")
        ):
            raise ValueError("Invalid private role configuration field")
        result[key] = item
    return result


def _credential_identity(value: Mapping[str, str], prefix: str) -> tuple[str, ...]:
    username, password, token = (
        value.get(prefix + suffix) for suffix in ("USERNAME", "PASSWORD", "TOKEN")
    )
    if token and not username and not password:
        return ("token", token)
    if username and password and not token:
        return ("basic", username)
    raise ValueError("Exactly one complete credential mechanism is required")


def load_runtime_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved configuration without printing secrets or mutating the caller."""
    environment = dict(environ)
    role = environment.get("ML_RUNTIME_ROLE", "")
    if role not in ROLES:
        raise ValueError("Unknown production runtime role")
    if any(environment.get(key) for key in PRIVATE_KEYS):
        raise ValueError("Private values must come from the role configuration file")
    private = _read_configuration(Path(environment.get("ML_RUNTIME_CONFIG_FILE", "")))
    allowed = set(REQUIRED[role])
    writer = role in {"supervisor", "control", "operator"}
    reader = role in {"control", "operator"}
    if writer:
        allowed.update(WRITER_KEYS)
    if reader:
        allowed.update(READER_KEYS)
    if not set(private) <= allowed or not REQUIRED[role] <= set(private):
        raise ValueError("Role configuration has missing or overprivileged fields")
    if writer:
        writer_identity = _credential_identity(private, "MLFLOW_TRACKING_")
        if reader and writer_identity == _credential_identity(private, "ML_SERVING_MLFLOW_"):
            raise ValueError("Writer and serving identities must be distinct")
    for key, schemes in (
        ("ML_DATABASE_URL", {"postgresql", "postgresql+psycopg"}),
        ("ML_RABBITMQ_URL", {"amqp", "amqps"}),
        ("ML_REDIS_URL", {"redis", "rediss"}),
        ("APP_MONGO_URI", {"mongodb", "mongodb+srv"}),
    ):
        if key in private:
            uri = urlsplit(private[key])
            if uri.scheme not in schemes or not uri.hostname:
                raise ValueError("Invalid connection configuration")
    if "APP_JWT_SECRET" in private and len(private["APP_JWT_SECRET"].encode()) < 32:
        raise ValueError("JWT signing material is too short")
    if (
        environment.get("ML_MODEL_ENVIRONMENT") != "prod"
        or environment.get("ML_TRACKER_BACKEND") != "mlflow"
        or re.fullmatch(r"sha256:[a-f0-9]{64}", environment.get("ML_IMAGE_DIGEST", "")) is None
        or re.fullmatch(r"[a-f0-9]{40}", environment.get("ML_CODE_REVISION", "")) is None
    ):
        raise ValueError("Production requires a pinned MLflow release")
    endpoint = environment.get("ML_TRACKING_URI", "")
    uri = urlsplit(endpoint)
    if (
        uri.scheme != "https"
        or not uri.hostname
        or uri.username
        or uri.password
        or uri.query
        or uri.fragment
        or len(endpoint) > 4096
        or environment.get("MLFLOW_TRACKING_URI") != endpoint
        or environment.get("MLFLOW_TRACKING_INSECURE_TLS", "").lower() != "false"
    ):
        raise ValueError("Production MLflow requires matching verified HTTPS endpoints")
    certificate = Path(environment.get("MLFLOW_TRACKING_SERVER_CERT_PATH", ""))
    if not certificate.is_absolute() or not certificate.is_file() or certificate.is_symlink():
        raise ValueError("Approved MLflow CA bundle is required")
    ssl.create_default_context(cafile=str(certificate))
    environment.update(private)
    environment["ML_DEPLOYMENT_ROLE"] = ROLES[role]
    return environment


def configure_runtime_home(environment: dict[str, str]) -> None:
    root = Path(environment.get("ML_EXECUTION_ROOT", ""))
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Production execution root must be absolute")
    home = root / "runtime-homes" / environment["ML_RUNTIME_ROLE"]
    if any(path.is_symlink() for path in (home, *home.parents)):
        raise ValueError("Runtime home links are forbidden")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if home.stat().st_uid != os.geteuid():
        raise ValueError("Runtime home has a different owner")
    home.chmod(0o700)
    environment["HOME"] = str(home)


def main() -> int:
    try:
        if not sys.argv[1:]:
            raise ValueError("A runtime command is required")
        environment = load_runtime_environment(os.environ)
        configure_runtime_home(environment)
        os.execvpe(sys.argv[1], sys.argv[1:], environment)
    except Exception:
        print('{"error":"DEPLOYMENT_CONFIGURATION_REJECTED"}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
