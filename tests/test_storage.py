from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymongo.errors import AutoReconnect, DuplicateKeyError

from src.config.settings import Settings
from src.domain.identity.errors import IdentityUnavailable, UserAlreadyExists
from src.infrastructure.persistence.identity import MongoIdentityRepository
from src.infrastructure.persistence.mongo import MongoResource


def settings():
    return Settings(_env_file=None, environment="test", mongo_pool_size=7)


async def test_mongo_resource_is_lazy_process_scoped_and_closes_idempotently(monkeypatch):
    database = object()
    client = Mock()
    client.admin = SimpleNamespace(command=AsyncMock())
    client.close = AsyncMock()
    client.__getitem__ = Mock(return_value=database)
    factory = Mock(return_value=client)
    monkeypatch.setattr("src.infrastructure.persistence.mongo.AsyncMongoClient", factory)
    mongo = MongoResource(settings())
    assert not await mongo.is_ready()
    with pytest.raises(RuntimeError):
        _ = mongo.database
    factory.assert_not_called()
    await mongo.start()
    await mongo.start()
    factory.assert_called_once()
    assert factory.call_args.kwargs["maxPoolSize"] == 7
    assert factory.call_args.kwargs["tz_aware"] is True
    assert mongo.database is database
    assert await mongo.is_ready()
    client.admin.command.side_effect = AutoReconnect("private connection details")
    assert not await mongo.is_ready()
    await mongo.aclose()
    await mongo.aclose()
    client.close.assert_awaited_once()
    assert not await mongo.is_ready()


async def test_mongo_startup_failure_closes_partial_client(monkeypatch):
    client = SimpleNamespace(
        admin=SimpleNamespace(command=AsyncMock(side_effect=AutoReconnect("failed"))),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        "src.infrastructure.persistence.mongo.AsyncMongoClient", Mock(return_value=client)
    )
    mongo = MongoResource(settings())
    with pytest.raises(AutoReconnect):
        await mongo.start()
    client.close.assert_awaited_once()
    assert not await mongo.is_ready()


def repository():
    users, sessions, attempts = AsyncMock(), AsyncMock(), AsyncMock()
    return (
        MongoIdentityRepository(
            {
                "identity_users": users,
                "identity_sessions": sessions,
                "identity_login_attempts": attempts,
            }
        ),
        users,
        sessions,
        attempts,
    )


async def test_identity_indexes_enforce_unique_tenant_username_and_expiring_sessions():
    repo, users, sessions, attempts = repository()
    await repo.ensure_indexes()
    users.create_index.assert_awaited_once_with([("tenant_id", 1), ("username", 1)], unique=True)
    assert any(
        call.kwargs.get("expireAfterSeconds") == 0 for call in sessions.create_index.call_args_list
    )
    attempts.create_index.assert_awaited_once_with("expires_at", expireAfterSeconds=0)


async def test_identity_repository_always_scopes_user_and_session_queries_to_tenant():
    repo, users, sessions, _ = repository()
    users.find_one.return_value = None
    users.find_one_and_update.return_value = None
    sessions.find_one.return_value = None
    assert await repo.get_user("tenant_a", "user_id") is None
    users.find_one.assert_awaited_once_with({"tenant_id": "tenant_a", "_id": "user_id"})
    assert await repo.update_user("tenant_a", "user_id", active=False, permissions=None) is None
    assert users.find_one_and_update.call_args.args[0] == {
        "tenant_id": "tenant_a",
        "_id": "user_id",
    }
    assert await repo.get_session("tenant_a", "session_id") is None
    sessions.find_one.assert_awaited_once_with({"tenant_id": "tenant_a", "_id": "session_id"})
    await repo.revoke_session("tenant_a", "session_id")
    assert sessions.update_one.call_args.args[0] == {"tenant_id": "tenant_a", "_id": "session_id"}


async def test_database_failures_are_sanitized_and_duplicate_users_are_explicit():
    from datetime import UTC, datetime

    from src.domain.identity.models import User

    repo, users, _, _ = repository()
    users.find_one.side_effect = AutoReconnect("mongodb://private:password@host")
    with pytest.raises(IdentityUnavailable, match="Identity storage is unavailable") as error:
        await repo.get_user("tenant", "user")
    assert "password" not in str(error.value)
    users.insert_one.side_effect = DuplicateKeyError("duplicate")
    user = User("id", "tenant", "username", "hash", frozenset(), True, datetime.now(UTC))
    with pytest.raises(UserAlreadyExists):
        await repo.create_user(user)
