"""Bounded, content-addressed local EBM feature snapshots; never unpickle input."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from src.domain.datasets.ebm import EBM_FEATURE_SHAPE, EBMDatasetMetadata
from src.domain.ml import MLError, canonical_json

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_ARRAY_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 4096
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: bytes) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MLError("Duplicate EBM JSON keys are forbidden")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise MLError("Invalid EBM snapshot JSON") from exc


def _validate_array(array: NDArray[Any], metadata: EBMDatasetMetadata) -> NDArray[np.float32]:
    if (not isinstance(array, np.ndarray) or array.dtype != np.dtype("float32")
            or array.shape != (len(metadata.samples), *EBM_FEATURE_SHAPE)
            or array.nbytes > MAX_ARRAY_BYTES or not np.isfinite(array).all()):
        raise MLError("EBM spectrograms require bounded finite float32 [N,7,4,9,31]")
    first_by_lot: dict[str, int] = {}
    for index, sample in enumerate(metadata.samples):
        first = first_by_lot.setdefault(sample.lot_id, index)
        if first != index and not np.array_equal(array[first], array[index]):
            raise MLError("EBM wafer rows of the same Lot must reference identical Lot features")
    # Backing bytes are immutable, including when callers attempt setflags(write=True).
    return np.frombuffer(array.tobytes(order="C"), dtype=np.float32).reshape(array.shape)


def _decode_npz(data: bytes, metadata: EBMDatasetMetadata) -> NDArray[np.float32]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if (len(members) != 1 or members[0].filename != "spectrograms.npy"
                    or members[0].file_size > MAX_ARRAY_BYTES + 16384
                    or members[0].flag_bits & 1):
                raise MLError("EBM NPZ must contain only one bounded spectrograms array")
            # Inspect the NPY header before numpy allocates memory for its shape.
            with archive.open(members[0]) as member:
                # NumPy's public NPY header helpers do not ship typed signatures.
                version = np.lib.format.read_magic(member)  # type: ignore[no-untyped-call]
                if version == (1, 0):
                    header_reader = np.lib.format.read_array_header_1_0
                elif version == (2, 0):
                    header_reader = np.lib.format.read_array_header_2_0
                else:
                    raise MLError("Unsupported EBM NPY format version")
                shape, _, dtype = header_reader(member)  # type: ignore[no-untyped-call]
                if dtype != np.dtype("float32") or shape != (
                    len(metadata.samples), *EBM_FEATURE_SHAPE
                ):
                    raise MLError("EBM NPZ dtype or shape does not match its metadata")
                expected = int(np.prod(shape)) * dtype.itemsize
                if expected > MAX_ARRAY_BYTES or members[0].file_size - member.tell() != expected:
                    raise MLError("EBM NPY payload length does not match its header")
        with np.load(io.BytesIO(data), allow_pickle=False) as loaded:
            return _validate_array(loaded["spectrograms"], metadata)
    except MLError:
        raise
    except (ValueError, OSError, EOFError, KeyError, zipfile.BadZipFile) as exc:
        raise MLError("Invalid EBM NPZ snapshot") from exc


@dataclass(frozen=True)
class EBMFeatureSnapshot:
    metadata: EBMDatasetMetadata
    spectrograms: NDArray[np.float32]
    content_digest: str


class EBMFileDatasetSource:
    """A standalone local importer/reader, not yet a tenant catalog or remote adapter."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self._no_symlinks(self.root)

    @staticmethod
    def _no_symlinks(path: Path) -> None:
        for component in (path, *path.parents):
            if component.is_symlink():
                raise MLError("EBM snapshot symbolic links are forbidden")

    def _path(self, path: Path) -> Path:
        candidate = Path(os.path.abspath(path))
        if self.root not in candidate.parents:
            raise MLError("EBM snapshot is outside its approved store")
        self._no_symlinks(candidate)
        return candidate

    def _read(self, path: Path, maximum: int) -> bytes:
        candidate = self._path(path)
        try:
            descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise MLError("EBM snapshot file is not regular or exceeds its size limit")
                data = handle.read(maximum + 1)
                if len(data) > maximum:
                    raise MLError("EBM snapshot file exceeds its size limit")
                return data
        except OSError as exc:
            raise MLError("EBM snapshot file cannot be read") from exc

    def load(self, reference: Mapping[str, str]) -> EBMFeatureSnapshot:
        expected = reference.get("content_digest", "")
        ref = reference.get("snapshot_ref", "")
        if (not isinstance(expected, str) or not _DIGEST.fullmatch(expected)
                or not isinstance(ref, str) or not ref or "\x00" in ref or len(ref) > 4096):
            raise MLError("EBM snapshot requires a valid content digest and reference")
        path = self._path(Path(ref))
        manifest_bytes = self._read(path, MAX_MANIFEST_BYTES)
        if _sha(manifest_bytes) != expected:
            raise MLError("EBM snapshot manifest digest mismatch")
        manifest = _json(manifest_bytes)
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "files"}
                or manifest["schema_version"] != "ebm-snapshot-manifest-v1"
                or not isinstance(manifest["files"], dict)
                or set(manifest["files"]) != {"metadata.json", "spectrograms.npz"}):
            raise MLError("Invalid EBM snapshot manifest contract")
        payloads = {}
        for name, limit in (("metadata.json", MAX_METADATA_BYTES),
                            ("spectrograms.npz", MAX_ARRAY_BYTES + 32768)):
            entry = manifest["files"][name]
            if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                    or not isinstance(entry["sha256"], str)
                    or not _DIGEST.fullmatch(entry["sha256"])
                    or type(entry["size_bytes"]) is not int
                    or not 0 < entry["size_bytes"] <= limit):
                raise MLError("Invalid EBM snapshot file declaration")
            data = self._read(path.parent / name, limit)
            if len(data) != entry["size_bytes"] or _sha(data) != entry["sha256"]:
                raise MLError("EBM snapshot content digest or size mismatch")
            payloads[name] = data
        metadata = EBMDatasetMetadata.parse(_json(payloads["metadata.json"]))
        return EBMFeatureSnapshot(metadata, _decode_npz(payloads["spectrograms.npz"], metadata),
                                  expected)

    def import_files(self, metadata_path: Path, npz_path: Path) -> dict[str, str]:
        """Import bounded input files already staged inside the approved store."""
        metadata = EBMDatasetMetadata.parse(_json(self._read(metadata_path, MAX_METADATA_BYTES)))
        arrays = _decode_npz(self._read(npz_path, MAX_ARRAY_BYTES + 32768), metadata)
        return self.publish(metadata.to_dict(), arrays)

    def publish(self, metadata: Mapping[str, Any], spectrograms: NDArray[Any]) -> dict[str, str]:
        """Publish by content digest using a staging directory and atomic rename."""
        parsed = EBMDatasetMetadata.parse(dict(metadata))
        array = _validate_array(spectrograms, parsed)
        buffer = io.BytesIO()
        np.savez(buffer, spectrograms=array)
        payloads = {"metadata.json": canonical_json(parsed.to_dict()).encode("utf-8"),
                    "spectrograms.npz": buffer.getvalue()}
        if len(payloads["metadata.json"]) > MAX_METADATA_BYTES:
            raise MLError("EBM metadata exceeds its size limit")
        manifest = canonical_json({"schema_version": "ebm-snapshot-manifest-v1", "files": {
            name: {"sha256": _sha(data), "size_bytes": len(data)}
            for name, data in payloads.items()}}).encode("utf-8")
        content_digest = _sha(manifest)
        destination = self._path(self.root / content_digest)
        reference = {"snapshot_ref": str(destination / "manifest.json"),
                     "content_digest": content_digest}
        self._no_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.load(reference)
            return reference
        with tempfile.TemporaryDirectory(prefix=".ebm-publish-", dir=self.root) as staging_name:
            staging = Path(staging_name)
            for name, data in {**payloads, "manifest.json": manifest}.items():
                with (staging / name).open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            try:
                staging.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                self.load(reference)
        return reference
