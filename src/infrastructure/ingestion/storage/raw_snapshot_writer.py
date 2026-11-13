"""Private streaming staging and Linux atomic no-overwrite raw publication.

All path traversal uses directory file descriptors and refuses symbolic links.
The caller durably saves PublicationIntent before publishing and retains staging
until SQL completion succeeds. Recovery reads the same stable snapshot identity.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import os
import re
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from src.domain.ingestion.errors import (
    IngestionError,
    PublicationUncertain,
    SnapshotConflict,
    SnapshotInvalid,
)
from src.domain.ingestion.models import (
    BatchExtraction,
    ExtractionLimits,
    PublicationIntent,
    RawSnapshot,
    StagedBatch,
    identifier,
    sha256,
)
from src.domain.ml import canonical_json, digest
from src.infrastructure.ingestion.storage.jsonl import bounded_lines, object_record

_MAX_BYTES = 128 * 1024 * 1024
_MANIFEST_LIMIT = 32768
_PART_NAME = "data.jsonl"


class CheckControl(Protocol):
    def check(self) -> None: ...


def relative_parts(value: str) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise SnapshotInvalid("Invalid approved relative file location")
    parts = tuple(value.split("/"))
    if len(parts) > 64 or any(part in {"", ".", ".."} for part in parts):
        raise SnapshotInvalid("Invalid approved relative file location")
    return parts


@contextmanager
def directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open every absolute directory component without following symbolic links."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise SnapshotInvalid("Invalid raw storage directory")
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def relative_file_fd(root: Path, relative: str) -> Iterator[int]:
    parts = relative_parts(relative)
    with directory_fd(root) as root_descriptor:
        parent = os.dup(root_descriptor)
        try:
            for component in parts[:-1]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                os.close(parent)
                parent = child
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise SnapshotInvalid("Raw input must be a regular file")
                yield descriptor
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)


def _json(value: bytes) -> dict[str, Any]:
    from src.infrastructure.ingestion.storage.jsonl import object_record

    return object_record(value)


def _read_file(directory: Path, name: str, maximum: int) -> bytes:
    with relative_file_fd(directory, name) as descriptor:
        info = os.fstat(descriptor)
        if not 0 < info.st_size <= maximum:
            raise SnapshotInvalid("Raw snapshot metadata exceeds its size limit")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            result = stream.read(maximum + 1)
        if len(result) > maximum:
            raise SnapshotInvalid("Raw snapshot metadata exceeds its size limit")
        return result


def _chunks(descriptor: int, size: int) -> Iterator[bytes]:
    while value := os.read(descriptor, size):
        yield value


def _evidence(directory: Path, limits: ExtractionLimits) -> tuple[str, int, int]:
    total, records = 0, 0
    checksum = hashlib.sha256()
    with relative_file_fd(directory, _PART_NAME) as descriptor:
        before = os.fstat(descriptor)
        if not 0 < before.st_size <= limits.max_bytes:
            raise SnapshotInvalid("Raw snapshot payload exceeds its size limit")
        for line in bounded_lines(_chunks(descriptor, min(65536, limits.max_line_bytes)), limits):
            checksum.update(line)
            total += len(line)
            records += 1
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after) or total != before.st_size:
            raise SnapshotInvalid("Raw snapshot payload changed while being verified")
    return checksum.hexdigest(), total, records


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _write_file(directory_descriptor: int, name: str, value: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_descriptor,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _rename_exclusive(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
    """Linux renameat2(RENAME_NOREPLACE) cannot replace even an empty target directory."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise PublicationUncertain("Atomic no-overwrite raw publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError("Raw snapshot identity already exists")
        raise OSError(code, "Atomic raw publication failed")


class LocalRawSnapshotStore:
    """One approved raw store; RawSnapshots never register an ML Dataset Version."""

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        try:
            with directory_fd(self.root, create=True):
                pass
            with directory_fd(self.root / ".staging", create=True):
                pass
            with directory_fd(self.root / "published", create=True):
                pass
        except OSError as error:
            raise SnapshotInvalid("Approved raw snapshot store is unavailable") from error

    def _staging_directory(self, staged: StagedBatch) -> Path:
        path = Path(staged.staging_ref)
        expected_parent = self.root / ".staging" / digest(staged.tenant_id) / staged.job_id
        if (
            not path.is_absolute()
            or path.parent != expected_parent
            or not path.name.startswith(staged.attempt_id + "-")
            or len(path.name) != len(staged.attempt_id) + 33
            or any(char not in "0123456789abcdef" for char in path.name[-32:])
        ):
            raise SnapshotInvalid("Staging reference is outside the approved attempt store")
        return path

    def stage(
        self, extraction: BatchExtraction, chunks: Iterable[bytes], control: CheckControl
    ) -> StagedBatch:
        self._validate_extraction(extraction)
        execution = extraction.execution
        parent = self.root / ".staging" / digest(execution.tenant_id) / execution.job_id
        name = execution.attempt_id + "-" + uuid4().hex
        path = parent / name
        try:
            control.check()
            with directory_fd(parent, create=True) as parent_descriptor:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            with directory_fd(path) as descriptor:
                output = os.open(
                    _PART_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                checksum = hashlib.sha256()
                total, records = 0, 0
                with os.fdopen(output, "wb") as stream:
                    for line in bounded_lines(chunks, extraction.limits):
                        control.check()
                        stream.write(line)
                        checksum.update(line)
                        total += len(line)
                        records += 1
                    control.check()
                    stream.flush()
                    os.fsync(stream.fileno())
                staged = StagedBatch(
                    str(path),
                    execution.tenant_id,
                    execution.job_id,
                    execution.attempt_id,
                    checksum.hexdigest(),
                    total,
                    records,
                    extraction.source.data_format,
                    extraction.source.schema_version,
                    "sha256:" + checksum.hexdigest(),
                )
                _write_file(descriptor, "stage.json", canonical_json(staged.to_dict()).encode())
                os.fsync(descriptor)
            return staged
        except BaseException:
            self._discard_path(path, parent)
            raise

    @staticmethod
    def _validate_extraction(extraction: BatchExtraction) -> None:
        if (
            extraction.source.tenant_id != extraction.execution.tenant_id
            or extraction.source.data_format != "jsonl"
            or extraction.source.schema_version != "raw-json-object-v1"
            or extraction.limits.max_bytes > extraction.source.limits.max_bytes
            or extraction.limits.max_records > extraction.source.limits.max_records
            or extraction.limits.max_line_bytes > extraction.source.limits.max_line_bytes
            or extraction.limits.timeout_seconds > extraction.source.limits.timeout_seconds
        ):
            raise SnapshotInvalid("Raw extraction does not match its approved source contract")

    def _validate_stage(self, staged: StagedBatch, limits: ExtractionLimits) -> Path:
        path = self._staging_directory(staged)
        try:
            saved = _json(_read_file(path, "stage.json", _MANIFEST_LIMIT))
            expected = staged.to_dict()
            # File evidence belongs to the stager; external revision provenance
            # belongs to the approved source adapter (for example a Delta version).
            saved.pop("source_revision", None)
            expected.pop("source_revision")
            if saved != expected:
                raise SnapshotInvalid("Staging evidence does not match its registered attempt")
            evidence = _evidence(path, limits)
            if evidence != (staged.content_digest, staged.size_bytes, staged.record_count):
                raise SnapshotInvalid("Staged raw content digest or record metadata mismatch")
            return path
        except OSError as error:
            raise SnapshotInvalid("Staged raw content cannot be verified") from error

    def prepare(self, extraction: BatchExtraction, staged: StagedBatch) -> PublicationIntent:
        self._validate_extraction(extraction)
        execution, source = extraction.execution, extraction.source
        if (
            staged.tenant_id != execution.tenant_id
            or staged.job_id != execution.job_id
            or staged.attempt_id != execution.attempt_id
            or staged.data_format != source.data_format
            or staged.schema_version != source.schema_version
        ):
            raise SnapshotInvalid("Staged raw data does not belong to this ingestion execution")
        self._validate_stage(staged, extraction.limits)
        manifest = {
            "manifest_version": "raw-snapshot-v1",
            "snapshot_id": execution.snapshot_id,
            "tenant_id": execution.tenant_id,
            "source_id": source.source_id,
            "source_version": source.version,
            "adapter_type": source.adapter_type,
            "adapter_version": source.adapter_version,
            "source_configuration_digest": source.configuration_digest,
            "source_revision": staged.source_revision,
            "content_digest": staged.content_digest,
            "data_format": staged.data_format,
            "schema_version": staged.schema_version,
            "size_bytes": staged.size_bytes,
            "record_count": staged.record_count,
            "created_at": execution.created_at,
            "job_id": execution.job_id,
            "part": {
                "path": _PART_NAME,
                "sha256": staged.content_digest,
                "size_bytes": staged.size_bytes,
            },
        }
        return PublicationIntent(
            execution.snapshot_id,
            execution.tenant_id,
            execution.job_id,
            staged,
            canonical_json(manifest),
            digest(manifest),
        )

    def _final(self, tenant_id: str, snapshot_id: str) -> Path:
        identifier(tenant_id)
        identifier(snapshot_id)
        return self.root / "published" / digest(tenant_id) / snapshot_id

    def _snapshot(self, manifest: dict[str, Any], manifest_digest: str, path: Path) -> RawSnapshot:
        fields = {
            "manifest_version",
            "snapshot_id",
            "tenant_id",
            "source_id",
            "source_version",
            "adapter_type",
            "adapter_version",
            "source_configuration_digest",
            "source_revision",
            "content_digest",
            "data_format",
            "schema_version",
            "size_bytes",
            "record_count",
            "created_at",
            "job_id",
            "part",
        }
        try:
            if (
                set(manifest) != fields
                or manifest["manifest_version"] != "raw-snapshot-v1"
                or manifest["data_format"] != "jsonl"
                or manifest["schema_version"] != "raw-json-object-v1"
                or manifest["part"]
                != {
                    "path": _PART_NAME,
                    "sha256": manifest["content_digest"],
                    "size_bytes": manifest["size_bytes"],
                }
            ):
                raise ValueError("Invalid raw manifest")
            identifier(manifest["adapter_type"])
            identifier(manifest["adapter_version"])
            staged_revision = manifest["source_revision"]
            if not isinstance(staged_revision, str) or not re.fullmatch(
                r"[A-Za-z0-9:_.-]{1,256}", staged_revision
            ):
                raise ValueError("Invalid source revision")
            return RawSnapshot(
                **{
                    name: manifest[name]
                    for name in (
                        "snapshot_id",
                        "tenant_id",
                        "source_id",
                        "source_version",
                        "source_configuration_digest",
                        "content_digest",
                        "data_format",
                        "schema_version",
                        "size_bytes",
                        "record_count",
                        "created_at",
                        "job_id",
                        "source_revision",
                    )
                },
                manifest_digest=manifest_digest,
                snapshot_ref=str(path / "manifest.json"),
            )
        except (ValueError, TypeError, KeyError, IngestionError) as error:
            raise SnapshotInvalid("Invalid immutable raw snapshot manifest") from error

    def lookup(self, tenant_id: str, snapshot_id: str, manifest_digest: str) -> RawSnapshot | None:
        sha256(manifest_digest)
        path = self._final(tenant_id, snapshot_id)
        identity_observed = False
        try:
            with directory_fd(path.parent) as descriptor:
                try:
                    os.stat(snapshot_id, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return None
                identity_observed = True
            data = _read_file(path, "manifest.json", _MANIFEST_LIMIT)
            if hashlib.sha256(data).hexdigest() != manifest_digest:
                raise SnapshotConflict("Raw snapshot identity is already bound to other content")
            snapshot = self._snapshot(_json(data), manifest_digest, path)
            if snapshot.tenant_id != tenant_id or snapshot.snapshot_id != snapshot_id:
                raise SnapshotInvalid("Raw snapshot identity does not match its storage location")
            limits = ExtractionLimits(
                max_bytes=_MAX_BYTES, max_records=1_000_000, max_line_bytes=4 * 1024 * 1024
            )
            if _evidence(path, limits) != (
                snapshot.content_digest,
                snapshot.size_bytes,
                snapshot.record_count,
            ):
                raise SnapshotInvalid("Immutable raw snapshot payload digest mismatch")
            return snapshot
        except FileNotFoundError:
            # Classify the observation that failed. A competing publication can
            # create the tenant directory before this handler runs, so probing
            # its current existence would turn an ordinary miss into corruption.
            if not identity_observed:
                return None
            raise SnapshotInvalid("Incomplete raw snapshot is not published") from None
        except OSError as error:
            raise SnapshotInvalid("Immutable raw snapshot cannot be verified") from error

    def verify(self, snapshot: RawSnapshot) -> RawSnapshot:
        expected = self._final(snapshot.tenant_id, snapshot.snapshot_id) / "manifest.json"
        if Path(snapshot.snapshot_ref) != expected:
            raise SnapshotInvalid("Raw snapshot reference is outside the approved store")
        actual = self.lookup(snapshot.tenant_id, snapshot.snapshot_id, snapshot.manifest_digest)
        if actual is None or actual != snapshot:
            raise SnapshotInvalid("Raw snapshot metadata does not match the immutable artifact")
        return actual

    def read_records(self, snapshot: RawSnapshot) -> tuple[dict[str, Any], ...]:
        """Return verified JSONL records from this store's immutable snapshot namespace."""

        snapshot = self.verify(snapshot)
        path = self._final(snapshot.tenant_id, snapshot.snapshot_id)
        limits = ExtractionLimits(
            max_bytes=_MAX_BYTES,
            max_records=1_000_000,
            max_line_bytes=4 * 1024 * 1024,
        )
        try:
            with relative_file_fd(path, _PART_NAME) as descriptor:
                before = os.fstat(descriptor)
                if not 0 < before.st_size <= limits.max_bytes:
                    raise SnapshotInvalid("Raw snapshot payload exceeds its size limit")
                checksum = hashlib.sha256()
                total = 0
                records: list[dict[str, Any]] = []
                for line in bounded_lines(
                    _chunks(descriptor, min(65536, limits.max_line_bytes)), limits
                ):
                    checksum.update(line)
                    total += len(line)
                    records.append(object_record(line))
                after = os.fstat(descriptor)
                if _file_identity(before) != _file_identity(after) or total != before.st_size:
                    raise SnapshotInvalid("Raw snapshot payload changed while being read")
                if (
                    checksum.hexdigest(),
                    total,
                    len(records),
                ) != (
                    snapshot.content_digest,
                    snapshot.size_bytes,
                    snapshot.record_count,
                ):
                    raise SnapshotInvalid("Immutable raw snapshot payload digest mismatch")
                return tuple(records)
        except OSError as error:
            raise SnapshotInvalid("Immutable raw snapshot cannot be read") from error

    def publish(self, intent: PublicationIntent) -> RawSnapshot:
        # The exact intent is persisted by the application before this side effect.
        path = self._final(intent.tenant_id, intent.snapshot_id)
        manifest_bytes = intent.manifest_json.encode()
        if hashlib.sha256(manifest_bytes).hexdigest() != intent.manifest_digest:
            raise SnapshotInvalid(
                "Publication intent must contain canonical verified manifest bytes"
            )
        snapshot = self._snapshot(_json(manifest_bytes), intent.manifest_digest, path)
        staged = intent.staged
        if (
            snapshot.tenant_id != intent.tenant_id
            or snapshot.snapshot_id != intent.snapshot_id
            or snapshot.job_id != intent.job_id
            or (snapshot.content_digest, snapshot.size_bytes, snapshot.record_count)
            != (staged.content_digest, staged.size_bytes, staged.record_count)
            or snapshot.data_format != staged.data_format
            or snapshot.schema_version != staged.schema_version
            or snapshot.source_revision != staged.source_revision
        ):
            raise SnapshotInvalid("Raw publication intent identity or staging evidence mismatch")
        existing = self.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest)
        if existing is not None:
            return existing
        source = self._validate_stage(
            staged,
            ExtractionLimits(
                max_bytes=_MAX_BYTES, max_records=1_000_000, max_line_bytes=4 * 1024 * 1024
            ),
        )
        temporary_name = ".publish-" + intent.job_id + "-" + uuid4().hex
        temporary = path.parent / temporary_name
        try:
            with directory_fd(path.parent, create=True) as parent:
                os.mkdir(temporary_name, mode=0o700, dir_fd=parent)
            with directory_fd(temporary) as descriptor:
                output = os.open(
                    _PART_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                checksum = hashlib.sha256()
                total = 0
                with (
                    os.fdopen(output, "wb") as target,
                    relative_file_fd(source, _PART_NAME) as incoming,
                ):
                    for chunk in _chunks(incoming, 65536):
                        total += len(chunk)
                        if total > staged.size_bytes:
                            raise SnapshotInvalid("Staged content changed before raw publication")
                        target.write(chunk)
                        checksum.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if total != staged.size_bytes or checksum.hexdigest() != staged.content_digest:
                    raise SnapshotInvalid("Staged content changed before raw publication")
                _write_file(descriptor, "manifest.json", manifest_bytes)
                os.fsync(descriptor)
            with directory_fd(path.parent) as parent:
                try:
                    _rename_exclusive(temporary_name, path.name, parent, parent)
                except FileExistsError:
                    existing = self.lookup(
                        intent.tenant_id, intent.snapshot_id, intent.manifest_digest
                    )
                    if existing is None:
                        raise PublicationUncertain(
                            "Raw publication result requires reconciliation"
                        ) from None
                    return existing
                os.fsync(parent)
            result = self.lookup(intent.tenant_id, intent.snapshot_id, intent.manifest_digest)
            if result is None:
                raise PublicationUncertain("Raw publication result requires reconciliation")
            return result
        except (SnapshotConflict, SnapshotInvalid, PublicationUncertain):
            raise
        except OSError as error:
            raise PublicationUncertain("Raw publication result requires reconciliation") from error
        finally:
            self._discard_path(temporary, path.parent)

    def cleanup_abandoned(self, protected_job_ids: frozenset[str], older_than: float) -> int:
        """Boundedly clean old private attempts and job-tagged publication copies.

        The caller supplies all nonterminal job IDs, including uncertain
        publication/reconciliation jobs. Final snapshot contents are untouched;
        only tagged private publication siblings qualify for cleanup. Each of
        the two namespaces has its own 5,000-entry scan budget and an atomically
        persisted directory-cookie cursor, so later invocations resume instead
        of rescanning the same history. Unrecognized directories and symbolic
        links are left untouched.
        """
        if (
            not isinstance(protected_job_ids, frozenset)
            or isinstance(older_than, bool)
            or not isinstance(older_than, (int, float))
            or not math.isfinite(older_than)
            or older_than < 0
        ):
            raise SnapshotInvalid("Invalid staging retention bounds")
        for job_id in protected_job_ids:
            identifier(job_id)
        return self._cleanup_attempts(
            protected_job_ids, older_than
        ) + self._cleanup_publication_temps(protected_job_ids, older_than)

    def _cleanup_attempts(self, protected_job_ids: frozenset[str], older_than: float) -> int:
        from src.infrastructure.ingestion.storage.raw_snapshot_cleanup import cleanup_namespace

        return cleanup_namespace(self, ".staging", protected_job_ids, older_than)

    def _cleanup_publication_temps(
        self, protected_job_ids: frozenset[str], older_than: float
    ) -> int:
        from src.infrastructure.ingestion.storage.raw_snapshot_cleanup import cleanup_namespace

        return cleanup_namespace(self, "published", protected_job_ids, older_than)

    def discard(self, staged: StagedBatch) -> None:
        path = self._staging_directory(staged)
        self._discard_path(path, path.parent)

    @staticmethod
    def _discard_path(path: Path, parent: Path) -> bool:
        """Remove only this known private directory's fixed filenames; never recurse."""
        try:
            with directory_fd(path) as descriptor:
                for name in (_PART_NAME, "stage.json", "manifest.json"):
                    try:
                        os.unlink(name, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            with directory_fd(parent) as descriptor:
                os.rmdir(path.name, dir_fd=descriptor)
            return True
        except (OSError, SnapshotInvalid):
            # Recovery may still need this private evidence; cleanup is repeatable.
            return False
