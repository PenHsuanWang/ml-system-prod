"""Pure numerical replay of the PoC's fixed FDC spectrogram and normalization.

Traces arrive sampled at 1 Hz, after source-specific segment selection and
interpolation. This module does not query data sources or infer signal identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


PAD_ORDER = ("P1", "P2", "P3", "P4")
FEATURE_SHAPE = (7, 4, 9, 31)


def _orders(
    signal_order: Sequence[str], pad_order: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    signals, pads = tuple(signal_order), tuple(pad_order)
    if (
        len(signals) != 7
        or any(not isinstance(name, str) or not name.strip() for name in signals)
        or len(set(signals)) != 7
        or pads != PAD_ORDER
    ):
        raise ValueError("Expected seven unique named signals and Pad order P1, P2, P3, P4")
    return signals, pads


def _features(value: Any) -> NDArray[np.float32]:
    import numpy as np

    raw = np.asarray(value)
    if raw.dtype.kind not in "fiu":
        raise ValueError("Spectrogram features must be real numeric values")
    values = raw.astype(np.float32)
    if (
        values.ndim != 5
        or values.shape[0] < 1
        or values.shape[1:] != FEATURE_SHAPE
        or not np.isfinite(values).all()
    ):
        raise ValueError("Expected finite spectrogram features shaped [N,7,4,9,31]")
    return values


@dataclass(frozen=True)
class SpectrogramConfig:
    fixed_seconds: int = 256
    sample_rate_hz: float = 1.0
    nfft: int = 16
    noverlap: int = 8
    log1p: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.fixed_seconds) is not int
            or type(self.nfft) is not int
            or type(self.noverlap) is not int
            or isinstance(self.sample_rate_hz, bool)
            or (self.fixed_seconds, self.sample_rate_hz, self.nfft, self.noverlap)
            != (256, 1.0, 16, 8)
            or type(self.log1p) is not bool
        ):
            raise ValueError("FDC spectrogram v1 requires 256 seconds, 1 Hz, NFFT 16 and overlap 8")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "fdc_spectrogram_v1",
            "fixed_seconds": self.fixed_seconds,
            "sample_rate_hz": self.sample_rate_hz,
            "nperseg": self.nfft,
            "nfft": self.nfft,
            "noverlap": self.noverlap,
            "window": "hann",
            "detrend": "constant",
            "scaling": "spectrum",
            "mode": "magnitude",
            "return_onesided": True,
            "log1p": self.log1p,
            "trace_policy": "keep_tail_left_pad_and_replace_nonfinite_with_finite_median",
        }

    @classmethod
    def from_dict(cls, state: Mapping[str, Any]) -> SpectrogramConfig:
        try:
            config = cls(
                fixed_seconds=state["fixed_seconds"],
                sample_rate_hz=state["sample_rate_hz"],
                nfft=state["nfft"],
                noverlap=state["noverlap"],
                log1p=state["log1p"],
            )
        except (KeyError, TypeError) as error:
            raise ValueError("Incomplete FDC spectrogram configuration") from error
        if dict(state) != config.to_dict():
            raise ValueError("Unsupported FDC spectrogram configuration contract")
        return config


class FdcSpectrogramV1:
    def __init__(
        self,
        signal_order: Sequence[str],
        *,
        pad_order: Sequence[str] = PAD_ORDER,
        config: SpectrogramConfig | None = None,
    ) -> None:
        self.signal_order, self.pad_order = _orders(signal_order, pad_order)
        self.config = config or SpectrogramConfig()

    def metadata(self) -> dict[str, object]:
        return {
            **self.config.to_dict(),
            "signal_order": list(self.signal_order),
            "pad_order": list(self.pad_order),
            "axis_order": ["lot", "signal", "pad", "frequency", "time"],
            "feature_shape": list(FEATURE_SHAPE),
            "dtype": "float32",
        }

    def fixed_trace(self, trace: Sequence[float]) -> NDArray[np.float32]:
        import numpy as np

        raw = np.asarray(trace)
        if raw.ndim != 1 or raw.dtype.kind not in "fiu":
            raise ValueError("Trace must be a one-dimensional real numeric sequence")
        values = raw.astype(np.float32)
        finite = values[np.isfinite(values)]
        if not finite.size:
            raise ValueError("Trace has no finite observations")
        pad_value = float(np.median(finite))
        if len(values) >= self.config.fixed_seconds:
            values = values[-self.config.fixed_seconds :]
        else:
            values = np.pad(
                values,
                (self.config.fixed_seconds - len(values), 0),
                mode="constant",
                constant_values=pad_value,
            )
        return np.nan_to_num(values, nan=pad_value, posinf=pad_value, neginf=pad_value)

    def transform_trace(self, trace: Sequence[float]) -> NDArray[np.float32]:
        import numpy as np
        from scipy.signal import spectrogram

        _, _, magnitude = spectrogram(
            self.fixed_trace(trace),
            fs=self.config.sample_rate_hz,
            window="hann",
            nperseg=self.config.nfft,
            noverlap=self.config.noverlap,
            nfft=self.config.nfft,
            detrend="constant",
            return_onesided=True,
            scaling="spectrum",
            mode="magnitude",
        )
        magnitude = magnitude.astype(np.float32)
        if self.config.log1p:
            magnitude = np.log1p(np.maximum(magnitude, 0.0))
        if magnitude.shape != FEATURE_SHAPE[-2:] or not np.isfinite(magnitude).all():
            raise ValueError("Spectrogram calculation produced invalid output")
        return magnitude

    def transform_batch(
        self, lots: Sequence[Mapping[str, Mapping[str, Sequence[float]]]]
    ) -> NDArray[np.float32]:
        """Build [lot, signal, pad, frequency, time] from named signal/pad traces."""
        import numpy as np

        if not lots:
            raise ValueError("At least one Lot is required")
        result = np.empty((len(lots), *FEATURE_SHAPE), dtype=np.float32)
        for lot_index, lot in enumerate(lots):
            if not isinstance(lot, Mapping) or set(lot) != set(self.signal_order):
                raise ValueError("Lot signals do not match the declared signal order")
            for signal_index, signal in enumerate(self.signal_order):
                pads = lot[signal]
                if not isinstance(pads, Mapping) or set(pads) != set(self.pad_order):
                    raise ValueError("Each signal must contain exactly Pads P1, P2, P3, P4")
                for pad_index, pad in enumerate(self.pad_order):
                    result[lot_index, signal_index, pad_index] = self.transform_trace(pads[pad])
        return result


@dataclass(frozen=True)
class FittedSpectrogramNormalizer:
    """Statistics over unique training Lots and time, retaining signal/pad/frequency."""

    mean: NDArray[np.float32]
    std: NDArray[np.float32]
    signal_order: tuple[str, ...]
    pad_order: tuple[str, ...]
    train_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        import numpy as np

        signals, pads = _orders(self.signal_order, self.pad_order)
        for name in ("mean", "std"):
            values = np.array(getattr(self, name), dtype=np.float32, copy=True)
            if values.shape != (1, 7, 4, 9, 1) or not np.isfinite(values).all():
                raise ValueError("Invalid fitted spectrogram statistics")
            if name == "std" and (values <= 0).any():
                raise ValueError("Fitted spectrogram scales must be positive")
            values.setflags(write=False)
            object.__setattr__(self, name, values)
        indices = tuple(self.train_indices)
        if (
            not indices
            or any(type(index) is not int or index < 0 for index in indices)
            or len(set(indices)) != len(indices)
        ):
            raise ValueError("Training indices must be unique nonnegative integers")
        object.__setattr__(self, "signal_order", signals)
        object.__setattr__(self, "pad_order", pads)
        object.__setattr__(self, "train_indices", indices)

    @classmethod
    def fit(
        cls,
        features: Any,
        train_indices: Sequence[int],
        *,
        signal_order: Sequence[str],
        pad_order: Sequence[str] = PAD_ORDER,
    ) -> FittedSpectrogramNormalizer:
        import numpy as np

        values = _features(features)
        signals, pads = _orders(signal_order, pad_order)
        indices = tuple(train_indices)
        if (
            not indices
            or any(
                isinstance(index, (bool, np.bool_))
                or not isinstance(index, (int, np.integer))
                or index < 0
                or index >= len(values)
                for index in indices
            )
            or len(set(indices)) != len(indices)
        ):
            raise ValueError("Training indices must select unique valid Lot rows")
        training = values[list(indices)]
        mean = training.mean(axis=(0, 4), keepdims=True)
        std = training.std(axis=(0, 4), keepdims=True)
        std[~np.isfinite(std) | (std < 1e-6)] = 1.0
        return cls(mean, std, signals, pads, tuple(int(index) for index in indices))

    def transform(
        self,
        features: Any,
        *,
        signal_order: Sequence[str],
        pad_order: Sequence[str] = PAD_ORDER,
    ) -> NDArray[np.float32]:
        import numpy as np

        if _orders(signal_order, pad_order) != (self.signal_order, self.pad_order):
            raise ValueError("Spectrogram signal/Pad order differs from fitted normalization")
        normalized = (_features(features) - self.mean) / self.std
        if not np.isfinite(normalized).all():
            raise ValueError("Normalization produced non-finite features")
        return normalized

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "fdc_spectrogram_normalizer_v1",
            "fit_split": "train",
            "reduction_axes": [0, 4],
            "feature_shape": list(FEATURE_SHAPE),
            "signal_order": list(self.signal_order),
            "pad_order": list(self.pad_order),
            "train_indices": list(self.train_indices),
            "mean": self.mean.tolist(),
            "std": self.std.tolist(),
        }

    @classmethod
    def from_dict(cls, state: Mapping[str, Any]) -> FittedSpectrogramNormalizer:
        if (
            state.get("version") != "fdc_spectrogram_normalizer_v1"
            or state.get("fit_split") != "train"
            or state.get("reduction_axes") != [0, 4]
            or state.get("feature_shape") != list(FEATURE_SHAPE)
        ):
            raise ValueError("Unsupported fitted spectrogram normalization contract")
        return cls(
            state["mean"],
            state["std"],
            tuple(state["signal_order"]),
            tuple(state["pad_order"]),
            tuple(state["train_indices"]),
        )
