"""One official PyMongo async client per runtime process."""

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError

from src.config.settings import Settings


class MongoResource:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncMongoClient[dict[str, Any]] | None = None

    @property
    def database(self) -> AsyncDatabase[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("MongoDB has not been started")
        return self._client[self._settings.mongo_database]

    async def start(self) -> None:
        if self._client is not None:
            return
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            self._settings.mongo_uri.get_secret_value(),
            maxPoolSize=self._settings.mongo_pool_size,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
            tz_aware=True,
            appname="modular-fastapi-backend",
        )
        try:
            await client.admin.command("ping")
        except BaseException:
            await client.close()
            raise
        self._client = client

    async def is_ready(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
        except PyMongoError:
            return False
        return True

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()
