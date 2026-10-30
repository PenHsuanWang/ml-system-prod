"""PoC replay batches: shuffle within recipe/segment, then shuffle batches."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


def grouped_rank_batches(
    recipe_codes: Sequence[str | int],
    segment_codes: Sequence[str | int],
    *,
    batch_size: int,
    min_group_size: int = 3,
    rng: np.random.Generator,
) -> list[NDArray[np.int64]]:
    """Return row indices without crossing recipe/four-pad segment boundaries.

    Like the PoC, a final group fragment smaller than ``min_group_size`` is
    dropped. Failure to form any batch is an input error, never a successful
    zero-step training epoch. Passing the generator makes resume reproducible.
    """
    import numpy as np

    if (
        not isinstance(min_group_size, int)
        or isinstance(min_group_size, bool)
        or min_group_size not in (2, 3, 4)
    ):
        raise ValueError("min_group_size must be 2, 3, or 4")
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < min_group_size
    ):
        raise ValueError("batch_size must be an integer at least min_group_size")
    if not isinstance(rng, np.random.Generator):
        raise ValueError("rng must be a NumPy Generator")
    recipes = np.asarray(recipe_codes)
    segments = np.asarray(segment_codes)
    if recipes.ndim != 1 or segments.shape != recipes.shape or len(recipes) == 0:
        raise ValueError("recipe_codes and segment_codes must be nonempty matching vectors")
    groups: dict[tuple[str | int, str | int], list[int]] = {}
    for index, (recipe, segment) in enumerate(zip(recipes.tolist(), segments.tolist(), strict=True)):
        if any(
            isinstance(code, bool)
            or not isinstance(code, (str, int))
            or (isinstance(code, str) and not code.strip())
            for code in (recipe, segment)
        ):
            raise ValueError("group codes must be nonempty strings or integer identifiers")
        groups.setdefault((recipe, segment), []).append(index)
    batches = []
    for indices in groups.values():
        shuffled = np.asarray(indices, dtype=np.int64)
        rng.shuffle(shuffled)
        for start in range(0, len(shuffled), batch_size):
            batch = shuffled[start : start + batch_size]
            if len(batch) >= min_group_size:
                batches.append(batch)
    rng.shuffle(batches)
    if not batches:
        raise ValueError("No recipe/segment group has enough rows for a ranking batch")
    return batches
