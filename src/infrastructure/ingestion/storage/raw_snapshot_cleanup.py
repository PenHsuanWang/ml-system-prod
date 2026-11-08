"""Bounded Linux directory-cookie traversal for restartable raw-store cleanup."""

import ctypes
import errno
import fcntl
import json
import os
import re
import stat
import struct
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.domain.ingestion.errors import SnapshotInvalid
from src.domain.ml import canonical_json
from src.infrastructure.ingestion.storage.raw_snapshot_writer import (
    LocalRawSnapshotStore,
    _read_file,
    _write_file,
    directory_fd,
)

_BUDGET = 5000
_TENANT = re.compile(r"[a-f0-9]{64}")
_JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}-[a-f0-9]{32}")
_PUBLICATION = re.compile(r"\.publish-([A-Za-z0-9][A-Za-z0-9_.-]{0,127})-[a-f0-9]{32}")


def _frame(parts: list[str]) -> dict[str, Any]:
    return {"parts": parts, "cookie": 0, "device": 0, "inode": 0}


def _read_frames(root: Path, filename: str, namespace: str) -> list[dict[str, Any]]:
    try:
        state = json.loads(_read_file(root, filename, 16384))
    except FileNotFoundError:
        return [_frame([])]
    except (ValueError, OSError) as error:
        raise SnapshotInvalid("Raw cleanup cursor cannot be read") from error
    if not isinstance(state, dict) or set(state) != {"version", "frames"}:
        raise SnapshotInvalid("Invalid raw cleanup cursor")
    frames = state["frames"]
    maximum_depth = 3 if namespace == ".staging" else 2
    if (
        type(state["version"]) is not int
        or state["version"] != 1
        or not isinstance(frames, list)
        or len(frames) > maximum_depth
    ):
        raise SnapshotInvalid("Invalid raw cleanup cursor")
    for depth, frame in enumerate(frames):
        if (
            not isinstance(frame, dict)
            or set(frame) != {"parts", "cookie", "device", "inode"}
            or not isinstance(frame["parts"], list)
            or len(frame["parts"]) != depth
            or any(
                type(frame[key]) is not int or not 0 <= frame[key] <= 2**64 - 1
                for key in ("cookie", "device", "inode")
            )
            or frame["cookie"] > 2**63 - 1
        ):
            raise SnapshotInvalid("Invalid raw cleanup cursor")
        parts = frame["parts"]
        if (
            depth >= 1
            and (not isinstance(parts[0], str) or not _TENANT.fullmatch(parts[0]))
            or depth == 2
            and (not isinstance(parts[1], str) or not _JOB.fullmatch(parts[1]))
            or depth > 0
            and parts[:-1] != frames[depth - 1]["parts"]
        ):
            raise SnapshotInvalid("Invalid raw cleanup cursor")
    return frames or [_frame([])]


def _entries(descriptor: int, cookie: int) -> list[tuple[str, int]]:
    """Read one bounded kernel buffer, retaining each entry's exact resume cookie.

    A scandir iterator's underlying FD position can advance past buffered entries;
    serializing that position would skip work. Linux getdents64 exposes each
    directory entry's own d_off instead. Cookies are opaque, not numeric indexes.
    """
    function = getattr(ctypes.CDLL(None, use_errno=True), "getdents64", None)
    if function is None:
        raise SnapshotInvalid("Restartable raw cleanup requires Linux getdents64")
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    function.restype = ctypes.c_ssize_t
    os.lseek(descriptor, cookie, os.SEEK_SET)
    buffer = ctypes.create_string_buffer(4096)
    length = function(descriptor, buffer, len(buffer))
    if length < 0:
        raise OSError(ctypes.get_errno(), "Raw cleanup directory scan failed")
    result = []
    position = 0
    data = buffer.raw[:length]
    while position < length:
        if length - position < 20:
            raise SnapshotInvalid("Invalid raw cleanup directory entry")
        _, next_cookie, size, _ = struct.unpack_from("=QqHB", data, position)
        if size < 20 or position + size > length or next_cookie < 0:
            raise SnapshotInvalid("Invalid raw cleanup directory entry")
        name = os.fsdecode(data[position + 19 : position + size].split(b"\0", 1)[0])
        result.append((name, next_cookie))
        position += size
    return result


def _eligible(descriptor: int, name: str, older_than: float | None = None) -> bool:
    try:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        return stat.S_ISDIR(info.st_mode) and (older_than is None or info.st_mtime < older_than)
    except OSError:
        return False


def _scan(
    store: LocalRawSnapshotStore,
    namespace: str,
    protected: frozenset[str],
    older_than: float,
    frames: list[dict[str, Any]],
) -> int:
    removed, scanned = 0, 0
    base = store.root / namespace
    while frames and scanned < _BUDGET:
        frame = frames[-1]
        path = base.joinpath(*frame["parts"])
        try:
            with directory_fd(path) as descriptor:
                info = os.fstat(descriptor)
                if (frame["device"], frame["inode"]) != (info.st_dev, info.st_ino):
                    frame.update(cookie=0, device=info.st_dev, inode=info.st_ino)
                entries = _entries(descriptor, frame["cookie"])
                if not entries:
                    frames.pop()
                    # Unique terminal/orphan job IDs are not reused. Active jobs
                    # are protected, and fresh directories are never swept.
                    if (
                        namespace == ".staging"
                        and len(frame["parts"]) == 2
                        and frame["parts"][-1] not in protected
                        and info.st_mtime < older_than
                    ):
                        try:
                            with directory_fd(path.parent) as parent:
                                os.rmdir(path.name, dir_fd=parent)
                        except OSError:
                            pass
                    continue
                for name, cookie in entries:
                    scanned += 1
                    frame["cookie"] = cookie
                    depth = len(frame["parts"])
                    if name in {".", ".."}:
                        pass
                    elif depth == 0:
                        if _TENANT.fullmatch(name) and _eligible(descriptor, name):
                            frames.append(_frame([name]))
                            break
                    elif namespace == ".staging" and depth == 1:
                        if (
                            _JOB.fullmatch(name)
                            and name not in protected
                            and _eligible(descriptor, name)
                        ):
                            frames.append(_frame([*frame["parts"], name]))
                            break
                    else:
                        match = _PUBLICATION.fullmatch(name) if namespace == "published" else None
                        recognized = (
                            match is not None and match[1] not in protected
                            if namespace == "published"
                            else _ATTEMPT.fullmatch(name) is not None
                        )
                        if recognized and _eligible(descriptor, name, older_than):
                            removed += int(store._discard_path(path / name, path))
                    if scanned >= _BUDGET:
                        break
        except OSError:
            # Removed, replaced or inaccessible parents cannot strand the cursor.
            frames.pop()
    return removed


def cleanup_namespace(
    store: LocalRawSnapshotStore,
    namespace: str,
    protected: frozenset[str],
    older_than: float,
) -> int:
    if namespace not in {".staging", "published"}:
        raise SnapshotInvalid("Invalid raw cleanup namespace")
    label = "staging" if namespace == ".staging" else "publication"
    filename = f".cleanup-{label}.json"
    with directory_fd(store.root) as root:
        lock = os.open(
            f".cleanup-{label}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root
        )
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise SnapshotInvalid("Invalid raw cleanup lock")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return 0
                raise
            frames = _read_frames(store.root, filename, namespace)
            removed = _scan(store, namespace, protected, older_than, frames)
            temporary = ".cleanup-" + uuid4().hex
            try:
                _write_file(
                    root, temporary, canonical_json({"version": 1, "frames": frames}).encode()
                )
                os.replace(temporary, filename, src_dir_fd=root, dst_dir_fd=root)
                os.fsync(root)
            finally:
                try:
                    os.unlink(temporary, dir_fd=root)
                except FileNotFoundError:
                    pass
            return removed
        finally:
            os.close(lock)
