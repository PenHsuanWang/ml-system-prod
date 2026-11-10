"""Approved templates, fingerprint binding, parameterized SQL and typed JSONL."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from src.domain.ingestion.errors import IngestionError, SourceRejected
from src.domain.ingestion.models import SourceDefinition
from src.domain.ml import canonical_json
from src.infrastructure.ingestion.adapters.databricks_contract import (
    SERIALIZATION_VERSION,
    Column,
    ConnectionConfig,
    Predicate,
    Template,
    build_query,
    encode_row,
    parse_connections,
    parse_templates,
    validate_source,
)


@pytest.fixture
def connection():
    return ConnectionConfig(
        "approved.cloud.databricks.com",
        "/sql/1.0/warehouses/abcdef",
        "principal-123",
        Path("/run/secrets/databricks-client"),
    )


@pytest.fixture
def template():
    return Template(
        "warehouse",
        ("main", "approved", "events"),
        42,
        (Column("lot", "string"), Column("value", "decimal")),
        (Predicate("lot", "eq", "LOT' OR 1=1 --"),),
    )


def source_for(template, connection, **overrides):
    config = {
        "template_ref": "events-v1",
        "template_digest": template.digest(connection),
        "serialization_version": SERIALIZATION_VERSION,
    }
    config.update(overrides)
    return SourceDefinition(
        "events", "v1", "tenant-a", "databricks-sql", "1", canonical_json(config)
    )


def test_template_fingerprint_binds_effective_approval_but_allows_secret_rotation(
    template, connection
):
    value = template.digest(connection)
    assert value == template.digest(
        replace(connection, client_secret_file=Path("/run/secrets/rotated"))
    )
    for changed in (
        replace(template, delta_version=43),
        replace(template, table=("main", "other", "events")),
        replace(template, predicates=(Predicate("lot", "eq", "different"),)),
        replace(template, projection=(Column("lot", "string"), Column("value", "float"))),
    ):
        assert changed.digest(connection) != value
    for changed in (
        replace(connection, server_hostname="other.cloud.databricks.com"),
        replace(connection, http_path="/sql/1.0/warehouses/123456"),
        replace(connection, client_id="another-principal"),
    ):
        assert template.digest(changed) != value
    with pytest.raises(FrozenInstanceError):
        template.delta_version = 99
    assert "/run/secrets" not in repr(connection)


def test_fixed_delta_query_uses_only_identifiers_and_bound_predicates(template):
    sql, parameters = build_query(template, 100)
    assert sql == (
        "SELECT `lot`, `value` FROM `main`.`approved`.`events` VERSION AS OF 42 "
        "WHERE `lot` = :predicate_0 ORDER BY `lot`, `value` LIMIT :extract_limit"
    )
    assert "LOT' OR" not in sql
    assert parameters == {"predicate_0": "LOT' OR 1=1 --", "extract_limit": 101}
    null_template = replace(template, predicates=(Predicate("value", "is_null"),))
    assert "`value` IS NULL" in build_query(null_template, 1)[0]
    assert build_query(null_template, 1)[1] == {"extract_limit": 2}


@pytest.mark.parametrize("version", [-1, True, 1.5, "42", 2**63, None])
def test_delta_version_is_an_explicit_nonnegative_integer(template, version):
    with pytest.raises(SourceRejected):
        replace(template, delta_version=version)


@pytest.mark.parametrize(
    "hostname",
    [
        "http://approved.cloud.databricks.com",
        "127.0.0.1",
        "169.254.169.254",
        "approved.cloud.databricks.com:443",
        "approved.cloud.databricks.com.evil.test",
        "user@approved.cloud.databricks.com",
        "approved..cloud.databricks.com",
        "approved.cloud.databricks.com/",
        "approved.cloud.databricks.com.",
    ],
)
def test_nonapproved_endpoints_are_rejected(connection, hostname):
    with pytest.raises(SourceRejected):
        replace(connection, server_hostname=hostname)


@pytest.mark.parametrize(
    "path",
    [
        "/api/2.0/sql/statements",
        "/sql/1.0/warehouses/../secret",
        "https://x",
        "/sql/1.0/warehouses/a?token=bad",
    ],
)
def test_only_warehouse_http_paths_are_allowed(connection, path):
    with pytest.raises(SourceRejected):
        replace(connection, http_path=path)


@pytest.mark.parametrize(
    "predicate",
    [
        lambda: Predicate("lot", "execute", "x"),
        lambda: Predicate("lot; DROP TABLE x", "eq", "x"),
        lambda: Predicate("lot", "eq", object()),
        lambda: Predicate("lot", "eq", {"sql": "x"}),
        lambda: Predicate("lot", "eq", float("nan")),
        lambda: Predicate("lot", "eq", "e\u0301"),
        lambda: Predicate("lot", "eq", "\ud800"),
        lambda: Predicate("lot", "is_null", "unexpected"),
    ],
)
def test_predicate_contract_cannot_embed_code_or_ambiguous_values(predicate):
    with pytest.raises(SourceRejected):
        predicate()


def test_oversized_predicate_is_rejected_before_utf8_allocation():
    class OversizedString(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("Oversized predicate must not be encoded")

    with pytest.raises(SourceRejected):
        Predicate("lot", "eq", OversizedString("x" * 4097))


def test_template_rejects_ambiguous_columns_and_unapproved_predicate_columns(template):
    for projection in (
        (Column("lot", "string"), Column("LOT", "string")),
        (),
        [Column("lot", "string")],
    ):
        with pytest.raises(SourceRejected):
            replace(template, projection=projection)
    with pytest.raises(SourceRejected):
        replace(template, predicates=(Predicate("not_projected", "eq", 1),))
    with pytest.raises(SourceRejected):
        replace(template, table=("main", "safe", "events` WHERE true --"))
    with pytest.raises(SourceRejected):
        Column("payload", "unknown-vendor-type")


def test_source_requires_exact_template_approval_at_execution(template, connection):
    source = source_for(template, connection)
    assert validate_source(source, {"events-v1": template}, {"warehouse": connection}) == template
    changes = (
        {"template_digest": "a" * 64},
        {"template_ref": "unknown"},
        {"serialization_version": "v-next"},
        {"sql": "SELECT secret"},
        {"token": "DO_NOT_STORE"},
        {"server_hostname": "evil.test"},
    )
    for changed in changes:
        with pytest.raises(SourceRejected):
            validate_source(
                source_for(template, connection, **changed),
                {"events-v1": template},
                {"warehouse": connection},
            )
    with pytest.raises(SourceRejected):
        validate_source(
            source, {"events-v1": replace(template, delta_version=43)}, {"warehouse": connection}
        )
    with pytest.raises(SourceRejected):
        validate_source(source, {"events-v1": template}, {})


def test_json_parsers_reject_unknown_configuration_fields_and_preserve_schema(template, connection):
    connections = parse_connections(
        {
            "warehouse": {
                "server_hostname": connection.server_hostname,
                "http_path": connection.http_path,
                "client_id": connection.client_id,
                "client_secret_file": str(connection.client_secret_file),
            }
        }
    )
    templates = parse_templates(
        {
            "events-v1": {
                "connection_ref": "warehouse",
                "table": ["main", "approved", "events"],
                "delta_version": 42,
                "projection": [
                    {"name": "lot", "type": "string"},
                    {"name": "value", "type": "decimal"},
                ],
                "predicates": [{"column": "lot", "operator": "eq", "value": "LOT' OR 1=1 --"}],
            }
        }
    )
    assert templates["events-v1"] == template
    assert connections["warehouse"] == connection
    with pytest.raises(SourceRejected):
        parse_connections({"warehouse": {"sql": "SELECT secret"}})
    with pytest.raises(SourceRejected):
        parse_templates(
            {
                "events-v1": {
                    "connection_ref": "warehouse",
                    "table": ["x"],
                    "delta_version": 1,
                    "projection": [],
                    "python_class": "unsafe.Driver",
                }
            }
        )


def one_column(kind):
    return Template("warehouse", ("main", "approved", "events"), 0, (Column("value", kind),))


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("boolean", True, True),
        ("integer", -(2**63), -(2**63)),
        ("float", 1.25, 1.25),
        ("decimal", Decimal("12345678901234567890.123400"), "12345678901234567890.123400"),
        ("string", '繁體中文\nquoted"', '繁體中文\nquoted"'),
        ("date", date(2026, 11, 8), "2026-11-08"),
        ("timestamp", datetime(2026, 11, 8, 12, 30, 45, 123), "2026-11-08T12:30:45.000123Z"),
        (
            "timestamp",
            datetime(2026, 11, 8, 20, 30, 45, 123, tzinfo=timezone(timedelta(hours=8))),
            "2026-11-08T12:30:45.000123Z",
        ),
        ("binary", b"\x00\xff", "AP8="),
        ("binary", bytearray(b"x"), "eA=="),
    ],
)
def test_explicit_codec_is_lossless_and_has_fixed_jsonl_shape(kind, value, expected):
    encoded = encode_row(one_column(kind), (value,), 4096)
    assert encoded.endswith(b"\n")
    assert json.loads(encoded) == {"value": expected}
    assert encoded == encode_row(one_column(kind), (value,), 4096)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("integer", True),
        ("integer", 2**63),
        ("float", 1),
        ("float", float("nan")),
        ("float", float("inf")),
        ("decimal", Decimal("NaN")),
        ("decimal", Decimal("1e-39")),
        ("decimal", Decimal("9" * 39)),
        ("string", object()),
        ("string", "\ud800"),
        ("date", datetime(2026, 11, 8, tzinfo=UTC)),
        ("binary", {"unknown": "object"}),
        ("binary", memoryview(b"1234")[::2]),
    ],
)
def test_unknown_or_out_of_contract_sql_values_fail_closed(kind, value):
    with pytest.raises(IngestionError):
        encode_row(one_column(kind), (value,), 4096)


def test_codec_preserves_nulls_and_decimal_precision_without_default_str():
    for kind in ("boolean", "integer", "float", "decimal", "string", "date", "timestamp", "binary"):
        assert encode_row(one_column(kind), (None,), 4096) == b'{"value":null}\n'
    with localcontext() as context:
        context.prec = 2
        assert json.loads(encode_row(one_column("decimal"), (Decimal("123.45600"),), 4096)) == {
            "value": "123.45600"
        }

    class PrivateObject:
        def __str__(self):
            raise AssertionError("Unknown SQL objects must never be implicitly stringified")

    with pytest.raises(IngestionError):
        encode_row(one_column("string"), (PrivateObject(),), 4096)


def test_line_budget_includes_json_escaping_keys_and_newline():
    encoded = encode_row(one_column("string"), ("測試",), 100)
    assert encode_row(one_column("string"), ("測試",), len(encoded)) == encoded
    with pytest.raises(IngestionError):
        encode_row(one_column("string"), ("測試",), len(encoded) - 1)
    with pytest.raises(IngestionError):
        encode_row(one_column("string"), ("\x00" * 20,), 40)
    with pytest.raises(IngestionError):
        encode_row(one_column("integer"), (), 4096)
    with pytest.raises(IngestionError):
        encode_row(one_column("integer"), "123", 4096)
