import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.application.identity.policy import require_permission
from src.domain.identity.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    InvalidIdentityInput,
    UserNotFound,
)
from src.domain.identity.models import (
    ADMIN_PERMISSIONS,
    MEMBER_PERMISSIONS,
    AccessClaims,
    LoginResult,
    Principal,
    Session,
    User,
)
from src.domain.identity.ports import IdentityRepository, PasswordHasher, TokenCodec

_TENANT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_USERNAME = re.compile(r"^[a-zA-Z0-9_.@-]{3,64}$")
_PERMISSION = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def utcnow() -> datetime:
    return datetime.now(UTC)


class IdentityService:
    """Tenant-scoped use cases; access claims identify sessions, never grant access."""

    def __init__(
        self,
        repository: IdentityRepository,
        passwords: PasswordHasher,
        tokens: TokenCodec,
        *,
        access_token_seconds: int,
        dummy_password_hash: str,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repository = repository
        self._passwords = passwords
        self._tokens = tokens
        self._ttl = access_token_seconds
        self._dummy_hash = dummy_password_hash
        self._clock = clock

    @staticmethod
    def _validate_name(tenant_id: str, username: str) -> str:
        if not _TENANT.fullmatch(tenant_id) or not _USERNAME.fullmatch(username):
            raise InvalidIdentityInput("Invalid tenant or username")
        return username.casefold()

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 12 <= len(password) <= 128:
            raise InvalidIdentityInput("Password must contain between 12 and 128 characters")

    @staticmethod
    def _validate_permissions(permissions: frozenset[str]) -> None:
        if len(permissions) > 100 or any(not _PERMISSION.fullmatch(p) for p in permissions):
            raise InvalidIdentityInput("Invalid permission names")

    async def bootstrap_admin(
        self,
        tenant_id: str,
        username: str,
        password: str,
        *,
        additional_permissions: frozenset[str] = frozenset(),
    ) -> User:
        """Operator-only CLI operation; never exposed through an HTTP endpoint."""
        return await self._create_user(
            tenant_id, username, password, ADMIN_PERMISSIONS | additional_permissions
        )

    async def _create_user(
        self, tenant_id: str, username: str, password: str, permissions: frozenset[str]
    ) -> User:
        username = self._validate_name(tenant_id, username)
        self._validate_password(password)
        self._validate_permissions(permissions)
        user = User(
            user_id=uuid4().hex,
            tenant_id=tenant_id,
            username=username,
            password_hash=await self._passwords.hash(password),
            permissions=permissions,
            active=True,
            created_at=self._clock(),
        )
        await self._repository.create_user(user)
        return user

    async def register(
        self,
        principal: Principal,
        username: str,
        password: str,
        permissions: frozenset[str] | None = None,
    ) -> User:
        require_permission(principal, "identity.users.create")
        granted = MEMBER_PERMISSIONS if permissions is None else permissions
        if not granted.issubset(principal.permissions):
            raise AuthorizationDenied("Cannot grant permissions you do not hold")
        return await self._create_user(principal.tenant_id, username, password, granted)

    async def login(self, tenant_id: str, username: str, password: str) -> LoginResult:
        try:
            username = self._validate_name(tenant_id, username)
        except InvalidIdentityInput:
            raise AuthenticationFailed("Invalid credentials") from None
        if not 1 <= len(password) <= 128:
            raise AuthenticationFailed("Invalid credentials")
        now = self._clock().replace(microsecond=0)
        if not await self._repository.consume_login_attempt(tenant_id, username, now):
            raise AuthenticationFailed("Invalid credentials")
        user = await self._repository.find_username(tenant_id, username)
        password_hash = user.password_hash if user is not None else self._dummy_hash
        valid = await self._passwords.verify(password, password_hash)
        if user is None or not valid or not user.active:
            raise AuthenticationFailed("Invalid credentials")
        claims = AccessClaims(
            user.user_id, tenant_id, uuid4().hex, uuid4().hex, now + timedelta(seconds=self._ttl)
        )
        token = self._tokens.encode(claims, now)
        await self._repository.create_session(
            Session(claims.session_id, user.user_id, tenant_id, claims.jti, claims.expires_at)
        )
        await self._repository.clear_login_attempts(tenant_id, username)
        return LoginResult(token, self._ttl)

    async def authenticate(self, access_token: str) -> Principal:
        claims = self._tokens.decode(access_token)
        now = self._clock()
        session = await self._repository.get_session(claims.tenant_id, claims.session_id)
        if (
            session is None
            or session.revoked
            or session.expires_at <= now
            or claims.expires_at <= now
            or session.user_id != claims.user_id
            or session.tenant_id != claims.tenant_id
            or session.jti != claims.jti
        ):
            raise AuthenticationFailed("Invalid credentials")
        return await self.resolve_principal(claims.tenant_id, claims.user_id)

    async def resolve_principal(self, tenant_id: str, user_id: str) -> Principal:
        """Re-read active account and current permissions for background execution."""
        user = await self._repository.get_user(tenant_id, user_id)
        if user is None or not user.active:
            raise AuthenticationFailed("Invalid credentials")
        return user.principal()

    async def logout(self, principal: Principal, access_token: str) -> None:
        claims = self._tokens.decode(access_token)
        if claims.user_id != principal.user_id or claims.tenant_id != principal.tenant_id:
            raise AuthenticationFailed("Invalid credentials")
        await self._repository.revoke_session(claims.tenant_id, claims.session_id)

    async def list_users(self, principal: Principal, limit: int = 100) -> list[User]:
        require_permission(principal, "identity.users.read")
        if not 1 <= limit <= 100:
            raise InvalidIdentityInput("Limit must be between 1 and 100")
        return await self._repository.list_users(principal.tenant_id, limit)

    async def update_user(
        self,
        principal: Principal,
        user_id: str,
        *,
        active: bool | None = None,
        permissions: frozenset[str] | None = None,
    ) -> User:
        require_permission(principal, "identity.users.manage")
        if principal.user_id == user_id:
            raise AuthorizationDenied("Administrators cannot change their own account")
        if active is None and permissions is None:
            raise InvalidIdentityInput("Provide active or permissions")
        if permissions is not None:
            self._validate_permissions(permissions)
            if not permissions.issubset(principal.permissions):
                raise AuthorizationDenied("Cannot grant permissions you do not hold")
        user = await self._repository.update_user(
            principal.tenant_id, user_id, active=active, permissions=permissions
        )
        if user is None:
            raise UserNotFound("User not found")
        return user
