"""Durable identity and stop records shared by a trusted supervisor and guardian."""

import json
import os
from pathlib import Path
from uuid import uuid4

from src.domain.ml import canonical_json


def publish(path: Path, value: dict, *, exclusive: bool = False) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            stream.write(canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Invalid training runtime record")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid training runtime record")
    return value


def matches(value: dict, identity: dict) -> bool:
    return all(
        value.get(key) == identity[key]
        for key in (
            "execution_id",
            "attempt_id",
            "owner_token",
            "spec_digest",
            "pod_uid",
            "container_id",
        )
    )
