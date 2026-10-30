"""Differentiable losses used by the notebook's active CNN replay path.

No RUL, auxiliary head or feedback loss is implied by this module. The caller
owns coefficients and combines these terms with autoencoder reconstruction MSE.
Torch is imported only when evaluating a loss.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from torch import Generator, Tensor


def _validate_features(features: Tensor) -> None:
    import torch

    if (
        not isinstance(features, torch.Tensor)
        or features.ndim != 2
        or min(features.shape) < 1
        or not features.is_floating_point()
        or not bool(torch.isfinite(features).all())
    ):
        raise ValueError("features must be a nonempty finite floating tensor of shape [N, D]")


def _validate_cdf(features: Tensor, cdf: Tensor) -> None:
    import torch

    if (
        not isinstance(cdf, torch.Tensor)
        or cdf.shape != (len(features),)
        or cdf.device != features.device
        or not cdf.is_floating_point()
        or not bool(torch.isfinite(cdf).all())
        or bool(((cdf < 0) | (cdf > 1)).any())
    ):
        raise ValueError("cdf must be a finite [N] tensor in [0, 1] on the features device")


def _validate_codes(features: Tensor, codes: Tensor, name: str) -> None:
    import torch

    if (
        not isinstance(codes, torch.Tensor)
        or codes.shape != (len(features),)
        or codes.dtype != torch.int64
        or codes.device != features.device
    ):
        raise ValueError(f"{name} must be an int64 [N] tensor on the features device")


def cdf_anchor_loss(features: Tensor, cdf: Tensor, eps: float = 0.01) -> Tensor:
    """Smooth-L1 anchor of every latent coordinate to the clamped CDF logit."""
    import torch
    import torch.nn.functional as functional

    _validate_features(features)
    _validate_cdf(features, cdf)
    if isinstance(eps, bool) or not isinstance(eps, (int, float)) or not 0 < eps < 0.5:
        raise ValueError("eps must be finite and strictly between 0 and 0.5")
    bounded = cdf.clamp(eps, 1.0 - eps)
    target_logit = torch.log(bounded / (1.0 - bounded)).unsqueeze(1)
    return functional.smooth_l1_loss(features, target_logit.expand_as(features))


def lot_mean_correlation_loss(features: Tensor, cdf: Tensor, lot_codes: Tensor) -> Tensor:
    """One minus mean per-feature correlation across per-lot batch means."""
    import torch

    _validate_features(features)
    _validate_cdf(features, cdf)
    _validate_codes(features, lot_codes, "lot_codes")
    unique_lots, inverse = torch.unique(lot_codes, sorted=False, return_inverse=True)
    lot_count = int(unique_lots.numel())
    if lot_count < 3:
        return (features * 0.0).mean()
    counts = torch.bincount(inverse, minlength=lot_count).to(dtype=features.dtype)
    feature_sum = features.new_zeros((lot_count, features.shape[1]))
    feature_sum.index_add_(0, inverse, features)
    lot_features = feature_sum / counts.unsqueeze(1).clamp_min(1.0)
    cdf_sum = features.new_zeros(lot_count)
    cdf_sum.index_add_(0, inverse, cdf.to(dtype=features.dtype))
    lot_cdf = cdf_sum / counts.clamp_min(1.0)
    centered_features = lot_features - lot_features.mean(dim=0, keepdim=True)
    centered_cdf = lot_cdf - lot_cdf.mean()
    cdf_variance = centered_cdf.square().mean()
    if float(cdf_variance.detach()) <= 1e-12:
        return (features * 0.0).mean()
    covariance = (centered_features * centered_cdf.unsqueeze(1)).mean(dim=0)
    feature_variance = centered_features.square().mean(dim=0)
    denominator = torch.sqrt(feature_variance.clamp_min(1e-12) * cdf_variance.clamp_min(1e-12))
    feature_corr = (covariance / denominator.clamp_min(1e-8)).clamp(-1.0, 1.0)
    return 1.0 - feature_corr.mean()


def wafer_count_rank_loss(
    features: Tensor,
    counts: Tensor,
    lot_codes: Tensor,
    recipe_codes: Tensor,
    segment_codes: Tensor,
    *,
    generator: Generator,
    margin: float = 0.3,
    pair_sample_size: int = 512,
    rank_group_size: int = 3,
) -> Tensor:
    """Rank sampled different-lot pairs dominated across all four pad counts.

    Only pairs in the same recipe and four-pad segment contribute. Equal or
    mixed-direction pad counts are not ordered. No valid pair gives a connected
    zero so callers can safely backpropagate the aggregate loss.
    """
    import torch
    import torch.nn.functional as functional

    _validate_features(features)
    for name, codes in (
        ("lot_codes", lot_codes),
        ("recipe_codes", recipe_codes),
        ("segment_codes", segment_codes),
    ):
        _validate_codes(features, codes, name)
    if (
        not isinstance(counts, torch.Tensor)
        or counts.shape != (len(features), 4)
        or counts.device != features.device
        or not bool(torch.isfinite(counts).all())
        or bool((counts < 0).any())
    ):
        raise ValueError("counts must be a finite nonnegative [N, 4] tensor on the features device")
    if (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(margin)
        or margin < 0
    ):
        raise ValueError("margin must be finite and nonnegative")
    if (
        not isinstance(pair_sample_size, int)
        or isinstance(pair_sample_size, bool)
        or pair_sample_size < 1
    ):
        raise ValueError("pair_sample_size must be a positive integer")
    if (
        not isinstance(rank_group_size, int)
        or isinstance(rank_group_size, bool)
        or rank_group_size not in (2, 3, 4)
    ):
        raise ValueError("rank_group_size must be 2, 3, or 4")
    if not isinstance(generator, torch.Generator) or generator.device != features.device:
        raise ValueError("generator must be a Torch Generator on the features device")
    groups = torch.randint(
        0, len(features), (pair_sample_size, rank_group_size),
        device=features.device, generator=generator,
    )
    positions = torch.triu_indices(rank_group_size, rank_group_size, offset=1, device=features.device)
    i = groups[:, positions[0]].reshape(-1)
    j = groups[:, positions[1]].reshape(-1)
    i_dominates = torch.all(counts[i] > counts[j], dim=1)
    j_dominates = torch.all(counts[j] > counts[i], dim=1)
    valid = (
        (lot_codes[i] != lot_codes[j])
        & (recipe_codes[i] == recipe_codes[j])
        & (segment_codes[i] == segment_codes[j])
        & (i_dominates | j_dominates)
    )
    if not bool(valid.any()):
        return (features * 0.0).mean()
    sign = torch.where(
        i_dominates[valid],
        torch.ones_like(i_dominates[valid], dtype=features.dtype),
        -torch.ones_like(i_dominates[valid], dtype=features.dtype),
    ).unsqueeze(1)
    difference = (features[i[valid]] - features[j[valid]]) * sign
    return functional.relu(margin - difference).mean()
