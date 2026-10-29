import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from src.domain.identity.errors import AuthenticationFailed
from src.domain.identity.models import AccessClaims
from src.domain.identity.ports import ThreadRunner

_HEX_ID = re.compile(r"^[a-f0-9]{32}$")
T = TypeVar("T")


class Argon2PasswordHasher:
    def __init__(self, runner: ThreadRunner, concurrency: int) -> None:
        self._runner = runner
        self._semaphore = asyncio.Semaphore(concurrency)
        self._hasher = PasswordHash.recommended()

    async def _run(self, function: Callable[..., T], /, *args: object) -> T:
        await self._semaphore.acquire()
        task = asyncio.create_task(self._runner.run(function, *args))

        def complete(done: asyncio.Task[T]) -> None:
            self._semaphore.release()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(complete)
        # Cancellation never frees password capacity before Argon2 actually ends.
        return await asyncio.shield(task)

    async def hash(self, password: str) -> str:
        return await self._run(self._hasher.hash, password)

    async def verify(self, password: str, password_hash: str) -> bool:
        try:
            return await self._run(self._hasher.verify, password, password_hash)
        except (UnknownHashError, ValueError):
            return False


class JwtTokenCodec:
    """Fixed HS256 and mandatory claims; authorization is read from the repository."""

    def __init__(self, secret: str, issuer: str, audience: str) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes")
        self._secret = secret
        self._issuer = issuer
        self._audience = audience

    def encode(self, claims: AccessClaims, issued_at: datetime) -> str:
        return jwt.encode(
            {
                "iss": self._issuer,
                "aud": self._audience,
                "sub": claims.user_id,
                "tenant": claims.tenant_id,
                "session": claims.session_id,
                "jti": claims.jti,
                "iat": issued_at,
                "exp": claims.expires_at,
            },
            self._secret,
            algorithm="HS256",
        )

    def decode(self, token: str) -> AccessClaims:
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["iss", "aud", "sub", "tenant", "session", "jti", "iat", "exp"],
                    "strict_aud": True,
                },
            )
            for field in ("sub", "session", "jti"):
                if not isinstance(payload[field], str) or not _HEX_ID.fullmatch(payload[field]):
                    raise ValueError("Invalid identifier")
            if not isinstance(payload["tenant"], str) or not payload["tenant"]:
                raise ValueError("Invalid tenant")
            for field in ("exp", "iat"):
                if not isinstance(payload[field], int) or isinstance(payload[field], bool):
                    raise ValueError("Invalid timestamp")
            return AccessClaims(
                payload["sub"],
                payload["tenant"],
                payload["session"],
                payload["jti"],
                datetime.fromtimestamp(payload["exp"], UTC),
            )
        except (jwt.PyJWTError, KeyError, ValueError, TypeError, OverflowError):
            raise AuthenticationFailed("Invalid credentials") from None
