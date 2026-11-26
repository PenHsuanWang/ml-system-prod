"""Shared Databricks SQL connection, type mapping and child-process utilities."""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Callable, Sequence
from multiprocessing.process import BaseProcess
from typing import Protocol, cast

from src.domain.ingestion.errors import AcquisitionFailed, IngestionError, SourceRejected
from src.domain.ingestion.models import ExtractionLimits
from src.infrastructure.ingestion.adapters.databricks_contract import ConnectionConfig
from src.infrastructure.ingestion.storage.raw_snapshot_writer import relative_file_fd


class SQLCursor(Protocol):
    @property
    def description(self) -> Sequence[Sequence[object]] | None: ...
    def execute(self, operation: str, parameters: dict[str, object]) -> object: ...
    def fetchmany(self, size: int) -> Sequence[Sequence[object]]: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class SQLConnection(Protocol):
    def cursor(self, *, arraysize: int, buffer_size_bytes: int, row_limit: int) -> SQLCursor: ...
    def close(self) -> None: ...


ConnectFactory = Callable[[ConnectionConfig, ExtractionLimits], SQLConnection]


def sql_kind(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.upper()
    kinds = {
        "BOOLEAN": "boolean",
        "TINYINT": "integer",
        "SMALLINT": "integer",
        "INT": "integer",
        "INTEGER": "integer",
        "BIGINT": "integer",
        "FLOAT": "float",
        "DOUBLE": "float",
        "REAL": "float",
        "STRING": "string",
        "DATE": "date",
        "TIMESTAMP": "timestamp",
        "TIMESTAMP_NTZ": "timestamp",
        "TIMESTAMP_LTZ": "timestamp",
        "BINARY": "binary",
    }
    if name in kinds:
        return kinds[name]
    if re.fullmatch(r"DECIMAL(?:\((?:[1-9]|[12][0-9]|3[0-8]),(?:[0-9]|[12][0-9]|3[0-8])\))?", name):
        return "decimal"
    if re.fullmatch(r"(?:CHAR|VARCHAR)\([1-9][0-9]{0,6}\)", name):
        return "string"
    return None


def read_secret(config: ConnectionConfig) -> str:
    path = config.client_secret_file
    if path is None:
        raise SourceRejected("OAuth credential file is unavailable")
    try:
        with relative_file_fd(path.parent, path.name) as descriptor:
            info = os.fstat(descriptor)
            if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO) or not 0 < info.st_size <= 4096:
                raise SourceRejected("OAuth credential file must be private and bounded")
            value = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            if (
                len(value) != info.st_size
                or len(value) > 4096
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise SourceRejected("OAuth credential file changed while being read")
        secret = value.decode("utf-8").rstrip("\r\n")
        if not secret or any(ord(character) < 33 or ord(character) > 126 for character in secret):
            raise SourceRejected("OAuth credential file contains an invalid secret")
        return secret
    except (OSError, UnicodeError) as error:
        raise SourceRejected("OAuth credential file is unavailable") from error


def connect_authenticated(
    config: ConnectionConfig,
    limits: ExtractionLimits,
    *,
    connect: Callable[..., object],
    config_factory: Callable[..., object],
    oauth_provider: Callable[[object], object],
) -> SQLConnection:
    if config.access_token_env is not None:
        token = os.environ.get(config.access_token_env, "")
        if not 1 <= len(token) <= 4096 or any(
            ord(character) < 33 or ord(character) > 126 for character in token
        ):
            raise SourceRejected("Databricks access token is unavailable or invalid")
        return _connect_sql(config, limits, connect=connect, access_token=token)
    secret = read_secret(config)
    oauth_config = config_factory(
        host="https://" + config.server_hostname,
        client_id=config.client_id,
        client_secret=secret,
        auth_type="oauth-m2m",
    )

    def credentials_provider() -> object:
        return oauth_provider(oauth_config)

    return _connect_sql(config, limits, connect=connect, credentials_provider=credentials_provider)


def _connect_sql(
    config: ConnectionConfig,
    limits: ExtractionLimits,
    *,
    connect: Callable[..., object],
    **authentication: object,
) -> SQLConnection:
    return cast(
        SQLConnection,
        connect(
            server_hostname=config.server_hostname,
            http_path=config.http_path,
            **authentication,
            use_inline_params=False,
            use_cloud_fetch=False,
            enable_telemetry=False,
            force_enable_telemetry=False,
            _socket_timeout=min(10.0, limits.timeout_seconds),
            _retry_stop_after_attempts_count=1,
            session_configuration={
                "TIMEZONE": "UTC",
                "STATEMENT_TIMEOUT": str(max(1, math.ceil(limits.timeout_seconds))),
            },
        ),
    )


def connect_sql(config: ConnectionConfig, limits: ExtractionLimits) -> SQLConnection:
    # Deliberately lazy: Management startup and File-only workers need no SDK.
    from databricks import sql
    from databricks.sdk.core import Config, oauth_service_principal

    return connect_authenticated(
        config,
        limits,
        connect=sql.connect,
        config_factory=Config,
        oauth_provider=lambda config: oauth_service_principal(cast(Config, config)),
    )


def error_kind(error: Exception) -> bytes:
    """Only safe categories cross the process boundary, never exception details."""
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return b"RUNTIME"
    if isinstance(error, SourceRejected):
        return b"SOURCE"
    if isinstance(error, IngestionError):
        return b"DATA"
    status = getattr(error, "status_code", None)
    sqlstate = getattr(error, "sqlstate", None) or getattr(error, "sql_state", None)
    name = type(error).__name__
    if (
        status in (401, 403)
        or sqlstate in ("28000", "42501")
        or name
        in {
            "PermissionDenied",
            "Unauthenticated",
            "Unauthorized",
        }
    ):
        return b"AUTH"
    if name in {"ProgrammingError", "NotSupportedError", "ServerOperationError"}:
        return b"SOURCE"
    if isinstance(error, TimeoutError):
        return b"TIMEOUT"
    return b"TRANSIENT"


def stop_process(process: BaseProcess) -> None:
    process.join(timeout=0.1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    if process.is_alive():
        raise AcquisitionFailed("Databricks SQL runtime did not terminate")
    process.close()
