from dataclasses import dataclass
from datetime import datetime

ADMIN_PERMISSIONS = frozenset(
    {
        "identity.users.create",
        "identity.users.read",
        "identity.users.manage",
        "data.read",
        "data.read_all",
        "data.export",
        "kernel.read",
        "kernel.execute",
        "jobs.read",
        "jobs.read_all",
    }
)
MEMBER_PERMISSIONS = frozenset({"data.read", "kernel.read"})


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    tenant_id: str
    username: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    tenant_id: str
    username: str
    password_hash: str
    permissions: frozenset[str]
    active: bool
    created_at: datetime

    def principal(self) -> Principal:
        return Principal(self.user_id, self.tenant_id, self.username, self.permissions)


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    user_id: str
    tenant_id: str
    jti: str
    expires_at: datetime
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: str
    tenant_id: str
    session_id: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    expires_in: int
