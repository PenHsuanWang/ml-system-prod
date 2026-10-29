import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from src.api.identity.routers import router
from src.application.identity.facade import IdentityServices
from src.application.identity.service import IdentityService
from src.config.settings import Settings
from src.domain.identity.ports import ThreadRunner
from src.infrastructure.persistence.identity import MongoIdentityRepository
from src.infrastructure.persistence.mongo import MongoResource
from src.infrastructure.security import Argon2PasswordHasher, JwtTokenCodec


async def build_identity_services(
    settings: Settings, mongo: MongoResource, runner: ThreadRunner
) -> IdentityServices:
    secret = settings.jwt_secret
    if secret is None:
        raise ValueError("JWT secret is required when identity is enabled")
    repository = MongoIdentityRepository(mongo.database)
    await repository.ensure_indexes()
    passwords = Argon2PasswordHasher(runner, settings.password_hash_concurrency)
    dummy_hash = await passwords.hash(secrets.token_urlsafe(32))
    service = IdentityService(
        repository,
        passwords,
        JwtTokenCodec(secret.get_secret_value(), settings.jwt_issuer, settings.jwt_audience),
        access_token_seconds=settings.access_token_seconds,
        dummy_password_hash=dummy_hash,
    )
    return IdentityServices(service)


def build_identity_module(settings: Settings) -> ModuleDefinition:
    @asynccontextmanager
    async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        mongo = context.resources.get("mongodb")
        runner = context.resources.get("threads")
        if not isinstance(mongo, MongoResource) or runner is None:
            raise RuntimeError("Identity requires MongoDB and a bounded thread executor")
        services = await build_identity_services(settings, mongo, cast(ThreadRunner, runner))
        yield ModuleExports(services=services)

    return ModuleDefinition(name="identity", routers=(router,), lifespan=lifespan, kind="platform")
