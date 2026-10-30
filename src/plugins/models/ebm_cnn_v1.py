"""Pure CNN component of the EBM PoC replay recipe.

Each signal has its own autoencoder, shared across four pads. ``sigma`` is the
standard deviation across latent features, not a calibrated uncertainty. This
version deliberately exposes no Q/K, feedback or auxiliary-head configuration:
those paths do not participate in the reference notebook's neural computation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from torch import Tensor
    from torch.nn import Module

INPUT_SHAPE = (7, 4, 9, 31)
ARCHITECTURE_KEY = "ebm_cnn_v1"
ARCHITECTURE_VERSION = "1"


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


@dataclass(frozen=True)
class EBMModelConfig:
    """Serializable, bounded architecture parameters for the CPU replay slice."""

    channels: tuple[int, ...] = (16, 32)
    kernel_sizes: tuple[tuple[int, int], ...] = ((3, 3), (3, 3))
    strides: tuple[tuple[int, int], ...] = ((1, 1), (1, 1))
    features_per_pad: int = 4
    dropout: float = 0.1
    seed_size: tuple[int, int] = (4, 4)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.channels, tuple)
            or not 1 <= len(self.channels) <= 4
            or any(not _bounded_int(width, 1, 128) for width in self.channels)
        ):
            raise ValueError("channels must contain 1 to 4 integer widths in [1, 128]")
        for name, values, maximum in (
            ("kernel_sizes", self.kernel_sizes, 7),
            ("strides", self.strides, 4),
        ):
            if not isinstance(values, tuple) or len(values) != len(self.channels):
                raise ValueError(f"{name} must have one pair per CNN layer")
            for pair in values:
                if (
                    not isinstance(pair, tuple)
                    or len(pair) != 2
                    or any(not _bounded_int(value, 1, maximum) for value in pair)
                ):
                    raise ValueError(f"{name} pairs must contain integers in [1, {maximum}]")
        if not _bounded_int(self.features_per_pad, 1, 64):
            raise ValueError("features_per_pad must be an integer in [1, 64]")
        if (
            isinstance(self.dropout, bool)
            or not isinstance(self.dropout, (int, float))
            or not math.isfinite(self.dropout)
            or not 0 <= self.dropout < 1
        ):
            raise ValueError("dropout must be finite and in [0, 1)")
        if (
            not isinstance(self.seed_size, tuple)
            or len(self.seed_size) != 2
            or any(not _bounded_int(value, 1, 16) for value in self.seed_size)
        ):
            raise ValueError("seed_size must contain two integers in [1, 16]")

    def to_dict(self) -> dict[str, object]:
        """Return JSON-compatible constructor parameters, with explicit defaults."""
        result: dict[str, object] = asdict(self)
        result["channels"] = list(self.channels)
        result["kernel_sizes"] = [list(pair) for pair in self.kernel_sizes]
        result["strides"] = [list(pair) for pair in self.strides]
        result["seed_size"] = list(self.seed_size)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> EBMModelConfig:
        if not isinstance(value, Mapping):
            raise ValueError("model config must be a mapping")
        unknown = set(value) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unsupported EBM model configuration: {sorted(unknown, key=str)}")
        defaults = cls()

        def sequence(name: str) -> tuple[object, ...]:
            item = value.get(name, getattr(defaults, name))
            if not isinstance(item, (list, tuple)):
                raise ValueError(f"{name} must be an array")
            return tuple(item)

        def pairs(name: str) -> tuple[tuple[int, int], ...]:
            items = sequence(name)
            if any(not isinstance(item, (list, tuple)) for item in items):
                raise ValueError(f"{name} must be an array of pairs")
            return cast(
                tuple[tuple[int, int], ...],
                tuple(tuple(cast(list[object] | tuple[object, ...], item)) for item in items),
            )

        return cls(
            channels=cast(tuple[int, ...], sequence("channels")),
            kernel_sizes=pairs("kernel_sizes"),
            strides=pairs("strides"),
            features_per_pad=cast(int, value.get("features_per_pad", defaults.features_per_pad)),
            dropout=cast(float, value.get("dropout", defaults.dropout)),
            seed_size=cast(tuple[int, int], sequence("seed_size")),
        )


def build_ebm_cnn(config: EBMModelConfig | Mapping[str, object] | None = None) -> Module:
    """Build the approved CNN without importing Torch during module import.

    Inputs are finite float32 tensors shaped ``[N, 7, 4, 9, 31]``, already
    normalized with frozen training statistics. ``forward`` returns
    ``(mu, sigma, latent)``; ``autoencode`` also exposes the reconstruction for
    training. Model classes stay inside this factory so management roles need
    no Torch import. Serialize ``state_dict`` and this config, not the module.
    """
    if config is None:
        resolved = EBMModelConfig()
    elif isinstance(config, EBMModelConfig):
        resolved = config
    else:
        resolved = EBMModelConfig.from_dict(config)

    import torch
    import torch.nn as nn
    import torch.nn.functional as functional

    class PerSignalSharedCNNAutoencoder(nn.Module):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            layers: list[nn.Module] = []
            channels_in = 1
            for channels_out, kernel, stride in zip(
                resolved.channels, resolved.kernel_sizes, resolved.strides, strict=True
            ):
                padding = tuple(value // 2 for value in kernel)
                layers.extend(
                    [
                        nn.Conv2d(channels_in, channels_out, kernel, stride, padding),
                        nn.BatchNorm2d(channels_out),
                        nn.ReLU(),
                        nn.Dropout2d(resolved.dropout),
                    ]
                )
                channels_in = channels_out
            self.encoder_conv = nn.Sequential(*layers)
            self.channel_projection = nn.Conv2d(channels_in, 1, kernel_size=1)
            self.pool = nn.AdaptiveAvgPool2d((1, resolved.features_per_pad))
            self.decoder_channels = channels_in
            self.seed_h, self.seed_w = resolved.seed_size
            self.latent_to_seed = nn.Linear(
                resolved.features_per_pad, channels_in * self.seed_h * self.seed_w
            )
            decoder_mid = max(8, channels_in // 2)
            decoder_low = max(4, decoder_mid // 2)
            self.decoder_conv = nn.Sequential(
                nn.ConvTranspose2d(channels_in, decoder_mid, 4, 2, 1),
                nn.BatchNorm2d(decoder_mid),
                nn.ReLU(),
                nn.ConvTranspose2d(decoder_mid, decoder_low, 4, 2, 1),
                nn.BatchNorm2d(decoder_low),
                nn.ReLU(),
                nn.Conv2d(decoder_low, 1, 3, padding=1),
            )

        def encode(self, images: Tensor) -> Tensor:
            encoded = self.channel_projection(self.encoder_conv(images))
            return self.pool(encoded).flatten(1)

        def decode(self, latent: Tensor, output_size: tuple[int, int]) -> Tensor:
            seed = self.latent_to_seed(latent).reshape(
                len(latent), self.decoder_channels, self.seed_h, self.seed_w
            )
            reconstructed = self.decoder_conv(seed)
            if tuple(reconstructed.shape[-2:]) != tuple(output_size):
                reconstructed = functional.interpolate(
                    reconstructed, size=output_size, mode="bilinear", align_corners=False
                )
            return reconstructed

        def forward(self, images: Tensor) -> tuple[Tensor, Tensor]:
            latent = self.encode(images)
            return latent, self.decode(latent, (images.shape[-2], images.shape[-1]))

    class SpectrogramCNNAutoencoder(nn.Module):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.config = resolved
            self.signal_count, self.platen_count = INPUT_SHAPE[:2]
            self.signal_autoencoders = nn.ModuleList(
                PerSignalSharedCNNAutoencoder() for _ in range(self.signal_count)
            )
            self.latent_dim = self.signal_count * self.platen_count * resolved.features_per_pad

        def _validate(self, spectrogram_x: Tensor) -> None:
            if (
                not isinstance(spectrogram_x, torch.Tensor)
                or spectrogram_x.ndim != 5
                or spectrogram_x.shape[0] < 1
                or tuple(spectrogram_x.shape[1:]) != INPUT_SHAPE
            ):
                raise ValueError("Expected nonempty spectrogram tensor shaped [N, 7, 4, 9, 31]")
            if spectrogram_x.dtype != torch.float32:
                raise ValueError("Spectrogram tensor dtype must be float32")
            if not bool(torch.isfinite(spectrogram_x).all()):
                raise ValueError("Spectrogram tensor must contain only finite values")

        def encode(self, spectrogram_x: Tensor) -> Tensor:
            self._validate(spectrogram_x)
            batch_size = spectrogram_x.shape[0]
            pieces = []
            for signal_index, autoencoder in enumerate(self.signal_autoencoders):
                images = spectrogram_x[:, signal_index]
                images = images.reshape(batch_size * self.platen_count, 1, *INPUT_SHAPE[-2:])
                pieces.append(autoencoder.encode(images).reshape(batch_size, -1))
            return torch.cat(pieces, dim=1)

        def autoencode(self, spectrogram_x: Tensor) -> tuple[Tensor, Tensor]:
            self._validate(spectrogram_x)
            batch_size = spectrogram_x.shape[0]
            latent_pieces = []
            reconstructed_signals = []
            for signal_index, autoencoder in enumerate(self.signal_autoencoders):
                images = spectrogram_x[:, signal_index]
                flat_images = images.reshape(batch_size * self.platen_count, 1, *INPUT_SHAPE[-2:])
                compressed, reconstructed = autoencoder(flat_images)
                latent_pieces.append(compressed.reshape(batch_size, -1))
                reconstructed_signals.append(
                    reconstructed.reshape(batch_size, self.platen_count, *INPUT_SHAPE[-2:])
                )
            return torch.cat(latent_pieces, dim=1), torch.stack(reconstructed_signals, dim=1)

        def forward(self, spectrogram_x: Tensor) -> tuple[Tensor, Tensor, Tensor]:
            latent = self.encode(spectrogram_x)
            mu = latent.mean(dim=1)
            sigma = latent.std(dim=1, unbiased=False).clamp_min(1e-6)
            return mu, sigma, latent

    return SpectrogramCNNAutoencoder()
