"""Official SQL Connector isolated behind bounded byte streaming and explicit authentication.

SDK imports and secret material stay inside a dedicated spawned child. The
parent exchanges bounded bytes, applies cooperative cancellation/lease checks,
and physically reaps a blocked connector at its extraction deadline.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from multiprocessing.connection import Connection
from queue import Empty, Full, Queue
from threading import Event, Thread

from src.domain.ingestion.errors import (
    AcquisitionDeadline,
    AcquisitionFailed,
    IngestionError,
    SourceRejected,
)
from src.domain.ingestion.models import ExtractionLimits
from src.infrastructure.databricks import sql_runtime
from src.infrastructure.databricks.sql_runtime import (
    ConnectFactory as ConnectFactory,
)
from src.infrastructure.databricks.sql_runtime import (
    connect_authenticated,
    read_secret,
)
from src.infrastructure.databricks.sql_runtime import (
    connect_sql as _official_connect,
)
from src.infrastructure.databricks.sql_runtime import (
    error_kind as _error_kind,
)
from src.infrastructure.databricks.sql_runtime import (
    sql_kind as _sql_kind,
)
from src.infrastructure.databricks.sql_runtime import (
    stop_process as _stop_process,
)
from src.infrastructure.ingestion.adapters.databricks_contract import (
    ConnectionConfig,
    Template,
    build_query,
    encode_row,
)
from src.infrastructure.ingestion.storage.raw_snapshot_writer import CheckControl

# Preserve adapter-level imports used by existing callers.
SQLConnection = sql_runtime.SQLConnection
SQLCursor = sql_runtime.SQLCursor
_connect_authenticated = connect_authenticated
_read_secret = read_secret

RowProducer = Callable[[ConnectionConfig, Template, ExtractionLimits], Iterable[bytes]]


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
