"""Publish/backfill a verified model contract and a separately reviewed usage revision."""

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from bootstrap.ml_config import MLSettings
from bootstrap.ml_wiring import build_components
from bootstrap.prediction_contracts import extract_public_contract
from src.application.prediction.catalog import ContractPublicationService
from src.domain.ml import MLError
from src.infrastructure.ingestion.storage.raw_snapshot_writer import relative_file_fd
from src.infrastructure.ml_identity import MongoActorResolver
from src.infrastructure.serving.materializer import BundleMaterializer


def bounded_file(path: Path, maximum: int, *, private: bool = False) -> bytes:
    with relative_file_fd(path.parent, path.name) as descriptor:
        info = os.fstat(descriptor)
        if private and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("Session token file must be private")
        value = os.read(descriptor, maximum + 1)
        if not 0 < len(value) <= maximum:
            raise ValueError("Publication input exceeds its size limit")
        return value


async def publish(binding_id: str, usage_file: Path, token_file: Path) -> int:
    settings = MLSettings.from_env()
    identity = MongoActorResolver(
        os.environ["APP_MONGO_URI"],
        os.environ.get("APP_MONGO_DATABASE", "backend"),
        os.environ["APP_JWT_SECRET"],
        os.environ.get("APP_JWT_ISSUER", "backend"),
        os.environ.get("APP_JWT_AUDIENCE", "backend-api"),
    )
    try:
        actor = await identity.resolve(
            bounded_file(token_file, 16384, private=True).decode().strip()
        )
        usage = json.loads(bounded_file(usage_file, 16384))
        components = build_components(settings)
        try:
            materializer = BundleMaterializer(settings.artifact_root, settings.tracking_uri)
            service = ContractPublicationService(
                components.registry,
                components.registry,
                lambda binding: extract_public_contract(
                    binding, materializer, expected_code_digest=settings.code_revision
                ),
            )
            return service.publish(actor, binding_id, usage)
        finally:
            components.database.close()
    finally:
        identity.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding-id", required=True)
    parser.add_argument(
        "--review-file",
        type=Path,
        required=True,
        help="Approved plain-text purpose, conditions, limitations and support JSON",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        required=True,
        help="Private file containing the reviewer's current Identity access token",
    )
    args = parser.parse_args()
    try:
        revision = asyncio.run(publish(args.binding_id, args.review_file, args.token_file))
        print(json.dumps({"binding_id": args.binding_id, "usage_revision": revision}))
    except (MLError, ValueError, OSError, KeyError):
        print(
            "Contract publication failed; verify authorization and approved inputs.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
