"""Verify all bytes and paths before constructing or deserializing a model."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from src.domain.ml import MLError


class ArtifactVerifier:
    def __init__(self, *, maximum_bytes: int = 128*1024*1024) -> None:
        self.maximum_bytes = maximum_bytes

    def verify(self, directory: Path, expected_digest: str) -> dict[str, Any]:
        root = directory.resolve(strict=True)
        path = root / "manifest.json"
        if path.is_symlink() or path.stat().st_size > 65536:
            raise MLError("Invalid artifact manifest")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_digest:
            raise MLError("Artifact manifest digest mismatch")
        manifest = json.loads(data)
        if manifest.get("manifest_version") != "2" or (
            manifest.get("artifact", {}).get("format") != "trusted_pytorch_state_dict"
        ):
            raise MLError("Unsupported artifact contract")
        entries = manifest["artifact"]["files"]
        if not isinstance(entries, list) or len(entries) > 16:
            raise MLError("Invalid artifact file list")
        seen = set()
        total = 0
        for item in entries:
            name = item["path"]
            if not isinstance(name, str) or Path(name).name != name or name in {".", ".."}:
                raise MLError("Invalid artifact path")
            file = root/name
            if name in seen or file.is_symlink() or not file.is_file() or file.resolve().parent != root:
                raise MLError("Invalid artifact file")
            seen.add(name)
            total += file.stat().st_size
            if total > self.maximum_bytes or file.stat().st_size != item["size_bytes"]:
                raise MLError("Artifact size limit or size mismatch")
            if hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]:
                raise MLError("Artifact file digest mismatch")
        required = {"weights.pt", "preprocessing.json", "feature_schema.json",
                    "output_mapping.json", "golden_io.json"}
        if not required <= seen:
            raise MLError("Incomplete inference bundle")
        return cast(dict[str, Any], manifest)
