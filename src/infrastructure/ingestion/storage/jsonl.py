"""Bounded validation of raw UTF-8 JSON object records; no feature preparation."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from typing import Any

from src.domain.ingestion.errors import SnapshotInvalid
from src.domain.ingestion.models import ExtractionLimits


def object_record(line: bytes) -> dict[str, Any]:
    """Parse one complete JSONL record and reject ambiguous/non-finite JSON."""

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate object key")
            result[key] = value
        return result

    def finite(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite number")
        return result

    def invalid(value: str) -> None:
        raise ValueError("Non-finite JSON constant")

    try:
        if not line.strip():
            raise ValueError("Blank record")
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=finite,
            parse_constant=invalid,
        )
        if not isinstance(value, dict):
            raise ValueError("A raw JSONL record must be an object")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise SnapshotInvalid("Invalid raw JSONL object record") from error


def bounded_lines(chunks: Iterable[bytes], limits: ExtractionLimits) -> Iterator[bytes]:
    """Yield exact input lines while bounding file, line and record memory budgets."""
    pending = bytearray()
    total, records = 0, 0
    for chunk in chunks:
        if not isinstance(chunk, bytes) or len(chunk) > limits.max_line_bytes + 1:
            raise SnapshotInvalid("Invalid or oversized raw staging chunk")
        total += len(chunk)
        if total > limits.max_bytes:
            raise SnapshotInvalid("Raw input exceeds its approved byte limit")
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = end + 1 if end >= 0 else len(chunk)
            pending.extend(chunk[start:stop])
            if len(pending) > limits.max_line_bytes:
                raise SnapshotInvalid("Raw input exceeds its approved line limit")
            if end >= 0:
                records += 1
                if records > limits.max_records:
                    raise SnapshotInvalid("Raw input exceeds its approved record limit")
                line = bytes(pending)
                object_record(line)
                yield line
                pending.clear()
            start = stop
    if pending:
        records += 1
        if records > limits.max_records:
            raise SnapshotInvalid("Raw input exceeds its approved record limit")
        line = bytes(pending)
        object_record(line)
        yield line
    if not records:
        raise SnapshotInvalid("Empty raw JSONL input is not accepted")
