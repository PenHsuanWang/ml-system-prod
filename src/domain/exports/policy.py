from src.domain.exports.models import ExportColumn
from src.domain.exports.ports import ExportSource
from src.domain.identity.errors import AuthorizationDenied
from src.domain.identity.models import Principal


def authorize_export(principal: Principal) -> None:
    if not {"data.read", "data.export"}.issubset(principal.permissions):
        raise AuthorizationDenied("Permission denied")


def visible_columns(source: ExportSource, principal: Principal) -> tuple[ExportColumn, ...]:
    return tuple(
        column
        for column in source.columns
        if column.permission is None or column.permission in principal.permissions
    )
