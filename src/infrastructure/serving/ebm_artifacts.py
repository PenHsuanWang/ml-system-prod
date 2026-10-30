"""Bounded, hash-verified EBM neural bundles; no model deserialization here."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

from src.domain.ml import MLError

EBM_ARTIFACT_PROFILE = "ebm-neural-bundle-v1"
EBM_COMPATIBILITY_ID = "ebm-torch-cpu-v1"
EBM_BUNDLE_FILES = frozenset({
    "weights.pt", "model_config.json", "feature_schema.json", "spectrogram_config.json",
    "preprocessing.json", "normalization.npz", "output_mapping.json", "golden_io.json",
    "golden_input.npz",
})


def _read_regular_bytes(path: Path, maximum_bytes: int) -> bytes:
    """Bound reads and reject links, including links in ancestor directories."""
    absolute = path.absolute()
    if ".." in absolute.parts or any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError("Artifact symbolic links and parent traversal are forbidden")
    descriptor = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            raise ValueError("Artifact is not regular or exceeds its size limit")
        data = handle.read(maximum_bytes + 1)
        if len(data) > maximum_bytes:
            raise ValueError("Artifact exceeds its size limit")
        return data


def _parse_json(data: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError(f"Invalid JSON constant: {value}")

    value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def read_json(path: Path, *, maximum_bytes: int = 1024 * 1024) -> dict[str, Any]:
    """Reject duplicate keys and non-finite JSON before interpreting a contract."""
    try:
        return _parse_json(_read_regular_bytes(path, maximum_bytes))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise MLError(f"Invalid EBM JSON contract: {path.name}") from exc


def load_npz(path: Path, shapes: dict[str, tuple[int, ...]], *,
             maximum_bytes: int) -> dict[str, Any]:
    """Preflight ZIP and NPY headers before array allocation; never enable pickle."""
    import numpy as np

    try:
        if (type(maximum_bytes) is not int or maximum_bytes < 1 or not shapes
                or any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", key)
                       or not isinstance(shape, tuple) or not shape
                       or any(type(size) is not int or size < 1 for size in shape)
                       for key, shape in shapes.items())
                or sum(math.prod(shape) * 4 for shape in shapes.values()) > maximum_bytes):
            raise ValueError("Invalid bounded EBM array shape contract")
        # ZIP headers add a small overhead beyond the declared uncompressed NPY sizes.
        data = _read_regular_bytes(path, maximum_bytes + 65536)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if ({entry.filename for entry in entries} != {f"{key}.npy" for key in shapes}
                    or len(entries) != len(shapes)
                    or sum(entry.file_size for entry in entries) > maximum_bytes
                    or any(entry.flag_bits & 1 for entry in entries)):
                raise ValueError("Unexpected or oversized NPZ entries")
            for entry in entries:
                key = entry.filename[:-4]
                with archive.open(entry) as member:
                    # NumPy's public NPY header helpers have no typed signatures.
                    version = np.lib.format.read_magic(member)  # type: ignore[no-untyped-call]
                    if version == (1, 0):
                        header_reader = np.lib.format.read_array_header_1_0
                    elif version == (2, 0):
                        header_reader = np.lib.format.read_array_header_2_0
                    else:
                        raise ValueError("Unsupported NPY header version")
                    shape, _, dtype = header_reader(member)  # type: ignore[no-untyped-call]
                    if dtype != np.dtype("float32") or shape != shapes[key]:
                        raise ValueError("NPY shape or dtype does not match the approved contract")
                    expected_payload = math.prod(shapes[key]) * 4
                    if entry.file_size - member.tell() != expected_payload:
                        raise ValueError("NPY payload size does not match its header")
        # Reuse the exact bytes checked above, rather than reopening a mutable path.
        with np.load(io.BytesIO(data), allow_pickle=False) as archive:
            result = {key: archive[key] for key in shapes}
        if any(value.dtype != np.dtype("float32") or value.shape != shapes[key]
               or not np.isfinite(value).all() for key, value in result.items()):
            raise ValueError("Invalid finite float32 NPZ arrays")
        return result
    except (OSError, ValueError, KeyError, TypeError, EOFError, RecursionError,
            NotImplementedError, zipfile.BadZipFile) as exc:
        raise MLError(f"Invalid EBM array contract: {path.name}") from exc


class EBMArtifactVerifier:
    def __init__(self, *, maximum_bytes: int = 128 * 1024 * 1024) -> None:
        self.maximum_bytes = maximum_bytes

    def verify(self, directory: Path, expected_digest: str) -> dict[str, Any]:
        try:
            absolute = directory.absolute()
            if (".." in absolute.parts
                    or any(part.is_symlink() for part in (absolute, *absolute.parents))
                    or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
                raise ValueError("Invalid bundle reference")
            root = directory.resolve(strict=True)
            manifest_bytes = _read_regular_bytes(root / "manifest.json", 65536)
            manifest = _parse_json(manifest_bytes)
            if hashlib.sha256(manifest_bytes).hexdigest() != expected_digest:
                raise ValueError("Manifest digest mismatch")
            artifact = manifest["artifact"]
            if (manifest.get("manifest_version") != "2"
                    or artifact.get("format") != "trusted_pytorch_state_dict"
                    or artifact.get("profile") != EBM_ARTIFACT_PROFILE):
                raise ValueError("Unsupported EBM artifact profile")
            entries = artifact["files"]
            if not isinstance(entries, list) or len(entries) != len(EBM_BUNDLE_FILES):
                raise ValueError("Invalid EBM file list")
            seen: set[str] = set()
            total = 0
            for item in entries:
                name, size, sha = item["path"], item["size_bytes"], item["sha256"]
                if (not isinstance(name, str) or name not in EBM_BUNDLE_FILES or name in seen
                        or type(size) is not int or size < 1
                        or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
                    raise ValueError("Invalid EBM artifact entry")
                path = root / name
                if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
                    raise ValueError("Invalid EBM artifact path")
                total += size
                if total > self.maximum_bytes or path.stat().st_size != size:
                    raise ValueError("EBM artifact size mismatch")
                payload = _read_regular_bytes(path, size)
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != sha:
                    raise ValueError("EBM artifact digest mismatch")
                seen.add(name)
            if seen != EBM_BUNDLE_FILES:
                raise ValueError("Incomplete EBM inference bundle")
            return manifest
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise MLError("EBM artifact verification failed") from exc
