"""Explicit operator commands; no accounts are created during application startup."""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack

from bootstrap.config import load_settings
from bootstrap.identity import build_identity_services
from src.domain.identity.errors import IdentityError
from src.infrastructure.persistence.mongo import MongoResource
from src.services.concurrency import BoundedThreadExecutor


async def create_admin(
    tenant_id: str,
    username: str,
    password: str,
    *,
    additional_permissions: frozenset[str] = frozenset(),
) -> None:
    settings = load_settings()
    if not settings.mongo_enabled or not settings.identity_enabled:
        raise ValueError("Enable APP_MONGO_ENABLED and APP_IDENTITY_ENABLED before provisioning")
    async with AsyncExitStack() as stack:
        mongo = MongoResource(settings)
        stack.push_async_callback(mongo.aclose)
        await mongo.start()
        runner = BoundedThreadExecutor(settings)
        stack.push_async_callback(runner.aclose)
        await runner.start()
        services = await build_identity_services(settings, mongo, runner)
        user = await services.service.bootstrap_admin(
            tenant_id, username, password, additional_permissions=additional_permissions
        )
        print(f"Created administrator {user.username!r} in tenant {user.tenant_id!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "create-admin", help="Explicitly provision a tenant administrator"
    )
    command.add_argument("--tenant-id", required=True)
    command.add_argument("--username", required=True)
    command.add_argument("--password-stdin", action="store_true")
    command.add_argument(
        "--permission",
        action="append",
        default=[],
        help="Additional module permission; repeat as needed",
    )
    args = parser.parse_args(argv)
    if args.password_stdin:
        password = sys.stdin.readline(1024).rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
    try:
        if args.permission:
            asyncio.run(
                create_admin(
                    args.tenant_id,
                    args.username,
                    password,
                    additional_permissions=frozenset(args.permission),
                )
            )
        else:
            asyncio.run(create_admin(args.tenant_id, args.username, password))
    except (IdentityError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
