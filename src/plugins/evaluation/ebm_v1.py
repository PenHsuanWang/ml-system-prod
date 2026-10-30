"""Evaluate raw EBM neural outputs without assuming calibrated Energy or RUL.

Metrics describe the selected retrospective samples. They confer no approval
unless a caller supplies a separately reviewed, versioned quality policy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeGuard

from src.domain.datasets.ebm import EBMDatasetMetadata

SCORE_KIND = "uncalibrated_latent_mean_plus_std"
LOT_CORRELATION_METRIC = "lot_mean_raw_score_cdf_pearson"


def _finite_number(value: Any) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2:
        return None
    centered = []
    for values in (left, right):
        # Scaling before centering also handles very large, still finite outputs.
        scale = max(abs(value) for value in values)
        if scale == 0:
            return None
        scaled = [value / scale for value in values]
        mean = math.fsum(scaled) / len(scaled)
        centered.append([value - mean for value in scaled])
    x, y = centered
    x_squares = math.fsum(value * value for value in x)
    y_squares = math.fsum(value * value for value in y)
    if x_squares == 0 or y_squares == 0:
        return None
    denominator = math.sqrt(x_squares) * math.sqrt(y_squares)
    correlation = math.fsum(a * b for a, b in zip(x, y)) / denominator
    if not math.isfinite(correlation):
        raise ValueError("EBM correlation calculation produced a non-finite result")
    return max(-1.0, min(1.0, correlation))


def evaluate_neural_outputs(
    predictions: Sequence[Mapping[str, Any]],
    metadata: EBMDatasetMetadata,
    indices: Sequence[int],
    *,
    cdf_anchor_eps: float = 0.01,
) -> dict[str, float | int | None]:
    """Evaluate predictions aligned exactly with ``metadata.samples[indices]``.

    Anchor loss is Smooth L1 (beta=1) over every latent coordinate and sample,
    retaining the PoC's wafer weighting. Pearson instead gives each distinct Lot
    one vote after averaging its raw neural scores and CDF targets. An undefined
    correlation is ``None`` rather than zero or a fabricated passing value.
    """
    selected = tuple(indices)
    if (
        not isinstance(metadata, EBMDatasetMetadata)
        or not selected
        or len(predictions) != len(selected)
        or any(
            type(index) is not int or index < 0 or index >= len(metadata.samples)
            for index in selected
        )
        or len(set(selected)) != len(selected)
    ):
        raise ValueError("EBM predictions must align with unique valid sample indices")
    if not _finite_number(cdf_anchor_eps) or not 1e-6 <= cdf_anchor_eps <= 0.1:
        raise ValueError("EBM CDF anchor epsilon must be within [1e-6,0.1]")

    anchor_means = []
    lot_values: dict[str, list[tuple[float, float]]] = {}
    latent_dim = None
    for prediction, index in zip(predictions, selected):
        sample = metadata.samples[index]
        if (
            not isinstance(prediction, Mapping)
            or prediction.get("score_kind") != SCORE_KIND
            or any(not _finite_number(prediction.get(key)) for key in ("mu", "sigma", "raw_nn_score"))
            or prediction["sigma"] <= 0
        ):
            raise ValueError("Invalid EBM neural prediction contract")
        latent = prediction.get("latent")
        if (
            not isinstance(latent, (list, tuple))
            or not latent
            or any(not _finite_number(value) for value in latent)
            or latent_dim is not None and len(latent) != latent_dim
        ):
            raise ValueError("EBM predictions require finite, consistently sized latent vectors")
        latent_dim = len(latent)
        cdf = sample.cdf_target
        if not _finite_number(cdf) or not 0 <= cdf <= 1:
            raise ValueError("EBM evaluation requires finite CDF targets in [0,1]")
        clipped = max(cdf_anchor_eps, min(1.0 - cdf_anchor_eps, cdf))
        target = math.log(clipped / (1.0 - clipped))
        distances = [abs(float(value) - target) for value in latent]
        # Divide each term before summing to avoid overflow for finite outputs.
        anchor_means.append(math.fsum(
            (0.5 * distance * distance if distance < 1 else distance - 0.5) / latent_dim
            for distance in distances
        ))
        lot_values.setdefault(sample.lot_id, []).append((float(prediction["raw_nn_score"]), cdf))

    anchor_loss = math.fsum(value / len(selected) for value in anchor_means)
    if not math.isfinite(anchor_loss):
        raise ValueError("EBM anchor evaluation produced a non-finite result")
    lot_scores, lot_cdfs = [], []
    for lot_id in sorted(lot_values):
        values = lot_values[lot_id]
        lot_scores.append(math.fsum(score / len(values) for score, _ in values))
        lot_cdfs.append(math.fsum(cdf / len(values) for _, cdf in values))
    return {
        "cdf_anchor_smooth_l1": anchor_loss,
        LOT_CORRELATION_METRIC: _pearson(lot_scores, lot_cdfs),
        "samples": len(selected),
        "lots": len(lot_values),
    }


@dataclass(frozen=True)
class EBMQualityPolicy:
    """Explicit caller-owned thresholds; no implicit product-quality defaults."""

    policy_version: str
    max_anchor_loss: float
    minimum_lot_correlation: float
    minimum_lots: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_version, str)
            or not self.policy_version.strip()
            or self.policy_version != self.policy_version.strip()
            or len(self.policy_version) > 128
            or not _finite_number(self.max_anchor_loss)
            or self.max_anchor_loss < 0
            or not _finite_number(self.minimum_lot_correlation)
            or not -1 <= self.minimum_lot_correlation <= 1
            or type(self.minimum_lots) is not int
            or self.minimum_lots < 2
        ):
            raise ValueError("EBM quality policy requires a version and explicit valid thresholds")

    def decide(self, metrics: Mapping[str, Any]) -> str:
        loss = metrics.get("cdf_anchor_smooth_l1")
        correlation = metrics.get(LOT_CORRELATION_METRIC)
        samples, lots = metrics.get("samples"), metrics.get("lots")
        if (
            not _finite_number(loss)
            or not 0 <= loss <= self.max_anchor_loss
            or not _finite_number(correlation)
            or not self.minimum_lot_correlation <= correlation <= 1
            or type(lots) is not int
            or lots < self.minimum_lots
            or type(samples) is not int
            or samples < lots
        ):
            return "REJECTED_BY_POLICY"
        return "APPROVED_BY_POLICY"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EBMQualityPolicy:
        fields = {"policy_version", "max_anchor_loss", "minimum_lot_correlation", "minimum_lots"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("A complete explicit EBM quality policy is required")
        return cls(**dict(value))
