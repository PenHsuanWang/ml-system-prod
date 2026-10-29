from collections.abc import AsyncIterator
from typing import Protocol

from src.domain.exports.models import ExportColumn, ExportRow
from src.domain.identity.models import Principal


class ExportSource(Protocol):
    """A registered dataset adapter, responsible for enforcing row-level access.

    Implementations MUST constrain the backing query by the principal's tenant
    and ownership policy. HTTP callers cannot supply a collection or query.
    Values must be JSON-compatible; adapters convert database-specific types.
    The service independently applies column permissions and size/row limits.
    """

    @property
    def columns(self) -> tuple[ExportColumn, ...]: ...

    def rows(self, principal: Principal, *, limit: int) -> AsyncIterator[ExportRow]: ...
