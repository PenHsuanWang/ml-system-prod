from src.domain.identity.errors import AuthorizationDenied
from src.domain.identity.models import Principal


def require_permission(principal: Principal, permission: str) -> None:
    if permission not in principal.permissions:
        raise AuthorizationDenied("Permission denied")
