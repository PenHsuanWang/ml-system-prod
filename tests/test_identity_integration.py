"""Opt-in-capable local MongoDB integration; every test owns its short-lived server."""

import asyncio
import json
import os
import secrets
import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

import server
from bootstrap.components import ModuleContext, ModuleDefinition, ModuleExports
from bootstrap.identity import build_identity_module
from src.api.auth import IdentityServices
from src.config.settings import Settings
from src.domain.exports.models import ExportColumn
from src.infrastructure.persistence.exports import MongoExportSource
from src.infrastructure.persistence.identity import MongoIdentityRepository
from src.infrastructure.persistence.mongo import MongoResource

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("MONGO_TEST_URI") and shutil.which("mongod") is None,
        reason="MONGO_TEST_URI and mongod are unavailable",
    ),
]
PASSWORD = "integration-only-password-123"


@dataclass(frozen=True)
class MongoTestTarget:
    uri: SecretStr
    database_name: str


@asynccontextmanager
async def isolated_database(uri: str) -> AsyncIterator[MongoTestTarget]:
    target = MongoTestTarget(SecretStr(uri), f"identity_test_{uuid4().hex}")
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
    try:
        await client.admin.command("ping")
        yield target
    finally:
        try:
            # This UUID namespace is the only external database this fixture owns.
            await client.drop_database(target.database_name)
        finally:
            await client.close()


def free_port() -> int:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except PermissionError:
        pytest.skip("Sandbox does not permit MongoDB to bind a loopback TCP socket")


def startup_diagnostics(directory: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace")[-8000:]
        for path in (directory / "mongo.log", directory / "process.log")
        if path.exists()
    )


@pytest.fixture
async def authenticated_mongo_uri(tmp_path: Path) -> AsyncIterator[MongoTestTarget]:
    external_uri = os.getenv("MONGO_TEST_URI")
    if external_uri:
        async with isolated_database(external_uri) as target:
            yield target
        return
    executable = shutil.which("mongod")
    assert executable is not None
    port = free_port()
    data = tmp_path / "db"
    data.mkdir()
    client = None
    with (tmp_path / "process.log").open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--dbpath",
            str(data),
            "--port",
            str(port),
            "--bind_ip",
            "127.0.0.1",
            "--nounixsocket",
            "--auth",
            "--logpath",
            str(tmp_path / "mongo.log"),
            "--wiredTigerCacheSizeGB",
            "0.25",
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            client = AsyncMongoClient(
                f"mongodb://127.0.0.1:{port}/",
                serverSelectionTimeoutMS=200,
                connectTimeoutMS=200,
                tz_aware=True,
            )
            async with asyncio.timeout(20):
                while True:
                    if process.returncode is not None:
                        diagnostic = startup_diagnostics(tmp_path)
                        if any(
                            text in diagnostic
                            for text in ("Operation not permitted", "Permission denied")
                        ) and any(
                            text in diagnostic.lower() for text in ("listener", "bind", "socket")
                        ):
                            pytest.skip("Sandbox denied MongoDB's loopback listener")
                        pytest.fail(f"Isolated mongod exited during startup: {diagnostic}")
                    try:
                        await client.admin.command("ping")
                        break
                    except PyMongoError:
                        await asyncio.sleep(0.05)
            mongo_password = secrets.token_urlsafe(32)
            # Fresh databases allow the first admin only through localhost.
            # The server remains --auth throughout; no shared service is touched.
            await client.admin.command(
                "createUser",
                "integration_root",
                pwd=mongo_password,
                roles=[{"role": "root", "db": "admin"}],
            )
            await client.close()
            client = None
            uri = f"mongodb://integration_root:{mongo_password}@127.0.0.1:{port}/?authSource=admin"
            async with isolated_database(uri) as target:
                yield target
        finally:
            if client is not None:
                await client.close()
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)


async def test_real_mongo_identity_permissions_sessions_and_export_tenant_isolation(
    authenticated_mongo_uri: MongoTestTarget,
) -> None:
    config = Settings(
        _env_file=None,
        environment="test",
        task_backend="memory",
        mongo_enabled=True,
        mongo_uri=authenticated_mongo_uri.uri,
        mongo_database=authenticated_mongo_uri.database_name,
        identity_enabled=True,
        jwt_secret=SecretStr(secrets.token_urlsafe(48)),
        exports_api_enabled=True,
    )

    @asynccontextmanager
    async def probe(context: ModuleContext) -> AsyncIterator[ModuleExports]:
        mongo = context.resources["mongodb"]
        assert isinstance(mongo, MongoResource)
        source = MongoExportSource(mongo.database["probe_rows"], (ExportColumn("value"),))
        yield ModuleExports(datasets={"rows": source})

    app = server.create_app(
        config,
        modules=(build_identity_module(config), ModuleDefinition("probe", lifespan=probe)),
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        runtime = app.state.runtime
        identity = runtime.modules["identity"]
        assert isinstance(identity, IdentityServices)
        admin_a = await identity.service.bootstrap_admin("tenant_a", "admin", PASSWORD)
        admin_b = await identity.service.bootstrap_admin("tenant_b", "admin", PASSWORD)

        async def login(tenant: str, username: str) -> dict[str, str]:
            response = await client.post(
                "/api/v1/identity/login",
                json={"tenant_id": tenant, "username": username, "password": PASSWORD},
            )
            assert response.status_code == 200, response.text
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        headers_a = await login("tenant_a", "admin")
        headers_b = await login("tenant_b", "admin")
        body = {
            "username": "member",
            "password": PASSWORD,
            "permissions": ["data.read", "data.export"],
        }
        response = await client.post(
            "/api/v1/identity/register", headers=headers_a, json={**body, "tenant_id": "tenant_b"}
        )
        assert response.status_code == 422
        response = await client.post("/api/v1/identity/register", headers=headers_a, json=body)
        assert response.status_code == 201, response.text
        member_id = response.json()["user_id"]
        assert response.json()["tenant_id"] == "tenant_a"
        assert PASSWORD not in response.text and "password_hash" not in response.text
        assert (
            await client.post("/api/v1/identity/register", headers=headers_a, json=body)
        ).status_code == 409
        member_headers = await login("tenant_a", "member")
        assert (
            await client.post(
                "/api/v1/identity/login",
                json={"tenant_id": "tenant_b", "username": "member", "password": PASSWORD},
            )
        ).status_code == 401
        token = member_headers["Authorization"].removeprefix("Bearer ")
        parts = token.split(".")
        parts[2] = ("a" if parts[2][0] != "a" else "b") + parts[2][1:]
        assert (
            await client.get(
                "/api/v1/identity/me", headers={"Authorization": f"Bearer {'.'.join(parts)}"}
            )
        ).status_code == 401

        users = await client.get("/api/v1/identity/users", headers=headers_a)
        assert {user["tenant_id"] for user in users.json()} == {"tenant_a"}
        assert {user["user_id"] for user in users.json()} == {admin_a.user_id, member_id}
        assert (
            await client.patch(
                f"/api/v1/identity/users/{admin_b.user_id}",
                headers=headers_a,
                json={"active": False},
            )
        ).status_code == 404

        mongo = runtime.resources["mongodb"]
        assert isinstance(mongo, MongoResource)
        await mongo.database["probe_rows"].insert_many(
            [
                {"tenant_id": "tenant_a", "owner_id": admin_a.user_id, "value": "alpha_admin"},
                {"tenant_id": "tenant_a", "owner_id": member_id, "value": "alpha_member"},
                {"tenant_id": "tenant_b", "owner_id": admin_b.user_id, "value": "beta_admin"},
            ]
        )

        async def exported_values(headers: dict[str, str]) -> set[str]:
            response = await client.get("/api/v1/exports/probe.rows?format=jsonl", headers=headers)
            assert response.status_code == 200, response.text
            rows = [json.loads(line) for line in response.text.splitlines()]
            assert all(set(row) == {"value"} for row in rows)
            return {row["value"] for row in rows}

        assert await exported_values(member_headers) == {"alpha_member"}
        assert await exported_values(headers_a) == {"alpha_admin", "alpha_member"}
        assert await exported_values(headers_b) == {"beta_admin"}
        update_url = f"/api/v1/identity/users/{member_id}"
        assert (
            await client.patch(update_url, headers=headers_a, json={"permissions": ["data.read"]})
        ).status_code == 200
        assert (
            await client.get("/api/v1/exports/probe.rows?format=jsonl", headers=member_headers)
        ).status_code == 403
        await client.patch(update_url, headers=headers_a, json={"active": False})
        assert (await client.get("/api/v1/identity/me", headers=member_headers)).status_code == 401
        await client.patch(update_url, headers=headers_a, json={"active": True})
        assert (await client.get("/api/v1/identity/me", headers=member_headers)).status_code == 200
        assert (
            await client.post("/api/v1/identity/logout", headers=member_headers)
        ).status_code == 204
        assert (await client.get("/api/v1/identity/me", headers=member_headers)).status_code == 401
        assert (
            await mongo.database["identity_sessions"].count_documents(
                {"tenant_id": "tenant_a", "user_id": member_id, "revoked": True}
            )
            == 1
        )

        # Exercise Mongo's atomic limiter pipeline, including window rollover.
        repository = MongoIdentityRepository(mongo.database)
        now = datetime.now(UTC)
        assert all(
            [
                await repository.consume_login_attempt("tenant_a", "rate_probe", now)
                for _ in range(5)
            ]
        )
        assert not await repository.consume_login_attempt("tenant_a", "rate_probe", now)
        assert await repository.consume_login_attempt(
            "tenant_a", "rate_probe", now + timedelta(minutes=6)
        )
