import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError, PyMongoError

from src.domain.identity.errors import IdentityUnavailable, UserAlreadyExists
from src.domain.identity.models import Session, User


@contextmanager
def storage_errors() -> Iterator[None]:
    try:
        yield
    except PyMongoError:
        raise IdentityUnavailable("Identity storage is unavailable") from None


def user_from_document(document: dict[str, Any]) -> User:
    return User(
        user_id=document["_id"],
        tenant_id=document["tenant_id"],
        username=document["username"],
        password_hash=document["password_hash"],
        permissions=frozenset(document["permissions"]),
        active=document["active"],
        created_at=document["created_at"],
    )


class MongoIdentityRepository:
    def __init__(self, database: AsyncDatabase[dict[str, Any]]) -> None:
        self._users = database["identity_users"]
        self._sessions = database["identity_sessions"]
        self._attempts = database["identity_login_attempts"]

    async def ensure_indexes(self) -> None:
        with storage_errors():
            await self._users.create_index(
                [("tenant_id", ASCENDING), ("username", ASCENDING)], unique=True
            )
            await self._sessions.create_index("expires_at", expireAfterSeconds=0)
            await self._sessions.create_index([("tenant_id", ASCENDING), ("user_id", ASCENDING)])
            await self._attempts.create_index("expires_at", expireAfterSeconds=0)

    async def create_user(self, user: User) -> None:
        try:
            with storage_errors():
                try:
                    await self._users.insert_one(
                        {
                            "_id": user.user_id,
                            "tenant_id": user.tenant_id,
                            "username": user.username,
                            "password_hash": user.password_hash,
                            "permissions": sorted(user.permissions),
                            "active": user.active,
                            "created_at": user.created_at,
                        }
                    )
                except DuplicateKeyError:
                    raise UserAlreadyExists("Username already exists in this tenant") from None
        except UserAlreadyExists:
            raise

    async def find_username(self, tenant_id: str, username: str) -> User | None:
        with storage_errors():
            document = await self._users.find_one({"tenant_id": tenant_id, "username": username})
        return user_from_document(document) if document is not None else None

    async def get_user(self, tenant_id: str, user_id: str) -> User | None:
        with storage_errors():
            document = await self._users.find_one({"tenant_id": tenant_id, "_id": user_id})
        return user_from_document(document) if document is not None else None

    async def list_users(self, tenant_id: str, limit: int) -> list[User]:
        with storage_errors():
            return [
                user_from_document(document)
                async for document in self._users.find({"tenant_id": tenant_id})
                .sort("username", ASCENDING)
                .limit(limit)
            ]

    async def update_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        active: bool | None,
        permissions: frozenset[str] | None,
    ) -> User | None:
        changes: dict[str, Any] = {}
        if active is not None:
            changes["active"] = active
        if permissions is not None:
            changes["permissions"] = sorted(permissions)
        with storage_errors():
            document = await self._users.find_one_and_update(
                {"tenant_id": tenant_id, "_id": user_id},
                {"$set": changes},
                return_document=ReturnDocument.AFTER,
            )
        return user_from_document(document) if document is not None else None

    async def create_session(self, session: Session) -> None:
        with storage_errors():
            await self._sessions.insert_one(
                {
                    "_id": session.session_id,
                    "tenant_id": session.tenant_id,
                    "user_id": session.user_id,
                    "jti": session.jti,
                    "expires_at": session.expires_at,
                    "revoked": session.revoked,
                }
            )

    async def get_session(self, tenant_id: str, session_id: str) -> Session | None:
        with storage_errors():
            document = await self._sessions.find_one({"tenant_id": tenant_id, "_id": session_id})
        if document is None:
            return None
        return Session(
            document["_id"],
            document["user_id"],
            document["tenant_id"],
            document["jti"],
            document["expires_at"],
            document["revoked"],
        )

    async def revoke_session(self, tenant_id: str, session_id: str) -> None:
        with storage_errors():
            await self._sessions.update_one(
                {"tenant_id": tenant_id, "_id": session_id}, {"$set": {"revoked": True}}
            )

    @staticmethod
    def _attempt_key(tenant_id: str, username: str) -> str:
        return hashlib.sha256(f"{tenant_id}\x00{username}".encode()).hexdigest()

    async def consume_login_attempt(self, tenant_id: str, username: str, now: datetime) -> bool:
        """A MongoDB atomic five-attempt / five-minute window shared by all workers."""
        key = self._attempt_key(tenant_id, username)
        expired = {"$lte": [{"$ifNull": ["$expires_at", now]}, now]}
        pipeline = [
            {
                "$set": {
                    "tenant_id": tenant_id,
                    "count": {"$cond": [expired, 1, {"$add": [{"$ifNull": ["$count", 0]}, 1]}]},
                    "expires_at": {"$cond": [expired, now + timedelta(minutes=5), "$expires_at"]},
                }
            }
        ]
        with storage_errors():
            try:
                document = await self._attempts.find_one_and_update(
                    {"_id": key, "tenant_id": tenant_id},
                    pipeline,
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                # Concurrent first attempts may race on the implicit unique _id.
                document = await self._attempts.find_one_and_update(
                    {"_id": key, "tenant_id": tenant_id},
                    pipeline,
                    return_document=ReturnDocument.AFTER,
                )
        return document is not None and int(document["count"]) <= 5

    async def clear_login_attempts(self, tenant_id: str, username: str) -> None:
        with storage_errors():
            await self._attempts.delete_one(
                {"_id": self._attempt_key(tenant_id, username), "tenant_id": tenant_id}
            )
