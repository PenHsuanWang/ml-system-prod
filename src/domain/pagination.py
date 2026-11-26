"""Bounded collection windows shared by metadata discovery operations."""

import math
from dataclasses import dataclass

from src.domain.ml import SchemaMismatch


@dataclass(frozen=True)
class CollectionWindow:
    limit: int = 25
    offset: int = 0
    created_after: float | None = None
    created_before: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or type(self.offset) is not int
            or not 0 <= self.offset <= 1_000_000
        ):
            raise SchemaMismatch("Invalid collection page bounds")
        for value in (self.created_after, self.created_before):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise SchemaMismatch("Time bounds must be finite nonnegative UTC Unix seconds")
        if (
            self.created_after is not None
            and self.created_before is not None
            and self.created_after >= self.created_before
        ):
            raise SchemaMismatch("created_after must precede created_before")
