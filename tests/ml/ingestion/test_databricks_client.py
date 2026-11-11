"""SQL/OAuth boundary tests and real spawned-process cancellation/deadline tests."""

import json
import multiprocessing
import struct
import time
from dataclasses import replace

import pytest

from src.domain.ingestion.errors import (
    AcquisitionDeadline,
    AcquisitionFailed,
    IngestionCancelled,
    IngestionError,
    SourceRejected,
)
from src.domain.ingestion.models import ExtractionLimits
from src.domain.ml import Conflict
from src.infrastructure.ingestion.adapters.databricks_client import (
    DatabricksSQLClient,
    _connect_authenticated,
    _read_secret,
    _sdk_stream,
)
from src.infrastructure.ingestion.adapters.databricks_contract import (
    Column,
    ConnectionConfig,
    Template,
)
from tests.ml.ingestion.test_file_adapter import Control


@pytest.fixture
def connection(tmp_path):
    secret = tmp_path / "oauth-secret"
    secret.write_text("example-only-value\n")
    secret.chmod(0o600)
    return ConnectionConfig(
        "approved.cloud.databricks.com", "/sql/1.0/warehouses/abcdef", "principal", secret
    )


@pytest.fixture
def template():
    return Template("warehouse", ("main", "approved", "events"), 42, (Column("id", "integer"),))


class Cursor:
    description = (("id", "BIGINT", None, None, None, None, None),)

    def __init__(self, rows, failure=None):
        self.rows = list(rows)
        self.failure = failure
        self.fetches = []
        self.cancelled = self.closed = False

    def execute(self, operation, parameters):
        self.operation, self.parameters = operation, parameters

    def fetchmany(self, size):
        self.fetches.append(size)
        if self.failure:
            raise self.failure
        result, self.rows = self.rows[:size], self.rows[size:]
        return result

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class Session:
    def __init__(self, cursor):
        self.result = cursor
        self.closed = False

    def cursor(self, **options):
        self.options = options
        return self.result

    def close(self):
        self.closed = True


@pytest.mark.parametrize("kind", ["ARRAY", "MAP", "STRUCT", "VOID", "STRING"])
def test_sql_metadata_rejects_unknown_or_mismatched_types_even_when_all_null(
    connection, template, kind
):
    cursor = Cursor([(None,)])
    cursor.description = (("id", kind),)
    session = Session(cursor)
    with pytest.raises(SourceRejected):
        list(_sdk_stream(connection, template, ExtractionLimits(), connect=lambda *_: session))
    assert not cursor.fetches
    assert cursor.closed and cursor.cancelled and session.closed


def _partial_frame_send(connection, value):
    connection._send(struct.pack("!i", len(value)))
    time.sleep(60)


def _partial_frame_producer(*args):
    from multiprocessing.connection import Connection

    Connection._send_bytes = _partial_frame_send
    yield b"x" * 20000


def test_partial_ipc_frame_cannot_block_deadline_or_leave_a_reader(connection, template):
    import threading

    before = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    with pytest.raises(AcquisitionDeadline):
        list(
            DatabricksSQLClient(producer=_partial_frame_producer).stream(
                connection, template, ExtractionLimits(timeout_seconds=0.4), Control()
            )
        )
    assert time.monotonic() - started < 3
    assert not multiprocessing.active_children()
    assert not [
        thread
        for thread in threading.enumerate()
        if thread.ident not in before and thread.name == "databricks-result-reader"
    ]


def test_oauth_m2m_connection_is_explicit_private_and_uses_safe_driver_options(connection):
    received = {}

    def config(**kwargs):
        received["oauth"] = kwargs
        return kwargs

    def connect(**kwargs):
        received["connection"] = kwargs
        return Session(Cursor([]))

    _connect_authenticated(
        connection,
        ExtractionLimits(timeout_seconds=2.2),
        connect=connect,
        config_factory=config,
        oauth_provider=lambda value: {"Authorization": "Bearer example-header"},
    )
    assert received["oauth"]["auth_type"] == "oauth-m2m"
    assert received["oauth"]["client_secret"] == "example-only-value"
    options = received["connection"]
    assert "access_token" not in options and "client_secret" not in options
    assert callable(options["credentials_provider"])
    assert options["credentials_provider"]() == {"Authorization": "Bearer example-header"}
    assert options["use_inline_params"] is options["use_cloud_fetch"] is False
    assert options["enable_telemetry"] is options["force_enable_telemetry"] is False
    assert options["_socket_timeout"] == 2.2
    assert options["_retry_stop_after_attempts_count"] == 1
    assert options["session_configuration"] == {"TIMEZONE": "UTC", "STATEMENT_TIMEOUT": "3"}


@pytest.mark.parametrize("case", ["public", "empty", "oversize", "utf8", "symlink", "missing"])
def test_secret_files_fail_closed_without_returning_secret_details(connection, tmp_path, case):
    path = connection.client_secret_file
    if case == "public":
        path.chmod(0o644)
    elif case == "empty":
        path.write_bytes(b"")
    elif case == "oversize":
        path.write_bytes(b"x" * 4097)
    elif case == "utf8":
        path.write_bytes(b"\xff")
    elif case == "symlink":
        link = tmp_path / "secret-link"
        link.symlink_to(path)
        connection = replace(connection, client_secret_file=link)
    else:
        path.unlink()
    with pytest.raises(SourceRejected) as error:
        _read_secret(connection)
    assert "example-only-value" not in str(error.value)


def test_sdk_boundary_uses_fixed_version_bound_limit_fetchmany_and_closes(connection, template):
    cursor = Cursor([(1,), (2,)])
    session = Session(cursor)
    limits = ExtractionLimits(max_records=2)
    records = list(_sdk_stream(connection, template, limits, connect=lambda *args: session))
    assert [json.loads(value) for value in records] == [{"id": 1}, {"id": 2}]
    assert "VERSION AS OF 42" in cursor.operation
    assert cursor.parameters["extract_limit"] == 3
    assert cursor.fetches == [3, 1]
    assert session.options == {"arraysize": 3, "buffer_size_bytes": 4 * 1024 * 1024, "row_limit": 3}
    assert cursor.closed and session.closed and not cursor.cancelled


@pytest.mark.parametrize("case", ["records", "bytes", "line", "type", "schema", "network"])
def test_sdk_failures_close_and_cancel_without_truncating(connection, template, case):
    cursor = Cursor([(1,), (2,)])
    limits = ExtractionLimits(max_records=2)
    if case == "records":
        limits = replace(limits, max_records=1)
    elif case == "bytes":
        limits = replace(limits, max_bytes=1)
    elif case == "line":
        limits = replace(limits, max_line_bytes=1)
    elif case == "type":
        cursor.rows = [(object(),)]
    elif case == "schema":
        cursor.description = (("unexpected", "BIGINT"),)
    else:
        cursor.failure = TimeoutError("private-driver-detail")
    session = Session(cursor)
    with pytest.raises((IngestionError, TimeoutError)):
        list(_sdk_stream(connection, template, limits, connect=lambda *args: session))
    assert cursor.closed and cursor.cancelled and session.closed


def _hung_rows(*args):
    time.sleep(60)
    yield b'{"id":1}\n'


def _one_then_block(*args):
    yield b'{"id":1}\n'
    time.sleep(60)


def _failed_rows(*args):
    print("private-driver-detail")
    raise RuntimeError("private-driver-detail")
    yield b""  # Make this a lazily executed producer.


def _missing_sdk_rows(*args):
    raise ModuleNotFoundError("private-import-detail")
    yield b""


def _too_many_rows(*args):
    yield b'{"id":1}\n'
    yield b'{"id":2}\n'


def _assert_no_runtime_children():
    assert not any(p.name == "databricks-sql-extract" for p in multiprocessing.active_children())


def test_blocked_driver_is_physically_reaped_at_parent_deadline(connection, template):
    started = time.monotonic()
    with pytest.raises(AcquisitionDeadline):
        list(
            DatabricksSQLClient(producer=_hung_rows).stream(
                connection, template, ExtractionLimits(timeout_seconds=0.2), Control()
            )
        )
    assert time.monotonic() - started < 3
    _assert_no_runtime_children()


@pytest.mark.parametrize("failure", [IngestionCancelled, Conflict])
def test_control_failure_stops_blocked_child_and_preserves_category(connection, template, failure):
    class Interrupt:
        calls = 0

        def check(self):
            self.calls += 1
            if self.calls >= 3:
                raise failure("Safe control interruption")

    with pytest.raises(failure):
        list(
            DatabricksSQLClient(producer=_hung_rows).stream(
                connection, template, ExtractionLimits(), Interrupt()
            )
        )
    _assert_no_runtime_children()


def test_consumer_close_reaps_a_blocked_child(connection, template):
    stream = DatabricksSQLClient(producer=_one_then_block).stream(
        connection, template, ExtractionLimits(), Control()
    )
    assert next(stream) == b'{"id":1}\n'
    stream.close()
    _assert_no_runtime_children()


@pytest.mark.parametrize(
    "producer,error", [(_failed_rows, AcquisitionFailed), (_missing_sdk_rows, SourceRejected)]
)
def test_child_exception_details_and_stdout_never_cross_boundary(
    connection, template, capfd, producer, error
):
    with pytest.raises(error) as caught:
        list(
            DatabricksSQLClient(producer=producer).stream(
                connection, template, ExtractionLimits(), Control()
            )
        )
    assert "private-" not in str(caught.value)
    captured = capfd.readouterr()
    assert "private-" not in captured.out + captured.err
    _assert_no_runtime_children()


def test_parent_independently_rejects_record_overflow(connection, template):
    with pytest.raises(IngestionError, match="limits"):
        list(
            DatabricksSQLClient(producer=_too_many_rows).stream(
                connection, template, ExtractionLimits(max_records=1), Control()
            )
        )
    _assert_no_runtime_children()
