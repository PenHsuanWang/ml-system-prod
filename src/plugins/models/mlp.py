"""Approved CPU classifier; no dynamic architecture imports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from torch.nn import Module


def build_mlp(config: dict[str, object]) -> Module:
    import torch.nn as nn
    if (config.get("input_dim") != 2 or config.get("output_dim") != 2 or
            config.get("activation") != "relu"):
        raise ValueError("Unapproved MLP shape or activation")
    hidden = config.get("hidden_dims")
    if not isinstance(hidden, list) or not 1 <= len(hidden) <= 3 or any(
        not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 128 for n in hidden
    ):
        raise ValueError("Unapproved hidden layer configuration")
    layers = []
    size = 2
    for width in hidden:
        layers.extend([nn.Linear(size, width), nn.ReLU()])
        size = width
    layers.append(nn.Linear(size, 2))
    return nn.Sequential(*layers)
