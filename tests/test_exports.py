import csv
import io
import json
from collections.abc import AsyncIterator, Mapping
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.auth import current_principal
from src.api.exports.routers import get_export_service, router
from src.application.exports.service import ExportService
from src.domain.exports.errors import (
    ExportRowTooLarge,
    ExportSourceNotFound,
    ExportUnavailable,
    InvalidExportRequest,
)
from src.domain.exports.models import ExportColumn, ExportRequest, ExportRow, JSONValue
from src.domain.identity.errors import AuthorizationDenied
from src.domain.identity.models import Principal
from src.infrastructure.persistence.exports import MongoExportCursor, MongoExportSource


def principal(*extra_permissions: str) -> Principal:
    return Principal(
        "user-a",
        "tenant-a",
        "account-a",
        frozenset({"data.read", "data.export", *extra_permissions}),
    )


class FakeSource:
    def __init__(
        self,
        records: list[ExportRow],
        columns: tuple[ExportColumn, ...] = (ExportColumn("value"),),
        fail_after: int | None = None,
    ) -> None:
        self.columns = columns
        self.records = records
        self.read_count = 0
        self.closed = False
        self.limit: int | None = None
        self.fail_after = fail_after

    async def rows(self, user: Principal, *, limit: int) -> AsyncIterator[ExportRow]:
        self.limit = limit
        try:
            for record in self.records:
                if self.fail_after is not None and self.read_count >= self.fail_after:
                    raise RuntimeError(
                        "mongodb://secret-user:secret-password@host/private-collection"
                    )
                self.read_count += 1
                yield record
        finally:
            self.closed = True


class FakeCursor:
    def __init__(self, documents: list[Mapping[str, object]]) -> None:
        self.documents = documents
        self.row_limit: int | None = None
        self.batch: int | None = None
        self.closed = False

    def limit(self, limit: int) -> "FakeCursor":
        self.row_limit = limit
        return self

    def batch_size(self, batch_size: int) -> "FakeCursor":
        self.batch = batch_size
        return self

    async def __aiter__(self) -> AsyncIterator[Mapping[str, object]]:
        # Deliberately ignore the driver limit to also test the adapter's guard.
        for document in self.documents:
            yield document

    async def close(self) -> None:
        self.closed = True


class FakeCollection:
    def __init__(self, documents: list[Mapping[str, object]], fail: bool = False) -> None:
        self.documents = documents
        self.queries: list[dict[str, object]] = []
        self.projections: list[dict[str, int]] = []
        self.cursor: FakeCursor | None = None
        self.fail = fail

    def find(
        self, filter: Mapping[str, object], projection: Mapping[str, int]
    ) -> MongoExportCursor:
        if self.fail:
            raise RuntimeError("mongodb://secret-user:secret-password@host/private-collection")
        self.queries.append(dict(filter))
        self.projections.append(dict(projection))
        documents = [
            document
            for document in self.documents
            if all(document.get(key) == value for key, value in filter.items())
        ]
        self.cursor = FakeCursor(documents)
        return self.cursor


async def download(service: ExportService, request: ExportRequest, user: Principal) -> bytes:
    prepared = await service.prepare(request, user)
    return b"".join([part async for part in prepared.stream])


async def test_no_registered_sources_has_empty_listing_and_unknown_source() -> None:
    service = ExportService({})
    assert service.list_sources(principal()) == ()
    with pytest.raises(ExportSourceNotFound, match="does not exist"):
        await service.prepare(ExportRequest("alpha.rows"), principal())


@pytest.mark.parametrize(
    "permissions", [frozenset(), frozenset({"data.read"}), frozenset({"data.export"})]
)
async def test_listing_and_download_require_read_and_export(permissions: frozenset[str]) -> None:
    user = Principal("user-a", "tenant-a", "account-a", permissions)
    source = FakeSource([{"value": 1}])
    service = ExportService({"alpha.rows": source})
    with pytest.raises(AuthorizationDenied):
        service.list_sources(user)
    with pytest.raises(AuthorizationDenied):
        await service.prepare(ExportRequest("alpha.rows"), user)
    assert source.read_count == 0


async def test_service_masks_unregistered_and_unauthorized_fields_in_jsonl() -> None:
    source = FakeSource(
        [{"value": "測試", "restricted": 42, "unlisted": "hidden", "password_hash": "hidden"}],
        (ExportColumn("value"), ExportColumn("restricted", "column.read")),
    )
    service = ExportService({"alpha.rows": source})
    listing = service.list_sources(principal())
    assert [column.name for column in listing[0].columns] == ["value"]
    payload = await download(service, ExportRequest("alpha.rows", "jsonl"), principal())
    assert json.loads(payload) == {"value": "測試"}
    allowed = await download(
        service, ExportRequest("alpha.rows", "jsonl"), principal("column.read")
    )
    assert json.loads(allowed) == {"value": "測試", "restricted": 42}


async def test_no_visible_columns_hides_source_and_rejects_download() -> None:
    source = FakeSource([{"value": 1}], (ExportColumn("value", "column.read"),))
    service = ExportService({"alpha.rows": source})
    assert service.list_sources(principal()) == ()
    with pytest.raises(AuthorizationDenied):
        await service.prepare(ExportRequest("alpha.rows"), principal())


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)", "\ttext", "\rtext", "  =1", " \ttext", " \rtext"],
)
async def test_csv_escapes_formula_and_control_prefixes(value: str) -> None:
    service = ExportService({"alpha.rows": FakeSource([{"value": value}])})
    payload = await download(service, ExportRequest("alpha.rows"), principal())
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
    assert rows == [["value"], ["'" + value]]


async def test_csv_quotes_commas_quotes_newlines_and_preserves_plain_values() -> None:
    value = 'a,"b"\nc'
    service = ExportService({"alpha.rows": FakeSource([{"value": value}, {"value": None}])})
    payload = await download(service, ExportRequest("alpha.rows"), principal())
    assert list(csv.reader(io.StringIO(payload.decode()))) == [["value"], [value], [""]]


async def test_stream_is_lazy_bounded_and_closes_source() -> None:
    source = FakeSource([{"value": number} for number in range(100)])
    service = ExportService({"alpha.rows": source})
    prepared = await service.prepare(ExportRequest("alpha.rows", "jsonl", 2), principal())
    assert source.read_count == 1
    assert source.limit == 2
    iterator = prepared.stream
    assert json.loads(await anext(iterator)) == {"value": 0}
    assert source.read_count == 1
    assert json.loads(await anext(iterator)) == {"value": 1}
    assert source.read_count == 2
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert source.closed
    assert source.read_count == 2


async def test_closing_partial_stream_closes_source() -> None:
    source = FakeSource([{"value": number} for number in range(100)])
    prepared = await ExportService({"alpha.rows": source}).prepare(
        ExportRequest("alpha.rows"), principal()
    )
    assert await anext(prepared.stream) == b"value\r\n"
    await prepared.stream.aclose()  # type: ignore[attr-defined]
    assert source.closed
    assert source.read_count == 1


async def test_empty_csv_still_contains_header() -> None:
    source = FakeSource([])
    assert (
        await download(
            ExportService({"alpha.rows": source}), ExportRequest("alpha.rows"), principal()
        )
        == b"value\r\n"
    )
    assert source.closed


async def test_configured_max_rows_cannot_be_exceeded() -> None:
    source = FakeSource([{"value": 1}])
    service = ExportService({"alpha.rows": source}, max_rows=3)
    with pytest.raises(InvalidExportRequest, match="configured maximum"):
        await service.prepare(ExportRequest("alpha.rows", limit=4), principal())
    assert source.read_count == 0


async def test_row_bytes_are_bounded_before_first_chunk() -> None:
    source = FakeSource([{"value": "x" * 1000}])
    with pytest.raises(ExportRowTooLarge):
        await ExportService({"alpha.rows": source}, max_row_bytes=64).prepare(
            ExportRequest("alpha.rows", "jsonl"), principal()
        )
    assert source.closed


async def test_jsonl_hides_nested_protected_fields_and_rejects_non_json_values() -> None:
    source = FakeSource([{"value": {"label": "safe", "access_token": "secret", "_id": "private"}}])
    payload = await download(
        ExportService({"alpha.rows": source}), ExportRequest("alpha.rows", "jsonl"), principal()
    )
    assert json.loads(payload) == {"value": {"label": "safe"}}
    invalid = FakeSource([{"value": cast(JSONValue, object())}])
    with pytest.raises(ExportUnavailable, match="unsupported data"):
        await ExportService({"alpha.rows": invalid}).prepare(
            ExportRequest("alpha.rows"), principal()
        )


async def test_late_stream_failure_is_sanitized_and_closes_source() -> None:
    source = FakeSource([{"value": 1}, {"value": 2}], fail_after=1)
    prepared = await ExportService({"alpha.rows": source}).prepare(
        ExportRequest("alpha.rows", "jsonl"), principal()
    )
    assert json.loads(await anext(prepared.stream)) == {"value": 1}
    with pytest.raises(ExportUnavailable) as failure:
        await anext(prepared.stream)
    assert str(failure.value) == "Export source is unavailable"
    assert failure.value.__suppress_context__
    assert source.closed


@pytest.mark.parametrize(
    "name",
    ["_id", "password_hash", "access_token", "session", "api_key", "private_key", "profile.secret"],
)
def test_protected_or_nested_column_names_cannot_be_registered(name: str) -> None:
    with pytest.raises(InvalidExportRequest):
        ExportColumn(name)


@pytest.mark.parametrize("name", ["alpha\r\nInjected", "../rows", 'alpha."rows', "alpha.$where"])
def test_source_names_cannot_inject_headers_or_queries(name: str) -> None:
    with pytest.raises(InvalidExportRequest):
        ExportRequest(name)


@pytest.mark.parametrize("limit", [0, -1, 10_001, True])
def test_request_limit_is_bounded(limit: int) -> None:
    with pytest.raises(InvalidExportRequest):
        ExportRequest("alpha.rows", limit=limit)


def test_duplicate_source_columns_are_rejected() -> None:
    with pytest.raises(InvalidExportRequest, match="unique"):
        ExportService(
            {"alpha.rows": FakeSource([], (ExportColumn("value"), ExportColumn("value")))}
        )


async def test_mongo_source_enforces_tenant_and_owner_on_query() -> None:
    collection = FakeCollection(
        [
            {
                "tenant_id": "tenant-a",
                "owner_id": "user-a",
                "value": "own",
                "restricted": "sensitive",
            },
            {"tenant_id": "tenant-a", "owner_id": "user-b", "value": "other owner"},
            {"tenant_id": "tenant-b", "owner_id": "user-a", "value": "other tenant"},
        ]
    )
    source = MongoExportSource(
        collection, (ExportColumn("value"), ExportColumn("restricted", "column.read"))
    )
    records = [row async for row in source.rows(principal(), limit=10)]
    assert records == [{"value": "own"}]
    assert collection.queries == [{"tenant_id": "tenant-a", "owner_id": "user-a"}]
    assert collection.projections == [{"value": 1, "_id": 0}]
    assert collection.cursor is not None
    assert collection.cursor.closed
    assert collection.cursor.row_limit == 10
    assert collection.cursor.batch == 10


async def test_mongo_read_all_keeps_tenant_constraint() -> None:
    collection = FakeCollection(
        [
            {"tenant_id": "tenant-a", "owner_id": "user-b", "value": "same tenant"},
            {"tenant_id": "tenant-b", "owner_id": "user-a", "value": "other tenant"},
        ]
    )
    source = MongoExportSource(collection, (ExportColumn("value"),))
    records = [row async for row in source.rows(principal("data.read_all"), limit=10)]
    assert records == [{"value": "same tenant"}]
    assert collection.queries == [{"tenant_id": "tenant-a"}]


async def test_mongo_adapter_enforces_limit_even_if_driver_ignores_it() -> None:
    collection = FakeCollection(
        [{"tenant_id": "tenant-a", "owner_id": "user-a", "value": number} for number in range(10)]
    )
    source = MongoExportSource(collection, (ExportColumn("value"),))
    assert [row async for row in source.rows(principal(), limit=2)] == [{"value": 0}, {"value": 1}]


async def test_mongo_adapter_rechecks_permissions_when_used_directly() -> None:
    collection = FakeCollection([])
    source = MongoExportSource(collection, (ExportColumn("value"),))
    user = Principal("user-a", "tenant-a", "account-a", frozenset({"data.read"}))
    with pytest.raises(AuthorizationDenied):
        await anext(source.rows(user, limit=10))
    assert collection.queries == []


async def test_mongo_initial_failure_is_sanitized_before_streaming() -> None:
    collection = FakeCollection([], fail=True)
    source = MongoExportSource(collection, (ExportColumn("value"),))
    with pytest.raises(ExportUnavailable) as failure:
        await ExportService({"alpha.rows": source}).prepare(
            ExportRequest("alpha.rows"), principal()
        )
    assert str(failure.value) == "Export source is unavailable"
    assert failure.value.__suppress_context__


async def test_export_http_routes_stream_only_registered_sources() -> None:
    app = FastAPI()
    app.include_router(router)
    service = ExportService({"alpha.rows": FakeSource([{"value": "safe"}])})
    app.dependency_overrides[get_export_service] = lambda: service
    app.dependency_overrides[current_principal] = lambda: principal()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get("/exports/sources")
        assert listing.status_code == 200
        assert listing.json() == [
            {"source_name": "alpha.rows", "columns": [{"name": "value", "permission": None}]}
        ]
        response = await client.get("/exports/alpha.rows", params={"format": "jsonl", "limit": 2})
        assert response.status_code == 200
        assert json.loads(response.content) == {"value": "safe"}
        assert response.headers["content-disposition"] == 'attachment; filename="alpha.rows.jsonl"'
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/exports/unregistered.rows")).status_code == 404
        assert (
            await client.get("/exports/alpha.rows", params={"format": "sql"})
        ).status_code == 422
        assert (await client.get("/exports/alpha.rows", params={"limit": 10001})).status_code == 422


async def test_http_export_source_failures_are_sanitized() -> None:
    app = FastAPI()
    app.include_router(router)
    source = MongoExportSource(FakeCollection([], fail=True), (ExportColumn("value"),))
    app.dependency_overrides[get_export_service] = lambda: ExportService({"alpha.rows": source})
    app.dependency_overrides[current_principal] = lambda: principal()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/exports/alpha.rows")
    assert response.status_code == 503
    assert response.json() == {"detail": "Export source is unavailable"}


async def test_http_exports_reject_missing_bearer_before_using_runtime() -> None:
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/exports/sources")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
