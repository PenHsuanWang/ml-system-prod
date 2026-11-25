"""Bounded official SQL metadata queries with a separately configured identity."""

import json
import logging
import multiprocessing
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from multiprocessing.connection import Connection
from typing import Any

from src.domain.catalog.models import (
    DiscoveryFailed,
    DiscoveryScope,
    DiscoveryTimeout,
    MetadataItem,
    MetadataQuery,
    metadata_name,
)
from src.domain.ingestion.models import ExtractionLimits
from src.domain.ml import CapacityExceeded
from src.infrastructure.ingestion.adapters.databricks_client import (
    ConnectFactory,
    _error_kind,
    _official_connect,
    _sql_kind,
    _stop_process,
)
from src.infrastructure.ingestion.adapters.databricks_contract import ConnectionConfig

MAX_BYTES = 256 * 1024
MetadataFetch = Callable[
    [ConnectionConfig, MetadataQuery, tuple[DiscoveryScope, ...]], tuple[MetadataItem, ...]
]


def build_metadata_query(
    query: MetadataQuery, scopes: tuple[DiscoveryScope, ...]
) -> tuple[str, dict[str, object]]:
    """Only fixed metadata relations/fields; every caller value is a bound parameter."""
    relations = {
        "catalogs": ("catalogs", "catalog_name", "catalog_name"),
        "schemas": ("schemata", "schema_name", "catalog_name,schema_name"),
        "tables": ("tables", "table_name,table_type", "table_catalog,table_schema,table_name"),
        "columns": (
            "columns",
            "column_name,full_data_type,is_nullable",
            "table_catalog,table_schema,table_name",
        ),
    }
    relation, fields, path_fields = relations[query.kind]
    names = path_fields.split(",")
    parameters: dict[str, object] = {"page_size": query.limit + 1, "page_offset": query.offset}
    where = []
    for i, part in enumerate(query.path):
        where.append(f"{names[i]} = :path_{i}")
        parameters[f"path_{i}"] = part
    allowed = []
    for i, scope in enumerate(scopes):
        terms = []
        for j, part in enumerate(scope.table_scope[: len(names)]):
            key = f"scope_{i}_{j}"
            terms.append(f"{names[j]} = :{key}")
            parameters[key] = part
        if query.kind == "columns":
            keys = []
            for j, column in enumerate(scope.columns):
                key = f"column_{i}_{j}"
                parameters[key] = column
                keys.append(":" + key)
            terms.append("column_name IN (" + ",".join(keys) + ")")
        allowed.append("(" + " AND ".join(terms) + ")")
    if not allowed:
        raise DiscoveryFailed("Discovery access is unavailable")
    where.append("(" + " OR ".join(allowed) + ")")
    sql = (
        f"SELECT DISTINCT {fields} FROM system.information_schema.{relation} WHERE "
        + " AND ".join(where)
        + f" ORDER BY {fields.split(',')[0]} LIMIT :page_size OFFSET :page_offset"
    )
    return sql, parameters


def _item(query: MetadataQuery, row: Any) -> MetadataItem:
    expected = 1 if query.kind in {"catalogs", "schemas"} else 2 if query.kind == "tables" else 3
    if not isinstance(row, (tuple, list)) or len(row) != expected:
        raise ValueError("Invalid metadata shape")
    name = metadata_name(row[0])
    if query.kind == "tables":
        if not isinstance(row[1], str) or len(row[1]) > 128:
            raise ValueError("Invalid table metadata")
        return MetadataItem(name, table_type=row[1])
    if query.kind == "columns":
        native, nullable = row[1:]
        if (
            not isinstance(native, str)
            or not 1 <= len(native) <= 4096
            or nullable not in {"YES", "NO"}
        ):
            raise ValueError("Invalid column metadata")
        kind = _sql_kind(native)
        return MetadataItem(
            name,
            native_type=native,
            nullable=nullable == "YES",
            ingestion_type=kind,
            supported=kind is not None,
        )
    return MetadataItem(name)


def fetch_sql_metadata(
    config: ConnectionConfig,
    query: MetadataQuery,
    scopes: tuple[DiscoveryScope, ...],
    *,
    connect: ConnectFactory = _official_connect,
) -> tuple[MetadataItem, ...]:
    sql, parameters = build_metadata_query(query, scopes)
    limits = ExtractionLimits(max_bytes=MAX_BYTES, max_records=query.limit + 1, timeout_seconds=10)
    connection = connect(config, limits)
    cursor = None
    try:
        cursor = connection.cursor(
            arraysize=query.limit + 1, buffer_size_bytes=MAX_BYTES, row_limit=query.limit + 1
        )
        cursor.execute(sql, parameters=parameters)
        rows = cursor.fetchmany(query.limit + 1)
        if len(rows) > query.limit + 1:
            raise ValueError("Metadata row budget exceeded")
        items = tuple(_item(query, list(row)) for row in rows)
        if len(json.dumps([asdict(i) for i in items]).encode()) > MAX_BYTES:
            raise ValueError("Metadata byte budget exceeded")
        return items
    finally:
        if cursor is not None:
            try:
                cursor.cancel()
            finally:
                try:
                    cursor.close()
                finally:
                    connection.close()
        else:
            connection.close()


def _metadata_child(
    sender: Connection,
    config: ConnectionConfig,
    query: MetadataQuery,
    scopes: tuple[DiscoveryScope, ...],
    fetch: MetadataFetch,
) -> None:
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        try:
            values = fetch(config, query, scopes)
            if len(values) > query.limit + 1:
                raise ValueError
            data = json.dumps([asdict(i) for i in values], allow_nan=False).encode()
            if len(data) > MAX_BYTES:
                raise ValueError
            sender.send_bytes(b"D" + data)
        except Exception as error:
            sender.send_bytes(b"E" + _error_kind(error))
        finally:
            sender.close()


class DatabricksMetadataProvider:
    def __init__(
        self,
        connections: dict[str, ConnectionConfig],
        *,
        timeout: float = 12,
        capacity: int = 2,
        fetch: MetadataFetch = fetch_sql_metadata,
    ) -> None:
        if not 0 < timeout <= 30 or not 1 <= capacity <= 8:
            raise ValueError("Invalid discovery execution budget")
        self.connections, self.timeout, self.fetcher = dict(connections), timeout, fetch
        self.capacity = threading.BoundedSemaphore(capacity)

    def fetch(
        self, connection_ref: str, query: MetadataQuery, scopes: tuple[DiscoveryScope, ...]
    ) -> tuple[MetadataItem, ...]:
        config = self.connections.get(connection_ref)
        if config is None:
            raise DiscoveryFailed("Discovery connection is unavailable")
        if not self.capacity.acquire(blocking=False):
            raise CapacityExceeded("Discovery capacity is occupied")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_metadata_child, args=(sender, config, query, scopes, self.fetcher)
        )
        messages: queue.Queue[bytes | None] = queue.Queue(maxsize=1)

        def receive() -> None:
            try:
                messages.put(receiver.recv_bytes(MAX_BYTES + 1))
            except (OSError, EOFError):
                messages.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        started = False
        reading = False
        deadline = time.monotonic() + self.timeout
        try:
            process.start()
            started = True
            sender.close()
            reader.start()
            reading = True
            try:
                data = messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise DiscoveryTimeout("Metadata request exceeded its deadline") from None
            if data is None or not data.startswith(b"D"):
                if data == b"ETIMEOUT":
                    raise DiscoveryTimeout("Metadata request exceeded its deadline")
                raise DiscoveryFailed("Metadata request failed")
            try:
                values = json.loads(data[1:])
                if not isinstance(values, list) or len(values) > query.limit + 1:
                    raise ValueError
                return tuple(MetadataItem(**value) for value in values)
            except (ValueError, TypeError):
                raise DiscoveryFailed("Metadata response is invalid") from None
        finally:
            # Keep the slot until the physical child AND pipe reader have stopped.
            # Cleanup failure deliberately retains capacity, preventing oversubscription.
            try:
                if started:
                    _stop_process(process)
            finally:
                sender.close()
                receiver.close()
                if reading:
                    reader.join(timeout=1)
            if reading and reader.is_alive():
                raise DiscoveryFailed("Metadata cleanup is incomplete")
            self.capacity.release()
