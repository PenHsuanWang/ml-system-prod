"""Adapt projected Kubernetes files to the unchanged production bootstrap contract."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path


def copy_private(source: Path, destination: Path, maximum: int = 65536) -> None:
    # The source intentionally follows Kubernetes' atomic-writer symlinks. Only
    # the init container mounts it; the application receives a private copy.
    with source.open("rb") as stream:
        data = stream.read(maximum + 1)
    if not data or len(data) > maximum:
        raise ValueError("Projected configuration has an invalid size")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("Private configuration destination links are forbidden")
    destination.parent.chmod(0o700)
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400
        )
    except FileExistsError:
        # An init-container restart may reuse only the exact validated copy.
        descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o400
                and stream.read(maximum + 1) == data
            ):
                return
        raise FileExistsError("Private configuration differs; recreate the Pod") from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    from bootstrap.deployment_entrypoint import configure_runtime_home, load_runtime_environment

    copy_private(Path("/projected/role/runtime.json"), Path("/run/private/config/runtime.json"))
    copy_private(Path("/projected/ca/ca.crt"), Path("/run/private/config/mlflow-ca.pem"))
    environment = load_runtime_environment(os.environ)
    configure_runtime_home(environment)
    if os.environ.get("ML_WAIT_FOR_SCHEMA") == "true":
        from src.infrastructure.persistence.ml_database import Database

        database = Database(environment["ML_DATABASE_URL"])
        try:
            deadline = time.monotonic() + 240
            while True:
                try:
                    database.ready()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Migration schema is not ready") from None
                    time.sleep(2)
        finally:
            database.close()
    print("Production bootstrap configuration validated")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Foundation initialization failed; inspect configuration without logging secrets"
        ) from None
