from collections.abc import AsyncIterator, Mapping
from typing import Protocol, Self, cast

from src.domain.exports.errors import (
    ExportError,
    ExportUnavailable,
    InvalidExportRequest,
)
from src.domain.exports.models import MAX_EXPORT_ROWS, ExportColumn, ExportRow
from src.domain.exports.policy import authorize_export, visible_columns
from src.domain.identity.errors import AuthorizationDenied
from src.domain.identity.models import Principal


class MongoExportCursor(Protocol):
    def limit(self, limit: int) -> Self: ...

    def batch_size(self, batch_size: int) -> Self: ...

    def __aiter__(self) -> AsyncIterator[Mapping[str, object]]: ...

    async def close(self) -> None: ...


class MongoExportCollection(Protocol):
    def find(
        self, filter: Mapping[str, object], projection: Mapping[str, int]
    ) -> MongoExportCursor: ...


class MongoExportSource:
    """An async PyMongo-compatible adapter with mandatory tenant/owner isolation.

    The collection is supplied by trusted server-side module composition. Neither
    collection names nor query operators are accepted from an HTTP caller.
    Row/field permissions are rechecked here even when called without ExportService.
    """

    def __init__(
        self, collection: MongoExportCollection, columns: tuple[ExportColumn, ...]
    ) -> None:
        if not columns or len({column.name for column in columns}) != len(columns):
            raise InvalidExportRequest("Export source requires unique, nonempty columns")
        self._collection = collection
        self._columns = tuple(columns)

    @property
    def columns(self) -> tuple[ExportColumn, ...]:
        return self._columns

    async def rows(self, principal: Principal, *, limit: int) -> AsyncIterator[ExportRow]:
        authorize_export(principal)
        if not principal.tenant_id or not principal.user_id:
            raise AuthorizationDenied("Permission denied")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_EXPORT_ROWS
        ):
            raise InvalidExportRequest("Invalid export row limit")
        columns = visible_columns(self, principal)
        if not columns:
            raise AuthorizationDenied("Permission denied")
        query: dict[str, object] = {"tenant_id": principal.tenant_id}
        if "data.read_all" not in principal.permissions:
            query["owner_id"] = principal.user_id
        projection = {column.name: 1 for column in columns}
        projection["_id"] = 0
        cursor: MongoExportCursor | None = None
        try:
            cursor = (
                self._collection.find(query, projection).limit(limit).batch_size(min(limit, 100))
            )
            count = 0
            async for document in cursor:
                # A driver cursor obeys limit, but adapters must remain bounded even
                # if a test double or alternative driver does not honor it.
                yield cast(
                    ExportRow, {column.name: document.get(column.name) for column in columns}
                )
                count += 1
                if count >= limit:
                    break
        except (ExportError, AuthorizationDenied):
            raise
        except Exception:
            raise ExportUnavailable("Export source is unavailable") from None
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    raise ExportUnavailable("Export source is unavailable") from None
