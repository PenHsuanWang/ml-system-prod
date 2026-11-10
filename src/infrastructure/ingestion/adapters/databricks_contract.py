"""Approved SQL Warehouse templates and a strict, versioned JSONL codec."""

from __future__ import annotations

import base64
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.domain.ingestion.errors import IngestionError, SourceRejected
from src.domain.ingestion.models import SourceDefinition, identifier, sha256
from src.domain.ml import digest as content_digest

SERIALIZATION_VERSION = "dbx-jsonl-v1"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_KINDS = frozenset(
    {"boolean", "integer", "float", "decimal", "string", "date", "timestamp", "binary"}
)
_OPERATORS = {"eq": "=", "ge": ">=", "gt": ">", "le": "<=", "lt": "<"}


def _name(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SourceRejected("Invalid approved Databricks SQL identifier")


def _reference(value: str) -> None:
    try:
        identifier(value)
    except IngestionError:
        raise SourceRejected("Invalid approved Databricks configuration reference") from None


@dataclass(frozen=True)
class ConnectionConfig:
    server_hostname: str
    http_path: str
    client_id: str
    client_secret_file: Path = field(repr=False)

    def __post_init__(self) -> None:
        hostname = self.server_hostname
        if (
            not isinstance(hostname, str)
            or not 1 <= len(hostname) <= 253
            or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname)
            or not hostname.lower().endswith(
                (".cloud.databricks.com", ".azuredatabricks.net", ".gcp.databricks.com")
            )
            or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in hostname.split(".")
            )
            or not isinstance(self.http_path, str)
            or not re.fullmatch(r"/sql/1\.0/warehouses/[a-f0-9]{1,64}", self.http_path)
            or not isinstance(self.client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", self.client_id)
            or not isinstance(self.client_secret_file, Path)
            or not self.client_secret_file.is_absolute()
            or ".." in self.client_secret_file.parts
            or len(str(self.client_secret_file)) > 4096
        ):
            raise SourceRejected("Invalid approved Databricks SQL connection")
        object.__setattr__(self, "server_hostname", hostname.lower())


@dataclass(frozen=True)
class Column:
    name: str
    type: str

    def __post_init__(self) -> None:
        _name(self.name)
        if not isinstance(self.type, str) or self.type not in _KINDS:
            raise SourceRejected("Unsupported approved Databricks column type")


@dataclass(frozen=True)
class Predicate:
    column: str
    operator: str
    value: object = None

    def __post_init__(self) -> None:
        _name(self.column)
        if self.operator == "is_null":
            if self.value is not None:
                raise SourceRejected("Null predicate cannot carry a bound value")
            return
        if not isinstance(self.operator, str) or self.operator not in _OPERATORS:
            raise SourceRejected("Unsupported approved Databricks predicate")
        value = self.value
        if isinstance(value, str):
            if len(value) > 4096:
                raise SourceRejected("Invalid approved Databricks predicate value")
            try:
                if len(value.encode("utf-8", errors="strict")) > 4096:
                    raise SourceRejected("Invalid approved Databricks predicate value")
            except UnicodeError:
                raise SourceRejected("Invalid approved Databricks predicate value") from None
        if (
            type(value) not in {str, bool, int, float}
            or type(value) is int
            and not -(2**63) <= value < 2**63
            or type(value) is float
            and not math.isfinite(value)
            or isinstance(value, str)
            and (len(value) > 4096 or unicodedata.normalize("NFC", value) != value)
        ):
            raise SourceRejected("Invalid approved Databricks predicate value")


@dataclass(frozen=True)
class Template:
    connection_ref: str
    table: tuple[str, str, str]
    delta_version: int
    projection: tuple[Column, ...]
    predicates: tuple[Predicate, ...] = ()

    def __post_init__(self) -> None:
        _reference(self.connection_ref)
        if (
            not isinstance(self.table, tuple)
            or len(self.table) != 3
            or type(self.delta_version) is not int
            or not 0 <= self.delta_version < 2**63
            or not isinstance(self.projection, tuple)
            or not 1 <= len(self.projection) <= 128
            or not all(isinstance(column, Column) for column in self.projection)
            or not isinstance(self.predicates, tuple)
            or len(self.predicates) > 32
            or not all(isinstance(predicate, Predicate) for predicate in self.predicates)
        ):
            raise SourceRejected("Invalid approved Databricks table template")
        for part in self.table:
            _name(part)
        names = {column.name for column in self.projection}
        if len({name.casefold() for name in names}) != len(self.projection) or any(
            predicate.column not in names for predicate in self.predicates
        ):
            raise SourceRejected("Ambiguous Databricks projection or predicate column")

    def digest(self, connection: ConnectionConfig) -> str:
        if not isinstance(connection, ConnectionConfig):
            raise SourceRejected("Invalid approved Databricks connection identity")
        return content_digest(
            {
                "contract": "databricks-source-v1",
                "serialization_version": SERIALIZATION_VERSION,
                "connection_ref": self.connection_ref,
                "connection": {
                    "server_hostname": connection.server_hostname,
                    "http_path": connection.http_path,
                    "client_id": connection.client_id,
                },
                "table": self.table,
                "delta_version": self.delta_version,
                "projection": [
                    {"name": column.name, "type": column.type} for column in self.projection
                ],
                "predicates": [
                    {"column": item.column, "operator": item.operator, "value": item.value}
                    for item in self.predicates
                ],
            }
        )


def _object(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - (optional or set())
    ):
        raise SourceRejected("Invalid approved Databricks configuration envelope")
    return value


def parse_connections(value: dict[str, Any]) -> dict[str, ConnectionConfig]:
    if not isinstance(value, dict) or len(value) > 32:
        raise SourceRejected("Invalid approved Databricks connection collection")
    result = {}
    for name, item in value.items():
        _reference(name)
        data = _object(item, {"server_hostname", "http_path", "client_id", "client_secret_file"})
        path = data["client_secret_file"]
        if not isinstance(path, str) or not 1 <= len(path) <= 4096:
            raise SourceRejected("Invalid Databricks credential file reference")
        result[name] = ConnectionConfig(
            data["server_hostname"], data["http_path"], data["client_id"], Path(path)
        )
    return result


def parse_templates(value: dict[str, Any]) -> dict[str, Template]:
    if not isinstance(value, dict) or len(value) > 128:
        raise SourceRejected("Invalid approved Databricks template collection")
    result = {}
    for name, item in value.items():
        _reference(name)
        data = _object(
            item, {"connection_ref", "table", "delta_version", "projection"}, {"predicates"}
        )
        if (
            not isinstance(data["table"], list)
            or len(data["table"]) != 3
            or not isinstance(data["projection"], list)
            or not 1 <= len(data["projection"]) <= 128
            or not isinstance(data.get("predicates", []), list)
            or len(data.get("predicates", [])) > 32
        ):
            raise SourceRejected("Invalid approved Databricks template shape")
        columns = []
        for column in data["projection"]:
            column = _object(column, {"name", "type"})
            columns.append(Column(column["name"], column["type"]))
        predicates = []
        for predicate in data.get("predicates", []):
            predicate = _object(predicate, {"column", "operator"}, {"value"})
            predicates.append(
                Predicate(predicate["column"], predicate["operator"], predicate.get("value"))
            )
        result[name] = Template(
            data["connection_ref"],
            tuple(data["table"]),
            data["delta_version"],
            tuple(columns),
            tuple(predicates),
        )
    return result


def validate_source(
    source: SourceDefinition,
    templates: Mapping[str, Template],
    connections: Mapping[str, ConnectionConfig],
) -> Template:
    if (
        source.adapter_type != "databricks-sql"
        or source.adapter_version != "1"
        or source.data_format != "jsonl"
        or source.schema_version != "raw-json-object-v1"
    ):
        raise SourceRejected("Unsupported approved Databricks source contract")
    data = _object(
        json.loads(source.configuration_json),
        {"template_ref", "template_digest", "serialization_version"},
    )
    _reference(data["template_ref"])
    try:
        sha256(data["template_digest"])
    except IngestionError:
        raise SourceRejected("Invalid approved Databricks template fingerprint") from None
    template = templates.get(data["template_ref"])
    if (
        data["serialization_version"] != SERIALIZATION_VERSION
        or template is None
        or template.connection_ref not in connections
        or template.digest(connections[template.connection_ref]) != data["template_digest"]
    ):
        raise SourceRejected("Databricks source approval does not match its template")
    return template


def build_query(template: Template, max_records: int) -> tuple[str, dict[str, object]]:
    if type(max_records) is not int or not 1 <= max_records <= 1_000_000:
        raise SourceRejected("Invalid bounded Databricks extraction request")
    projection = ", ".join(f"`{column.name}`" for column in template.projection)
    table = ".".join(f"`{part}`" for part in template.table)
    query = f"SELECT {projection} FROM {table} VERSION AS OF {template.delta_version}"
    parameters: dict[str, object] = {"extract_limit": max_records + 1}
    clauses = []
    for index, predicate in enumerate(template.predicates):
        if predicate.operator == "is_null":
            clauses.append(f"`{predicate.column}` IS NULL")
        else:
            name = f"predicate_{index}"
            clauses.append(f"`{predicate.column}` {_OPERATORS[predicate.operator]} :{name}")
            parameters[name] = predicate.value
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    return query + f" ORDER BY {projection} LIMIT :extract_limit", parameters


def _encode_value(column: Column, value: object, maximum: int) -> object:
    if value is None:
        return None
    kind = column.type
    if kind == "boolean" and type(value) is bool:
        return value
    if kind == "integer" and type(value) is int and -(2**63) <= value < 2**63:
        return value
    if kind == "float" and type(value) is float and math.isfinite(value):
        return value
    if kind == "decimal" and isinstance(value, Decimal) and value.is_finite():
        _, digits, exponent = value.as_tuple()
        if len(digits) <= 38 and isinstance(exponent, int) and abs(exponent) <= 38:
            return format(value, "f")
    if kind == "string" and isinstance(value, str) and len(value) <= maximum:
        if len(value.encode("utf-8", errors="strict")) <= maximum:
            return value
    if kind == "date" and type(value) is date:
        return value.isoformat()
    if kind == "timestamp" and isinstance(value, datetime):
        stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if kind == "binary" and isinstance(value, (bytes, bytearray, memoryview)):
        size = value.nbytes if isinstance(value, memoryview) else len(value)
        if size <= maximum and (not isinstance(value, memoryview) or value.c_contiguous):
            return base64.b64encode(value).decode("ascii")
    raise IngestionError("Databricks row does not match its approved serialization schema")


def encode_row(template: Template, row: Sequence[object], max_line_bytes: int) -> bytes:
    if (
        type(max_line_bytes) is not int
        or not 1 <= max_line_bytes <= 4 * 1024 * 1024
        or not isinstance(row, Sequence)
        or isinstance(row, (str, bytes, bytearray, memoryview))
        or len(row) != len(template.projection)
    ):
        raise IngestionError("Invalid bounded Databricks row envelope")
    try:
        parts = [b"{"]
        total = 3  # Opening brace, closing brace and newline.
        for index, (column, value) in enumerate(zip(template.projection, row, strict=True)):
            prefix = (b"," if index else b"") + json.dumps(column.name).encode("ascii") + b":"
            remaining = max_line_bytes - total - len(prefix)
            if remaining < 1:
                raise IngestionError("Databricks row exceeds the approved JSONL line limit")
            scalar = _encode_value(column, value, remaining)
            encoded = json.dumps(scalar, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > remaining:
                raise IngestionError("Databricks row exceeds the approved JSONL line limit")
            parts.extend((prefix, encoded))
            total += len(prefix) + len(encoded)
        result = b"".join([*parts, b"}\n"])
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise IngestionError("Databricks row serialization failed") from None
    if len(result) > max_line_bytes:
        raise IngestionError("Databricks row exceeds the approved JSONL line limit")
    return result
