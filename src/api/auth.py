from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.dependencies import get_runtime
from src.application.identity.facade import IdentityServices as IdentityServices
from src.domain.identity.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    IdentityError,
    IdentityUnavailable,
    InvalidIdentityInput,
    UserAlreadyExists,
    UserNotFound,
)
from src.domain.identity.models import Principal


def identity_http_error(error: IdentityError) -> HTTPException:
    if isinstance(error, AuthenticationFailed):
        return HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    if isinstance(error, AuthorizationDenied):
        return HTTPException(403, str(error))
    if isinstance(error, UserNotFound):
        return HTTPException(404, str(error))
    if isinstance(error, UserAlreadyExists):
        return HTTPException(409, str(error))
    if isinstance(error, IdentityUnavailable):
        return HTTPException(503, "Identity storage is unavailable")
    if isinstance(error, InvalidIdentityInput):
        return HTTPException(400, str(error))
    return HTTPException(500, "Identity operation failed")


def get_identity_services(request: Request) -> IdentityServices:
    services = get_runtime(request).modules.get("identity")
    if not isinstance(services, IdentityServices):
        raise HTTPException(503, "Identity services are unavailable")
    return services


bearer = HTTPBearer(auto_error=False)


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    services = get_identity_services(request)
    try:
        principal = await services.authenticate(credentials.credentials)
    except IdentityError as error:
        raise identity_http_error(error) from None
    request.state.principal = principal
    request.state.access_token = credentials.credentials
    return principal


PrincipalDependency = Annotated[Principal, Depends(current_principal)]
IdentityDependency = Annotated[IdentityServices, Depends(get_identity_services)]
