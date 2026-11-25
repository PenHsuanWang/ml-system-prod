import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.domain.catalog.models import (
    DiscoveryFailed,
    DiscoveryScope,
    DiscoveryTimeout,
    MetadataItem,
    MetadataQuery,
)
from src.domain.ml import CapacityExceeded, SchemaMismatch
from src.infrastructure.catalog.databricks_metadata import (
    DatabricksMetadataProvider,
    build_metadata_query,
    fetch_sql_metadata,
)

SCOPES = (DiscoveryScope(("synthetic", "fdc"), ("temperature", "unsupported")),)


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.cancelled = self.closed = False

    def execute(self, sql, parameters):
        self.sql, self.parameters = sql, parameters

    def fetchmany(self, size):
        self.size = size
        return self.rows

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, rows):
        self.value, self.closed = Cursor(rows), False

    def cursor(self, **budgets):
        self.budgets = budgets
        return self.value

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "kind,path,rows,names",
    [
        ("catalogs", (), [("synthetic",)], ["synthetic"]),
        ("schemas", ("synthetic",), [("fdc",)], ["fdc"]),
        ("tables", ("synthetic", "fdc"), [("trace", "MANAGED", "DELTA")], ["trace"]),
        (
            "columns",
            ("synthetic", "fdc", "trace"),
            [("temperature", "DOUBLE", "YES"), ("unsupported", "ARRAY<STRING>", "NO")],
            ["temperature", "unsupported"],
        ),
    ],
)
def test_real_adapter_fixed_sql_and_metadata_only(discovery, kind, path, rows, names):
    config = discovery[-1]
    connection = Connection(rows)
    query = MetadataQuery(kind, path, 5, 10)
    values = fetch_sql_metadata(config, query, SCOPES, connect=lambda *a: connection)
    assert [v.name for v in values] == names
    assert "system.information_schema." in connection.value.sql
    assert all(
        part not in connection.value.sql for part in ("synthetic", "fdc", "trace", "temperature")
    )
    assert connection.value.parameters["page_size"] == 6
    assert connection.value.parameters["page_offset"] == 10
    assert connection.value.size == 6 and connection.budgets["row_limit"] == 6
    assert connection.value.cancelled and connection.value.closed and connection.closed
    if kind == "columns":
        assert values[0].supported and values[0].nullable and values[0].ingestion_type == "float"
        assert not values[1].supported and values[1].ingestion_type is None
        assert values[1].native_type == "ARRAY<STRING>"
        assert "column_name IN" in connection.value.sql


@pytest.mark.parametrize("path", [("cat;DROP", "fdc"), ("cat", "fdc --"), ("cat", "a.b")])
def test_sql_injection_rejected(path):
    with pytest.raises(SchemaMismatch):
        MetadataQuery("tables", path)


@pytest.mark.parametrize(
    "rows", [[("bad", "STRING", "unknown")], [("name", "X" * 4097, "YES")], [("one",)]]
)
def test_malformed_metadata_rejected_and_closed(discovery, rows):
    connection = Connection(rows)
    with pytest.raises(ValueError):
        fetch_sql_metadata(
            discovery[-1],
            MetadataQuery("columns", ("synthetic", "fdc", "trace")),
            SCOPES,
            connect=lambda *a: connection,
        )
    assert connection.closed and connection.value.closed


def fixture_fetch(config, query, scopes):
    return (MetadataItem("trace"),)


def hanging_fetch(config, query, scopes):
    time.sleep(60)
    return ()


def oversized_fetch(config, query, scopes):
    return (MetadataItem("x", native_type="SECRET" * 100000),)


def failing_fetch(config, query, scopes):
    raise RuntimeError("SECRET external endpoint token")


def test_spawned_adapter_and_no_shared_mutable_client(discovery):
    provider = DatabricksMetadataProvider({"warehouse": discovery[-1]}, fetch=fixture_fetch)
    assert provider.fetch("warehouse", MetadataQuery("catalogs"), SCOPES) == (
        MetadataItem("trace"),
    )


@pytest.mark.parametrize("fetch", [oversized_fetch, failing_fetch])
def test_bounded_errors_are_sanitized(discovery, fetch):
    provider = DatabricksMetadataProvider({"warehouse": discovery[-1]}, fetch=fetch)
    with pytest.raises(DiscoveryFailed) as error:
        provider.fetch("warehouse", MetadataQuery("catalogs"), SCOPES)
    assert "SECRET" not in str(error.value)


def test_hard_timeout_capacity_and_physical_cleanup(discovery):
    before = {p.pid for p in multiprocessing.active_children()}
    provider = DatabricksMetadataProvider(
        {"warehouse": discovery[-1]}, timeout=0.6, capacity=1, fetch=hanging_fetch
    )
    with ThreadPoolExecutor(1) as executor:
        request = executor.submit(provider.fetch, "warehouse", MetadataQuery("catalogs"), SCOPES)
        deadline = time.monotonic() + 2
        while not provider.capacity._value == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not request.cancel()  # Cancelling an awaiter cannot stop the physical operation.
        with pytest.raises(CapacityExceeded):
            provider.fetch("warehouse", MetadataQuery("catalogs"), SCOPES)
        with pytest.raises(DiscoveryTimeout):
            request.result(timeout=5)
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert provider.capacity.acquire(blocking=False)
    provider.capacity.release()


def test_no_unbounded_or_arbitrary_relation_query():
    sql, params = build_metadata_query(MetadataQuery("tables", ("synthetic", "fdc")), SCOPES)
    assert sql.startswith(
        "SELECT DISTINCT table_name,table_type,data_source_format "
        "FROM system.information_schema.tables"
    )
    assert params["scope_0_0"] == "synthetic"
    assert "LIMIT :page_size OFFSET :page_offset" in sql


@pytest.mark.parametrize("native", ["DELTA", None])
def test_table_format_is_metadata_and_does_not_invent_delta_history(discovery, native):
    connection = Connection([("trace", "MANAGED" if native else "VIEW", native)])
    values = fetch_sql_metadata(
        discovery[-1],
        MetadataQuery("tables", ("synthetic", "fdc")),
        SCOPES,
        connect=lambda *a: connection,
    )
    assert values[0].data_format == native
    assert values[0].table_type == ("MANAGED" if native else "VIEW")
