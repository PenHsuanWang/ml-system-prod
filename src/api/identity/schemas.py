from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from src.domain.identity.models import Principal, User


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictRequest):
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: SecretStr = Field(min_length=1, max_length=128)


class RegisterRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: SecretStr = Field(min_length=12, max_length=128)
    permissions: set[str] | None = Field(default=None, max_length=100)


class UpdateUserRequest(StrictRequest):
    active: bool | None = Field(default=None, strict=True)
    permissions: set[str] | None = Field(default=None, max_length=100)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class PrincipalResponse(BaseModel):
    user_id: str
    tenant_id: str
    username: str
    permissions: list[str]

    @classmethod
    def from_principal(cls, principal: Principal) -> "PrincipalResponse":
        return cls(
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            username=principal.username,
            permissions=sorted(principal.permissions),
        )


class UserResponse(PrincipalResponse):
    active: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            username=user.username,
            permissions=sorted(user.permissions),
            active=user.active,
            created_at=user.created_at,
        )
