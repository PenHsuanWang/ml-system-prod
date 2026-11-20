"""Private setup files, literal dotenv updates and bounded rollback on write errors."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dotenv.parser import parse_stream

MAX_FILE = 1024 * 1024
_SINGLE_LITERAL = re.compile(r"\A\s*(?:export\s+)?(?:'[^']+'|[^\s=#]+)\s*=\s*'((?:\\'|[^'])*)'")


class SetupError(Exception):
    """Messages are fixed instructions and never contain supplied values."""


def absolute_path(value: str | Path) -> Path:
    if re.search(r"\$(?:\{|[A-Za-z_])", str(value)):
        raise SetupError("Use an explicit path; shell/environment references are not expanded.")
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SetupError("Parent traversal is not allowed.")
    return Path(os.path.abspath(path))


def no_links(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SetupError("Symlinks are not allowed in setup paths.")


def read_regular(path: Path, *, private: bool = False) -> bytes:
    no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_FILE
            or private
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600})
        ):
            raise SetupError("Expected an owner-only regular setup file (0400 or 0600).")
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
        if (
            len(data) > MAX_FILE
            or identity(after) != identity(info)
            or after.st_mode != info.st_mode
            or after.st_uid != info.st_uid
        ):
            raise SetupError("Setup input changed or exceeds its size limit.")
        return data


def env_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise SetupError("The existing environment file has invalid syntax.")
        if binding.key:
            if binding.key in values:
                raise SetupError("Duplicate environment keys must be resolved before setup.")
            literal = _SINGLE_LITERAL.match(binding.original.string)
            # Compose preserves backslashes in single quotes, unlike python-dotenv.
            values[binding.key] = (
                literal.group(1).replace("\\'", "'")
                if literal
                else (binding.value or "").replace("$$", "$")
            )
    return values


def env_quote(value: str) -> str:
    if any(c in value for c in "\x00\r\n"):
        raise SetupError("Environment values must be single-line strings.")
    if value.endswith("\\") or ("\\" in value and "'" in value):
        # A closing single quote after a backslash is ambiguous to Compose.
        return json.dumps(value.replace("$", "$$"), ensure_ascii=False)
    return "'" + value.replace("'", "\\'") + "'"


def referenced_keys(text: str) -> set[str]:
    return {
        binding.key
        for binding in parse_stream(io.StringIO(text))
        if binding.key
        and not _SINGLE_LITERAL.match(binding.original.string)
        and re.search(r"\$(?:\{|[A-Za-z_])", (binding.value or "").replace("$$", ""))
    }


def merge_env(text: str, updates: dict[str, str]) -> str:
    env_values(text)
    remaining = dict(updates)
    lines = []
    for binding in parse_stream(io.StringIO(text)):
        if binding.key in remaining:
            lines.append(f"{binding.key}={env_quote(remaining.pop(binding.key))}\n")
        else:
            lines.append(binding.original.string)
    result = "".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    for key, value in remaining.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise SetupError("Invalid managed environment key.")
        result += f"{key}={env_quote(value)}\n"
    return result


@dataclass(repr=False)
class Snapshot:
    data: bytes = field(repr=False)
    identity: tuple[int, int, int, int, int]


def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class PrivateFiles:
    """Capture inputs before prompting; publish only after all overwrite decisions."""

    def __init__(self, project: Path) -> None:
        if shutil.which("git") is None:
            raise SetupError(
                "Git is required to verify that private outputs are untracked and ignored."
            )
        self.project = absolute_path(project)
        self.originals: dict[Path, Snapshot | None] = {}
        self.created_directories: list[Path] = []

    def guard(self, path: Path) -> None:
        no_links(path)
        ancestor = path.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        result = subprocess.run(
            ["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            if path.is_relative_to(self.project):
                raise SetupError("Run setup in a Git checkout with the setup ignore rules.")
            return
        root = Path(result.stdout.strip())
        relative = path.relative_to(root).as_posix()
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
            capture_output=True,
            check=False,
        )
        ignored = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--quiet", "--", relative],
            capture_output=True,
            check=False,
        )
        if tracked.returncode == 0 or ignored.returncode != 0:
            raise SetupError("Private outputs and backups must be untracked and Git-ignored.")

    def read(self, path: Path) -> bytes | None:
        path = absolute_path(path)
        self.guard(path)
        if any(path.parent.glob(path.name + ".init-*")):
            raise SetupError(
                "Unfinished setup staging exists; review private recovery files first."
            )
        if path not in self.originals:
            try:
                data = read_regular(path, private=True)
                self.originals[path] = Snapshot(data, identity(path.stat()))
            except FileNotFoundError:
                self.originals[path] = None
        original = self.originals[path]
        return original.data if original else None

    def _directory(self, path: Path) -> int:
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            no_links(directory.parent)
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
            self.created_directories.append(directory)
        no_links(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(descriptor)
            raise SetupError(
                "Output directories must be owned by this user and not writable by others."
            )
        return descriptor

    def _unchanged(self, path: Path) -> None:
        self.guard(path)
        original = self.originals[path]
        if original is None:
            if path.exists():
                raise SetupError("A setup destination appeared after the overwrite decision.")
        elif (
            identity(path.stat()) != original.identity
            or hashlib.sha256(read_regular(path, private=True)).digest()
            != hashlib.sha256(original.data).digest()
        ):
            raise SetupError("A setup destination changed after it was read.")

    def _stage(self, descriptor: int, data: bytes, path: Path, kind: str = "new") -> str:
        name = path.name + ".init-" + kind + "-" + uuid.uuid4().hex
        self.guard(path.with_name(name))
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name, dir_fd=descriptor)
            raise
        return name

    def write(
        self,
        outputs: dict[Path, bytes],
        choices: dict[Path, str],
        directories: tuple[Path, ...] = (),
    ) -> list[Path]:
        if set(choices) != set(outputs) or any(
            c not in {"overwrite", "backup"} for c in choices.values()
        ):
            raise SetupError("Every output requires an explicit write decision.")
        parents: dict[Path, int] = {}
        parent_directories: dict[Path, int] = {}
        staged: dict[Path, str] = {}
        recovery: dict[Path, str] = {}
        published: list[Path] = []
        backups: list[Path] = []
        committed = False
        rollback_failed = False
        self.created_directories = []
        try:
            for path in outputs:
                if path not in self.originals:
                    raise SetupError("Output was not captured before confirmation.")
                self._unchanged(path)
            for directory in directories:
                os.close(self._directory(directory))
            for path, data in outputs.items():
                if len(data) > MAX_FILE:
                    raise SetupError("Generated setup file exceeds its size limit.")
                if path.parent not in parent_directories:
                    descriptor = self._directory(path.parent)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        os.close(descriptor)
                        raise SetupError(
                            "Another setup write is active; retry after it completes."
                        ) from None
                    parent_directories[path.parent] = descriptor
                parents[path] = parent_directories[path.parent]
                staged[path] = self._stage(parents[path], data, path)
                original = self.originals[path]
                if original:
                    recovery[path] = self._stage(parents[path], original.data, path, "old")
                os.fsync(parents[path])
            for path in outputs:
                self._unchanged(path)
                original = self.originals[path]
                if original and choices[path] == "backup":
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                    backup = path.with_name(path.name + ".bak-" + stamp + "-" + uuid.uuid4().hex)
                    self.guard(backup)
                    # Stage separate bytes so rollback can restore a single-link file.
                    backup_source = self._stage(parents[path], original.data, path, "backup")
                    try:
                        os.link(
                            backup_source,
                            backup.name,
                            src_dir_fd=parents[path],
                            dst_dir_fd=parents[path],
                            follow_symlinks=False,
                        )
                    finally:
                        os.unlink(backup_source, dir_fd=parents[path])
                    backups.append(backup)
                os.replace(
                    staged[path], path.name, src_dir_fd=parents[path], dst_dir_fd=parents[path]
                )
                del staged[path]
                published.append(path)
                os.fsync(parents[path])
            committed = True
            return backups
        except BaseException as error:
            for path in reversed(published):
                try:
                    original = self.originals[path]
                    if original:
                        os.replace(
                            recovery[path],
                            path.name,
                            src_dir_fd=parents[path],
                            dst_dir_fd=parents[path],
                        )
                        del recovery[path]
                    else:
                        os.unlink(path.name, dir_fd=parents[path])
                    os.fsync(parents[path])
                except OSError:
                    rollback_failed = True
            if rollback_failed:
                raise SetupError(
                    "Rollback incomplete; retain private files and backups for recovery."
                ) from None
            raise error
        finally:
            for path, name in [*staged.items(), *(recovery.items() if not rollback_failed else [])]:
                try:
                    os.unlink(name, dir_fd=parents[path])
                except FileNotFoundError:
                    pass
            for descriptor in parent_directories.values():
                os.close(descriptor)
            for directory in reversed(self.created_directories) if not committed else []:
                try:
                    directory.rmdir()
                except OSError:
                    pass
