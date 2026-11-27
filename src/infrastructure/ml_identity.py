"""Serving verifies signed tokens against the template's current Mongo sessions/users."""

import asyncio
import hmac
from datetime import datetime, timezone

import jwt

from src.domain.ml import Actor, Forbidden, MLError, TransientDependency, Unavailable


class AuthenticationRequired(MLError):
    code, status = "UNAUTHENTICATED", 401


class MongoActiveActorResolver:
    """Resolve current account grants for an already admitted background job."""

    def __init__(self, uri: str, database: str) -> None:
        from pymongo import MongoClient
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                                  socketTimeoutMS=5000, tz_aware=True, maxPoolSize=10)
        self.db = self.client[database]

    def _active_user(self, tenant_id: str, user_id: str):
        from pymongo.errors import ConnectionFailure, PyMongoError
        try:
            return self.db.identity_users.find_one(
                {"_id": user_id, "tenant_id": tenant_id, "active": True},
                {"permissions": 1},
            )
        except ConnectionFailure:
            raise TransientDependency("Identity storage is unavailable") from None
        except PyMongoError:
            raise Unavailable("Identity storage is unavailable") from None

    def resolve_active(self, tenant_id: str, user_id: str) -> Actor:
        user = self._active_user(tenant_id, user_id)
        if user is None:
            raise Forbidden("DatasetBuilder principal is unavailable")
        return Actor.from_identity(tenant_id, user_id, frozenset(user["permissions"]))

    def close(self) -> None:
        self.client.close()


class MongoActorResolver(MongoActiveActorResolver):
    def __init__(self, uri: str, database: str, secret: str, issuer: str, audience: str) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("JWT secret must contain at least 32 bytes")
        super().__init__(uri, database)
        self.secret, self.issuer, self.audience = secret, issuer, audience
        self.capacity = asyncio.Semaphore(10)

    async def resolve(self, token: str) -> Actor:
        # Keep admission until the blocking physical lookup is complete.
        async with self.capacity:
            task = asyncio.create_task(asyncio.to_thread(self._resolve, token))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _resolve(self, token: str) -> Actor:
        from pymongo.errors import PyMongoError
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=self.issuer,
                                audience=self.audience, options={"require": ["sub", "tenant", "session", "jti", "exp", "iat"]})
            if any(not isinstance(claims[key], str) or not claims[key] for key in ("sub", "tenant", "session", "jti")):
                raise AuthenticationRequired("Invalid credentials")
            session = self.db.identity_sessions.find_one({"_id": claims["session"],
                                                          "tenant_id": claims["tenant"], "user_id": claims["sub"], "revoked": False})
            user = self._active_user(claims["tenant"], claims["sub"])
            if not session or not user or session["expires_at"] <= datetime.now(timezone.utc) or (
                not hmac.compare_digest(session["jti"], claims["jti"])
            ):
                raise AuthenticationRequired("Invalid credentials")
            return Actor.from_identity(claims["tenant"], claims["sub"], frozenset(user["permissions"]))
        except jwt.InvalidTokenError:
            raise AuthenticationRequired("Invalid credentials") from None
        except PyMongoError:
            raise Unavailable("Identity storage is unavailable") from None
