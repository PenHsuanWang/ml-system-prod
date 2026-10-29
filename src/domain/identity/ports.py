from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar

from src.domain.identity.models import AccessClaims, Principal, Session, User

T = TypeVar("T")


class ThreadRunner(Protocol):
    async def run(self, function: Callable[..., T], /, *args: object) -> T: ...


class PasswordHasher(Protocol):
    async def hash(self, password: str) -> str: ...

    async def verify(self, password: str, password_hash: str) -> bool: ...


class TokenCodec(Protocol):
    def encode(self, claims: AccessClaims, issued_at: datetime) -> str: ...

    def decode(self, token: str) -> AccessClaims: ...


class PrincipalAuthenticator(Protocol):
    """An SSO adapter may replace local token verification and directory lookups."""

    async def authenticate(self, access_token: str) -> Principal: ...

    async def resolve_principal(self, tenant_id: str, user_id: str) -> Principal: ...


class IdentityRepository(Protocol):
    async def ensure_indexes(self) -> None: ...

    async def create_user(self, user: User) -> None: ...

    async def find_username(self, tenant_id: str, username: str) -> User | None: ...

    async def get_user(self, tenant_id: str, user_id: str) -> User | None: ...

    async def list_users(self, tenant_id: str, limit: int) -> list[User]: ...

    async def update_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        active: bool | None,
        permissions: frozenset[str] | None,
    ) -> User | None: ...

    async def create_session(self, session: Session) -> None: ...

    async def get_session(self, tenant_id: str, session_id: str) -> Session | None: ...

    async def revoke_session(self, tenant_id: str, session_id: str) -> None: ...

    async def consume_login_attempt(self, tenant_id: str, username: str, now: datetime) -> bool: ...

    async def clear_login_attempts(self, tenant_id: str, username: str) -> None: ...
