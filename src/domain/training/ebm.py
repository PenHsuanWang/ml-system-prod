"""Framework-free, bounded parameters for the first EBM replay workload."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

from src.domain.ml import MLError

EBM_RECIPE_ID = "ebm_poc_replay_v1"
EBM_RECIPE_VERSION = "1.0.0"
EBM_NOTEBOOK_SHA256 = "6559bcd463a79b4b9697a7d3916e110851d02a05ea3bbbb8e96d4d9f0448fba9"


@dataclass(frozen=True)
class EBMTrainingConfig:
    epochs: int = 1
    batch_size: int = 32
    seed: int = 43
    learning_rate: float = 0.001
    weight_decay: float = 0.00001
    rank_group_size: int = 3
    pair_sample_size: int = 512
    cdf_anchor_weight: float = 1.0
    cdf_mean_corr_weight: float = 1.0
    wafer_rank_weight: float = 1.0
    reconstruction_weight: float = 1.0
    cdf_anchor_eps: float = 0.01
    wafer_rank_margin: float = 0.3
    risk_z: float = 1.645

    def __post_init__(self) -> None:
        integers = {"epochs": (1, 1000), "batch_size": (3, 256), "seed": (0, 2**31 - 1),
                    "rank_group_size": (2, 4), "pair_sample_size": (1, 16384)}
        for name, (low, high) in integers.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise MLError(f"Invalid EBM training parameter: {name}")
        ranges = {"learning_rate": (1e-8, 0.1), "weight_decay": (0, 1),
                  "cdf_anchor_weight": (0, 100), "cdf_mean_corr_weight": (0, 100),
                  "wafer_rank_weight": (0, 100), "reconstruction_weight": (0, 100),
                  "cdf_anchor_eps": (1e-6, 0.1), "wafer_rank_margin": (0, 10),
                  "risk_z": (0, 10)}
        for name, (lower, upper) in ranges.items():
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not lower <= value <= upper):
                raise MLError(f"Invalid EBM training parameter: {name}")
        if self.batch_size < self.rank_group_size or not any((self.cdf_anchor_weight,
                self.cdf_mean_corr_weight, self.wafer_rank_weight, self.reconstruction_weight)):
            raise MLError("Invalid EBM group size or empty training objective")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EBMTrainingConfig:
        if not isinstance(value, dict) or set(value) - {field.name for field in fields(cls)}:
            raise MLError("Unknown EBM training options (feedback/Aux/RUL are not supported)")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
