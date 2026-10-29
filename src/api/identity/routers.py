from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status

from src.api.auth import (
    IdentityDependency,
    PrincipalDependency,
    identity_http_error,
)
from src.api.identity.schemas import (
    LoginRequest,
    PrincipalResponse,
    RegisterRequest,
    TokenResponse,
    UpdateUserRequest,
    UserResponse,
)
from src.domain.identity.errors import IdentityError

router = APIRouter(tags=["identity"])


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, response: Response, services: IdentityDependency
) -> TokenResponse:
    try:
        result = await services.service.login(
            body.tenant_id, body.username, body.password.get_secret_value()
        )
    except IdentityError as error:
        raise identity_http_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(access_token=result.access_token, expires_in=result.expires_in)


@router.get("/me", response_model=PrincipalResponse)
async def me(principal: PrincipalDependency) -> PrincipalResponse:
    return PrincipalResponse.from_principal(principal)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, principal: PrincipalDependency, services: IdentityDependency
) -> Response:
    try:
        await services.service.logout(principal, request.state.access_token)
    except IdentityError as error:
        raise identity_http_error(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, principal: PrincipalDependency, services: IdentityDependency
) -> UserResponse:
    try:
        user = await services.service.register(
            principal,
            body.username,
            body.password.get_secret_value(),
            frozenset(body.permissions) if body.permissions is not None else None,
        )
    except IdentityError as error:
        raise identity_http_error(error) from None
    return UserResponse.from_user(user)


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    principal: PrincipalDependency,
    services: IdentityDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[UserResponse]:
    try:
        users = await services.service.list_users(principal, limit)
    except IdentityError as error:
        raise identity_http_error(error) from None
    return [UserResponse.from_user(user) for user in users]


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")],
    body: UpdateUserRequest,
    principal: PrincipalDependency,
    services: IdentityDependency,
) -> UserResponse:
    try:
        user = await services.service.update_user(
            principal,
            user_id,
            active=body.active,
            permissions=frozenset(body.permissions) if body.permissions is not None else None,
        )
    except IdentityError as error:
        raise identity_http_error(error) from None
    return UserResponse.from_user(user)
