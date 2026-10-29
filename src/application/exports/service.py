import csv
import io
import json
import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.domain.exports.errors import (
    ExportError,
    ExportRowTooLarge,
    ExportSourceNotFound,
    ExportUnavailable,
    InvalidExportRequest,
)
from src.domain.exports.models import (
    MAX_EXPORT_ROWS,
    ExportColumn,
    ExportDescriptor,
    ExportRequest,
    ExportRow,
    JSONValue,
    PreparedExport,
    is_protected_field,
    validate_source_name,
)
from src.domain.exports.policy import authorize_export as authorize_export
from src.domain.exports.policy import visible_columns as visible_columns
from src.domain.exports.ports import ExportSource
from src.domain.identity.errors import AuthorizationDenied
from src.domain.identity.models import Principal
from src.utils.formatting import csv_cell as _csv_cell


@dataclass
class _Budget:
    remaining: int

    def consume(self, size: int) -> None:
        self.remaining -= size
        if self.remaining < 0:
            raise ExportRowTooLarge("Export row exceeds the configured size limit")


def _safe_value(value: object, budget: _Budget, depth: int = 0) -> JSONValue:
    if depth > 32:
        raise ExportUnavailable("Export row contains unsupported data")
    budget.consume(1)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > budget.remaining:
            raise ExportRowTooLarge("Export row exceeds the configured size limit")
        budget.consume(len(value.encode("utf-8")))
        return value
    if isinstance(value, int):
        # Bound huge integers before serialization without converting them to a string.
        budget.consume(max(1, value.bit_length() // 3))
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExportUnavailable("Export row contains unsupported data")
        budget.consume(24)
        return value
    if isinstance(value, list):
        if len(value) > budget.remaining:
            raise ExportRowTooLarge("Export row exceeds the configured size limit")
        return [_safe_value(item, budget, depth + 1) for item in value]
    if isinstance(value, Mapping):
        clean: dict[str, JSONValue] = {}
        if len(value) > budget.remaining:
            raise ExportRowTooLarge("Export row exceeds the configured size limit")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExportUnavailable("Export row contains unsupported data")
            if not is_protected_field(key):
                _safe_value(key, budget, depth + 1)
                clean[key] = _safe_value(item, budget, depth + 1)
        return clean
    raise ExportUnavailable("Export row contains unsupported data")


async def _close(iterator: AsyncIterator[ExportRow]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:
            raise ExportUnavailable("Export source is unavailable") from None


class ExportService:
    def __init__(
        self,
        sources: Mapping[str, ExportSource],
        max_rows: int = MAX_EXPORT_ROWS,
        max_row_bytes: int = 1024 * 1024,
    ) -> None:
        if (
            not isinstance(max_rows, int)
            or isinstance(max_rows, bool)
            or not 1 <= max_rows <= MAX_EXPORT_ROWS
        ):
            raise InvalidExportRequest("Invalid configured export row limit")
        if (
            not isinstance(max_row_bytes, int)
            or isinstance(max_row_bytes, bool)
            or max_row_bytes < 1
        ):
            raise InvalidExportRequest("Invalid configured export row size")
        for name, source in sources.items():
            validate_source_name(name)
            columns = tuple(source.columns)
            if not columns or len({column.name for column in columns}) != len(columns):
                raise InvalidExportRequest("Export source requires unique, nonempty columns")
        self._sources = MappingProxyType(dict(sources))
        self._max_rows = max_rows
        self._max_row_bytes = max_row_bytes

    def list_sources(self, principal: Principal) -> tuple[ExportDescriptor, ...]:
        authorize_export(principal)
        return tuple(
            ExportDescriptor(name, columns)
            for name, source in sorted(self._sources.items())
            if (columns := visible_columns(source, principal))
        )

    def _encode_row(
        self, row: ExportRow, columns: tuple[ExportColumn, ...], request: ExportRequest
    ) -> bytes:
        budget = _Budget(self._max_row_bytes)
        clean = {column.name: _safe_value(row.get(column.name), budget) for column in columns}
        try:
            if request.format == "jsonl":
                text = (
                    json.dumps(clean, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    + "\n"
                )
            else:
                output = io.StringIO(newline="")
                csv.writer(output).writerow([_csv_cell(clean[column.name]) for column in columns])
                text = output.getvalue()
            if len(text) > self._max_row_bytes:
                raise ExportRowTooLarge("Export row exceeds the configured size limit")
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_row_bytes:
                raise ExportRowTooLarge("Export row exceeds the configured size limit")
            return encoded
        except ExportError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise ExportUnavailable("Export row contains unsupported data") from None

    async def prepare(self, request: ExportRequest, principal: Principal) -> PreparedExport:
        """Authorize and prefetch one row so initial source failures precede HTTP headers.

        Streaming still reads at most one row at a time. A failure after headers
        terminates the stream; clients must treat interrupted downloads as incomplete.
        """
        authorize_export(principal)
        if request.limit > self._max_rows:
            raise InvalidExportRequest("Export limit exceeds the configured maximum")
        source = self._sources.get(request.source_name)
        if source is None:
            raise ExportSourceNotFound("Export source does not exist")
        columns = visible_columns(source, principal)
        if not columns:
            raise AuthorizationDenied("Permission denied")
        try:
            iterator = source.rows(principal, limit=request.limit)
        except (ExportError, AuthorizationDenied):
            raise
        except Exception:
            raise ExportUnavailable("Export source is unavailable") from None
        first: bytes | None = None
        try:
            row = await anext(iterator, None)
            if row is not None:
                first = self._encode_row(row, columns, request)
        except BaseException as exc:
            await _close(iterator)
            if not isinstance(exc, Exception) or isinstance(exc, ExportError | AuthorizationDenied):
                raise
            raise ExportUnavailable("Export source is unavailable") from None

        header = b""
        if request.format == "csv":
            output = io.StringIO(newline="")
            csv.writer(output).writerow([column.name for column in columns])
            header = output.getvalue().encode("utf-8")
            if len(header) > self._max_row_bytes:
                await _close(iterator)
                raise ExportRowTooLarge("Export header exceeds the configured size limit")

        async def stream() -> AsyncIterator[bytes]:
            try:
                if header:
                    yield header
                if first is not None:
                    yield first
                    count = 1
                    while count < request.limit:
                        row = await anext(iterator, None)
                        if row is None:
                            break
                        yield self._encode_row(row, columns, request)
                        count += 1
            except ExportError:
                raise
            except Exception:
                raise ExportUnavailable("Export source is unavailable") from None
            finally:
                await _close(iterator)

        return PreparedExport(
            columns,
            request.format,
            f"{request.source_name}.{request.format}",
            "text/csv" if request.format == "csv" else "application/x-ndjson",
            stream(),
        )
