"""Immutable, strictly validated two-feature dataset snapshots for the CPU MVP."""

from dataclasses import dataclass
import math
from typing import Any

from src.domain.ml import MLError


@dataclass(frozen=True)
class DatasetVersion:
    dataset_id: str
    version: str
    tenant_id: str
    content_digest: str
    snapshot_ref: str
    summary_json: str


@dataclass(frozen=True)
class DatasetSnapshot:
    features: tuple[tuple[float, float], ...]
    labels: tuple[int, ...]
    train: tuple[int, ...]
    validation: tuple[int, ...]
    test: tuple[int, ...]

    @classmethod
    def parse(cls, value: dict[str, Any]) -> "DatasetSnapshot":
        if (not isinstance(value, dict) or set(value) != {"features", "labels", "splits"} or
                not isinstance(value["splits"], dict) or set(value["splits"]) != {"train", "validation", "test"}):
            raise MLError(
                "Dataset must contain features, labels and fixed train/validation/test splits")
        raw, labels = value["features"], value["labels"]
        if not isinstance(raw, list) or not isinstance(labels, list) or not 6 <= len(raw) <= 100000 or len(labels) != len(raw):
            raise MLError("Dataset sample count is invalid")
        if any(not isinstance(row, list) or len(row) != 2 or any(
                isinstance(number, bool) or not isinstance(number, (int, float)) or
                abs(number) > 100 or not math.isfinite(number) for number in row) for row in raw):
            raise MLError(
                "Dataset features must be finite two-dimensional values within [-100,100]")
        if any(type(label) is not int or label not in {0, 1} for label in labels):
            raise MLError("Dataset labels must be 0 or 1")
        splits = value["splits"]
        combined = []
        for name in ("train", "validation", "test"):
            indices = splits[name]
            if not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices):
                raise MLError("Dataset split indices must be nonempty integer lists")
            combined.extend(indices)
        if len(combined) != len(raw) or set(combined) != set(range(len(raw))):
            raise MLError("Dataset splits must be disjoint and cover each sample exactly once")
        if len({labels[i] for i in splits["train"]}) != 2:
            raise MLError("Training split must contain both classes")
        return cls(tuple((float(row[0]), float(row[1])) for row in raw), tuple(labels),
                   tuple(splits["train"]), tuple(splits["validation"]), tuple(splits["test"]))
