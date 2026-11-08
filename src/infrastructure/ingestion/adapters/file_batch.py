"""Approved local-file acquisition without path-based traversal or transformations."""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from src.domain.ingestion.errors import (
    AcquisitionFailed,
    IngestionError,
    SourceChanged,
    SourceRejected,
)
from src.domain.ingestion.models import (
    BatchExtraction,
    SourceDefinition,
    StagedBatch,
    identifier,
    sha256,
)
from src.infrastructure.ingestion.storage.raw_snapshot_writer import (
    CheckControl,
    _file_identity,
    relative_file_fd,
    relative_parts,
)


class BatchStagerPort(Protocol):
    def stage(
        self, extraction: BatchExtraction, chunks: Iterable[bytes], control: CheckControl
    ) -> StagedBatch: ...
    def discard(self, staged: StagedBatch) -> None: ...


class FileBatchAdapter:
    def __init__(self, approved_roots: Mapping[str, Path], stager: BatchStagerPort) -> None:
        roots = {}
        for key, path in approved_roots.items():
            identifier(key)
            root = path.absolute()
            if ".." in root.parts:
                raise SourceRejected("Invalid approved file root")
            roots[key] = root
        self._roots = MappingProxyType(roots)
        self._stager = stager

    def validate_source(self, source: SourceDefinition) -> None:
        if (
            source.adapter_type != "file"
            or source.adapter_version != "1"
            or source.data_format != "jsonl"
            or source.schema_version != "raw-json-object-v1"
        ):
            raise SourceRejected("Unsupported approved file source contract")
        try:
            config = json.loads(source.configuration_json)
            if (
                not isinstance(config, dict)
                or not {"root_key", "relative_path"} <= set(config)
                or set(config) - {"root_key", "relative_path", "expected_sha256"}
                or not isinstance(config["root_key"], str)
                or config["root_key"] not in self._roots
            ):
                raise ValueError("Invalid root reference")
            relative_parts(config["relative_path"])
            if "expected_sha256" in config:
                sha256(config["expected_sha256"])
        except (ValueError, TypeError, KeyError, IngestionError) as error:
            raise SourceRejected("Invalid approved file source configuration") from error

    def acquire(self, extraction: BatchExtraction, control: CheckControl) -> StagedBatch:
        self.validate_source(extraction.source)
        config = json.loads(extraction.source.configuration_json)
        staged = None
        try:
            control.check()
            with relative_file_fd(
                self._roots[config["root_key"]], config["relative_path"]
            ) as descriptor:
                before = os.fstat(descriptor)
                if not 0 < before.st_size <= extraction.limits.max_bytes:
                    raise SourceRejected(
                        "Approved file is empty or exceeds the extraction byte limit"
                    )

                def chunks() -> Iterator[bytes]:
                    total = 0
                    while True:
                        control.check()
                        chunk = os.read(descriptor, min(65536, extraction.limits.max_line_bytes))
                        control.check()
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > extraction.limits.max_bytes:
                            raise SourceChanged("Approved source file grew during extraction")
                        yield chunk
                    after = os.fstat(descriptor)
                    if _file_identity(before) != _file_identity(after) or total != before.st_size:
                        raise SourceChanged("Approved source file changed during extraction")

                staged = self._stager.stage(extraction, chunks(), control)
            expected = config.get("expected_sha256")
            if expected is not None and staged.content_digest != expected:
                self._stager.discard(staged)
                raise SourceChanged("Approved source content revision does not match")
            return staged
        except OSError as error:
            if staged is not None:
                self._stager.discard(staged)
            if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                raise SourceRejected("Approved file source is not accessible") from error
            raise AcquisitionFailed("Approved file source could not be acquired") from error
