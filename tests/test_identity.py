import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

import server
from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from src.api.auth import IdentityServices
from src.api.identity.routers import router
from src.application.identity.service import IdentityService
from src.config.settings import Settings
from src.domain.identity.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    InvalidIdentityInput,
    UserAlreadyExists,
    UserNotFound,
)
from src.domain.identity.models import (
    ADMIN_PERMISSIONS,
    MEMBER_PERMISSIONS,
    AccessClaims,
)
from src.infrastructure.security import Argon2PasswordHasher, JwtTokenCodec

SECRET = "test-only-secret-long-enough-for-both-HS256-and-HS384-fixtures"
PASSWORD = "a-test-password-123"


class FakeRepository:
    def __init__(self):
        self.users = {}
        self.sessions = {}
        self.attempts = {}

    async def ensure_indexes(self):
        pass

    async def create_user(self, user):
        if any(
            row.tenant_id == user.tenant_id and row.username == user.username
            for row in self.users.values()
        ):
            raise UserAlreadyExists("Username already exists in this tenant")
        self.users[user.user_id] = user

    async def find_username(self, tenant_id, username):
        return next(
            (u for u in self.users.values() if u.tenant_id == tenant_id and u.username == username),
            None,
        )

    async def get_user(self, tenant_id, user_id):
        user = self.users.get(user_id)
        return user if user is not None and user.tenant_id == tenant_id else None

    async def list_users(self, tenant_id, limit):
        return sorted(
            (u for u in self.users.values() if u.tenant_id == tenant_id), key=lambda u: u.username
        )[:limit]

    async def update_user(self, tenant_id, user_id, *, active, permissions):
        user = await self.get_user(tenant_id, user_id)
        if user is None:
            return None
        user = replace(
            user,
            active=user.active if active is None else active,
            permissions=user.permissions if permissions is None else permissions,
        )
        self.users[user_id] = user
        return user

    async def create_session(self, session):
        self.sessions[session.session_id] = session

    async def get_session(self, tenant_id, session_id):
        session = self.sessions.get(session_id)
        return session if session is not None and session.tenant_id == tenant_id else None

    async def revoke_session(self, tenant_id, session_id):
        session = await self.get_session(tenant_id, session_id)
        if session is not None:
            self.sessions[session_id] = replace(session, revoked=True)

    async def consume_login_attempt(self, tenant_id, username, now):
        key = tenant_id, username
        count, until = self.attempts.get(key, (0, now))
        if until <= now:
            count, until = 0, now + timedelta(minutes=5)
        self.attempts[key] = count + 1, until
        return count + 1 <= 5

    async def clear_login_attempts(self, tenant_id, username):
        self.attempts.pop((tenant_id, username), None)


class FakePasswords:
    def __init__(self):
        self.verified = []

    async def hash(self, password):
        return f"test-hash:{password}"

    async def verify(self, password, password_hash):
        self.verified.append(password_hash)
        return password_hash == await self.hash(password)


@pytest.fixture
def identity():
    repository, passwords = FakeRepository(), FakePasswords()
    codec = JwtTokenCodec(SECRET, "test-issuer", "test-audience")
    service = IdentityService(
        repository,
        passwords,
        codec,
        access_token_seconds=900,
        dummy_password_hash="dummy-test-hash",
    )
    return service, repository, passwords


async def test_provisioning_is_tenant_scoped_and_never_grants_extra_permissions(identity):
    service, repository, _ = identity
    assert repository.users == {}
    admin = await service.bootstrap_admin("tenant_a", "Admin", PASSWORD)
    assert admin.username == "admin"
    assert admin.permissions == ADMIN_PERMISSIONS
    member = await service.register(admin.principal(), "Member", PASSWORD)
    assert member.tenant_id == "tenant_a"
    assert member.permissions == MEMBER_PERMISSIONS
    with pytest.raises(AuthorizationDenied):
        await service.register(member.principal(), "other", PASSWORD)
    with pytest.raises(AuthorizationDenied):
        await service.register(admin.principal(), "other", PASSWORD, frozenset({"system.root"}))
    with pytest.raises(UserAlreadyExists):
        await service.register(admin.principal(), "MEMBER", PASSWORD)
    await service.bootstrap_admin("tenant_b", "Member", PASSWORD)


@pytest.mark.parametrize("password", ["short", "x" * 129])
async def test_password_length_is_enforced_at_service_boundary(identity, password):
    service, _, _ = identity
    with pytest.raises(InvalidIdentityInput):
        await service.bootstrap_admin("tenant", "admin", password)


async def test_unknown_user_and_wrong_password_use_same_failure_and_dummy_verify(identity):
    service, _, passwords = identity
    await service.bootstrap_admin("tenant", "admin", PASSWORD)
    for username in ("missing", "admin"):
        with pytest.raises(AuthenticationFailed, match="Invalid credentials"):
            await service.login("tenant", username, "wrong-password")
    assert passwords.verified == ["dummy-test-hash", f"test-hash:{PASSWORD}"]


async def test_authentication_reloads_session_user_and_current_permissions(identity):
    service, repository, _ = identity
    admin = await service.bootstrap_admin("tenant", "admin", PASSWORD)
    member = await service.register(admin.principal(), "member", PASSWORD)
    login = await service.login("tenant", "member", PASSWORD)
    assert (await service.authenticate(login.access_token)).permissions == MEMBER_PERMISSIONS
    await service.update_user(admin.principal(), member.user_id, permissions=frozenset())
    assert (await service.authenticate(login.access_token)).permissions == frozenset()
    assert (await service.resolve_principal("tenant", member.user_id)).permissions == frozenset()
    await service.update_user(admin.principal(), member.user_id, active=False)
    with pytest.raises(AuthenticationFailed):
        await service.authenticate(login.access_token)
    with pytest.raises(AuthenticationFailed):
        await service.resolve_principal("tenant", member.user_id)
    assert len(repository.sessions) == 1


async def test_logout_revokes_database_session_immediately(identity):
    service, repository, _ = identity
    user = await service.bootstrap_admin("tenant", "admin", PASSWORD)
    login = await service.login("tenant", "admin", PASSWORD)
    await service.logout(user.principal(), login.access_token)
    assert next(iter(repository.sessions.values())).revoked
    with pytest.raises(AuthenticationFailed):
        await service.authenticate(login.access_token)


async def test_admin_cannot_cross_tenant_or_modify_own_account(identity):
    service, _, _ = identity
    a = await service.bootstrap_admin("tenant_a", "admin", PASSWORD)
    b = await service.bootstrap_admin("tenant_b", "admin", PASSWORD)
    assert await service.list_users(a.principal()) == [a]
    with pytest.raises(UserNotFound):
        await service.update_user(a.principal(), b.user_id, active=False)
    with pytest.raises(AuthorizationDenied):
        await service.update_user(a.principal(), a.user_id, permissions=frozenset())
    with pytest.raises(AuthenticationFailed):
        await service.resolve_principal("tenant_a", b.user_id)


async def test_login_attempt_limit_is_enforced_through_repository(identity):
    service, repository, _ = identity
    await service.bootstrap_admin("tenant", "admin", PASSWORD)
    for _ in range(5):
        with pytest.raises(AuthenticationFailed):
            await service.login("tenant", "admin", "wrong-password")
    with pytest.raises(AuthenticationFailed):
        await service.login("tenant", "admin", PASSWORD)
    assert repository.attempts[("tenant", "admin")][0] == 6


@pytest.mark.parametrize("alteration", ["expiry", "jti", "missing"])
async def test_database_session_expiry_and_binding_are_checked(identity, alteration):
    service, repository, _ = identity
    await service.bootstrap_admin("tenant", "admin", PASSWORD)
    login = await service.login("tenant", "admin", PASSWORD)
    key, session = next(iter(repository.sessions.items()))
    if alteration == "missing":
        repository.sessions.clear()
    elif alteration == "expiry":
        repository.sessions[key] = replace(session, expires_at=datetime.now(UTC) - timedelta(1))
    else:
        repository.sessions[key] = replace(session, jti=uuid4().hex)
    with pytest.raises(AuthenticationFailed):
        await service.authenticate(login.access_token)


@pytest.mark.parametrize("alteration", ["issuer", "audience", "expiry", "iat", "missing", "alg"])
def test_jwt_rejects_invalid_claims_and_algorithm(alteration):
    codec = JwtTokenCodec(SECRET, "test-issuer", "test-audience")
    now = datetime.now(UTC)
    claims = AccessClaims(
        uuid4().hex, "tenant", uuid4().hex, uuid4().hex, now + timedelta(minutes=5)
    )
    token = codec.encode(claims, now)
    payload = jwt.decode(token, SECRET, algorithms=["HS256"], audience="test-audience")
    algorithm = "HS256"
    if alteration == "issuer":
        payload["iss"] = "untrusted"
    elif alteration == "audience":
        payload["aud"] = "untrusted"
    elif alteration == "expiry":
        payload["exp"] = int(now.timestamp()) - 1
    elif alteration == "iat":
        payload["iat"] = int(now.timestamp()) + 3600
    elif alteration == "missing":
        del payload["session"]
    else:
        algorithm = "HS384"
    with pytest.raises(AuthenticationFailed):
        codec.decode(jwt.encode(payload, SECRET, algorithm=algorithm))


async def test_actual_argon2_hashes_are_offloaded_and_password_is_not_stored():
    class Runner:
        async def run(self, function, /, *args):
            return await asyncio.to_thread(function, *args)

    hasher = Argon2PasswordHasher(Runner(), concurrency=1)
    password_hash = await hasher.hash(PASSWORD)
    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash
    assert await hasher.verify(PASSWORD, password_hash)
    assert not await hasher.verify("wrong-password", password_hash)
    assert not await hasher.verify(PASSWORD, "unsupported-hash")


async def test_cancelled_password_request_keeps_capacity_until_hashing_really_finishes():
    started, release = asyncio.Event(), asyncio.Event()

    class Runner:
        calls = 0

        async def run(self, function, /, *args):
            self.calls += 1
            started.set()
            await release.wait()
            return "test-hash"

    runner = Runner()
    hasher = Argon2PasswordHasher(runner, concurrency=1)
    first = asyncio.create_task(hasher.hash(PASSWORD))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(hasher.hash(PASSWORD))
    await asyncio.sleep(0)
    assert runner.calls == 1
    release.set()
    assert await second == "test-hash"
    assert runner.calls == 2


async def test_http_identity_contract_register_is_controlled_and_logout_revokes(identity):
    service, _, _ = identity
    await service.bootstrap_admin("tenant_a", "admin", PASSWORD)

    @asynccontextmanager
    async def lifespan(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        yield ModuleExports(services=IdentityServices(service))

    module = ModuleDefinition(
        name="identity", kind="platform", routers=(router,), lifespan=lifespan
    )
    settings = Settings(_env_file=None, environment="test", task_backend="memory")
    app = server.create_app(settings, modules=(module,))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        assert (await client.get("/api/v1/identity/me")).status_code == 401
        assert (
            await client.post(
                "/api/v1/identity/register", json={"username": "member", "password": PASSWORD}
            )
        ).status_code == 401
        login = await client.post(
            "/api/v1/identity/login",
            json={"tenant_id": "tenant_a", "username": "admin", "password": PASSWORD},
        )
        assert login.status_code == 200
        assert login.headers["cache-control"] == "no-store"
        assert login.json()["token_type"] == "bearer"
        assert login.json()["expires_in"] == 900
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = await client.get("/api/v1/identity/me", headers=headers)
        assert me.json()["tenant_id"] == "tenant_a"
        body = {"username": "member", "password": PASSWORD, "tenant_id": "tenant_b"}
        assert (
            await client.post("/api/v1/identity/register", headers=headers, json=body)
        ).status_code == 422
        del body["tenant_id"]
        registered = await client.post("/api/v1/identity/register", headers=headers, json=body)
        assert registered.status_code == 201
        assert registered.json()["tenant_id"] == "tenant_a"
        assert "password_hash" not in registered.text and PASSWORD not in registered.text
        member_login = await client.post(
            "/api/v1/identity/login",
            json={"tenant_id": "tenant_a", "username": "member", "password": PASSWORD},
        )
        member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}
        assert (
            await client.post(
                "/api/v1/identity/register",
                headers=member_headers,
                json={"username": "another", "password": PASSWORD},
            )
        ).status_code == 403
        assert (
            await client.get("/api/v1/identity/users", headers=member_headers)
        ).status_code == 403
        logout = await client.post("/api/v1/identity/logout", headers=headers)
        assert logout.status_code == 204 and logout.content == b""
        assert (await client.get("/api/v1/identity/me", headers=headers)).status_code == 401


def test_manage_password_stdin_is_not_printed_or_received_as_cli_argument(monkeypatch, capsys):
    import io

    import manage

    captured = []

    async def create_admin(tenant_id, username, password):
        captured.append((tenant_id, username, password))

    monkeypatch.setattr(manage, "create_admin", create_admin)
    monkeypatch.setattr(manage.sys, "stdin", io.StringIO(f"{PASSWORD}\n"))
    assert (
        manage.main(
            ["create-admin", "--tenant-id", "tenant", "--username", "admin", "--password-stdin"]
        )
        == 0
    )
    assert captured == [("tenant", "admin", PASSWORD)]
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err


async def test_operator_can_explicitly_provision_module_permissions(identity):
    service, _, _ = identity
    admin = await service.bootstrap_admin(
        "tenant_a", "admin", PASSWORD, additional_permissions=frozenset({"alpha.execute"})
    )
    assert "alpha.execute" in admin.permissions
    member = await service.register(
        admin.principal(), "member", PASSWORD, permissions=frozenset({"alpha.execute"})
    )
    assert member.permissions == frozenset({"alpha.execute"})


def test_manage_accepts_explicit_module_permissions(monkeypatch):
    import io

    import manage

    captured = []

    async def create_admin(tenant_id, username, password, *, additional_permissions):
        captured.append(additional_permissions)

    monkeypatch.setattr(manage, "create_admin", create_admin)
    monkeypatch.setattr(manage.sys, "stdin", io.StringIO(f"{PASSWORD}\n"))
    assert (
        manage.main(
            [
                "create-admin",
                "--tenant-id",
                "tenant",
                "--username",
                "admin",
                "--password-stdin",
                "--permission",
                "alpha.execute",
                "--permission",
                "alpha.read",
            ]
        )
        == 0
    )
    assert captured == [frozenset({"alpha.execute", "alpha.read"})]
