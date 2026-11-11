"""Official SQL Connector isolated behind bounded byte streaming and explicit authentication.

SDK imports and secret material stay inside a dedicated spawned child. The
parent exchanges bounded bytes, applies cooperative cancellation/lease checks,
and physically reaps a blocked connector at its extraction deadline.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import os
import re
import stat
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Protocol, cast

from src.domain.ingestion.errors import (
    AcquisitionDeadline,
    AcquisitionFailed,
    IngestionError,
    SourceRejected,
)
from src.domain.ingestion.models import ExtractionLimits
from src.infrastructure.ingestion.adapters.databricks_contract import (
    ConnectionConfig,
    Template,
    build_query,
    encode_row,
)
from src.infrastructure.ingestion.storage.raw_snapshot_writer import CheckControl, relative_file_fd


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
RowProducer = Callable[[ConnectionConfig, Template, ExtractionLimits], Iterable[bytes]]


def _sql_kind(value: object) -> str | None:
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


def _read_secret(config: ConnectionConfig) -> str:
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


def _connect_authenticated(
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
    secret = _read_secret(config)
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


def _official_connect(config: ConnectionConfig, limits: ExtractionLimits) -> SQLConnection:
    # Deliberately lazy: Management startup and File-only workers need no SDK.
    from databricks import sql
    from databricks.sdk.core import Config, oauth_service_principal

    return _connect_authenticated(
        config,
        limits,
        connect=sql.connect,
        config_factory=Config,
        oauth_provider=lambda config: oauth_service_principal(cast(Config, config)),
    )


def _sdk_stream(
    config: ConnectionConfig,
    template: Template,
    limits: ExtractionLimits,
    *,
    connect: ConnectFactory | None = None,
) -> Iterator[bytes]:
    """Use bounded DB-API calls, with no SDK value escaping the codec boundary."""
    sql, parameters = build_query(template, limits.max_records)
    connection = (connect or _official_connect)(config, limits)
    cursor = None
    complete = False
    count, size = 0, 0
    try:
        cursor = connection.cursor(
            arraysize=min(128, limits.max_records + 1),
            buffer_size_bytes=min(4 * 1024 * 1024, limits.max_bytes),
            row_limit=limits.max_records + 1,
        )
        cursor.execute(sql, parameters=parameters)
        description = cursor.description
        if (
            description is None
            or any(isinstance(column, (str, bytes)) or len(column) < 2 for column in description)
            or tuple(column[0] for column in description)
            != tuple(column.name for column in template.projection)
            or tuple(_sql_kind(column[1]) for column in description)
            != tuple(column.type for column in template.projection)
        ):
            raise SourceRejected("Databricks result columns do not match the approved schema")
        while True:
            rows = cursor.fetchmany(min(128, limits.max_records - count + 1))
            if not rows:
                complete = True
                return
            for row in rows:
                count += 1
                if count > limits.max_records:
                    raise IngestionError("Databricks extraction exceeds its approved record limit")
                value = encode_row(template, row, limits.max_line_bytes)
                size += len(value)
                if size > limits.max_bytes:
                    raise IngestionError("Databricks extraction exceeds its approved byte limit")
                yield value
    finally:
        if cursor is not None:
            if not complete:
                with suppress(Exception):
                    cursor.cancel()
            with suppress(Exception):
                cursor.close()
        with suppress(Exception):
            connection.close()


def _error_kind(error: Exception) -> bytes:
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


def _child_entry(
    sender: Connection,
    config: ConnectionConfig,
    template: Template,
    limits: ExtractionLimits,
    producer: RowProducer,
) -> None:
    # Suppress Python and native-library diagnostics before loading the SDK.
    logging.disable(logging.CRITICAL)
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
        os.dup2(null, 2)
    finally:
        if null not in (1, 2):
            os.close(null)
    try:
        for value in producer(config, template, limits):
            if not isinstance(value, bytes) or not 0 < len(value) <= limits.max_line_bytes:
                raise IngestionError("Databricks producer returned an invalid record")
            sender.send_bytes(b"R" + value)
        sender.send_bytes(b"D")
    except Exception as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            sender.send_bytes(b"E" + _error_kind(error))
    finally:
        sender.close()


def _stop_process(process: BaseProcess) -> None:
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


def _raise_remote_error(kind: bytes) -> None:
    if kind in {b"SOURCE", b"AUTH", b"RUNTIME"}:
        raise SourceRejected("Approved Databricks source or SQL runtime is unavailable")
    if kind == b"DATA":
        raise IngestionError("Databricks data violates the approved extraction contract")
    if kind == b"TIMEOUT":
        raise AcquisitionDeadline("Databricks acquisition deadline exceeded")
    raise AcquisitionFailed("Databricks SQL acquisition failed")


class DatabricksSQLClient:
    """One bounded spawned SQL process per extraction, with byte-only backpressure."""

    def __init__(self, *, producer: RowProducer | None = None) -> None:
        # Explicit test-side injection; this is never selected through Source JSON.
        self._producer = producer or _sdk_stream

    def stream(
        self,
        connection: ConnectionConfig,
        template: Template,
        limits: ExtractionLimits,
        control: CheckControl,
    ) -> Iterator[bytes]:
        control.check()
        deadline = time.monotonic() + limits.timeout_seconds
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_child_entry,
            args=(sender, connection, template, limits, self._producer),
            name="databricks-sql-extract",
            daemon=True,
        )
        started = False
        stopped = Event()
        messages: Queue[bytes | None] = Queue(maxsize=1)
        reader: Thread | None = None

        def receive() -> None:
            while not stopped.is_set():
                try:
                    message = receiver.recv_bytes(limits.max_line_bytes + 32)
                except (EOFError, OSError):
                    message = None
                while not stopped.is_set():
                    try:
                        messages.put(message, timeout=0.05)
                        break
                    except Full:
                        continue
                if message is None or message == b"D" or message.startswith(b"E"):
                    return

        count, size = 0, 0
        try:
            process.start()
            started = True
            sender.close()
            reader = Thread(target=receive, name="databricks-result-reader", daemon=True)
            reader.start()
            while True:
                control.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcquisitionDeadline("Databricks acquisition deadline exceeded")
                try:
                    message = messages.get(timeout=min(0.05, remaining))
                except Empty:
                    continue
                if message is None:
                    raise AcquisitionFailed("Databricks SQL runtime returned no valid result")
                if message == b"D":
                    control.check()
                    return
                if message.startswith(b"E"):
                    _raise_remote_error(message[1:])
                if not message.startswith(b"R") or len(message) < 2:
                    raise AcquisitionFailed("Databricks SQL runtime returned an invalid message")
                value = message[1:]
                count += 1
                size += len(value)
                if (
                    len(value) > limits.max_line_bytes
                    or count > limits.max_records
                    or size > limits.max_bytes
                ):
                    raise IngestionError("Databricks extraction exceeds its approved limits")
                yield value
        except OSError:
            raise AcquisitionFailed("Databricks SQL runtime is unavailable") from None
        finally:
            stopped.set()
            sender.close()
            if started:
                _stop_process(process)
            else:
                process.close()
            receiver.close()
            if reader is not None:
                reader.join(timeout=1)
                if reader.is_alive():
                    raise AcquisitionFailed("Databricks result reader did not terminate")
