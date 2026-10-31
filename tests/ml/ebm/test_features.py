import json

import numpy as np
import pytest
from scipy.signal import spectrogram

from src.plugins.features.fdc_spectrogram_v1 import (
    FEATURE_SHAPE,
    PAD_ORDER,
    FdcSpectrogramV1,
    FittedSpectrogramNormalizer,
    SpectrogramConfig,
)

SIGNALS = tuple(f"signal_{index}" for index in range(7))


def test_config_roundtrip_and_rejects_changed_numerical_contract():
    config = SpectrogramConfig(log1p=False)
    assert SpectrogramConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config
    state = config.to_dict()
    state["window"] = "boxcar"
    with pytest.raises(ValueError, match="contract"):
        SpectrogramConfig.from_dict(state)
    with pytest.raises(ValueError, match="requires"):
        SpectrogramConfig(nfft=32)


def test_trace_matches_notebook_scipy_reference():
    builder = FdcSpectrogramV1(SIGNALS)
    trace = (np.sin(np.arange(300, dtype=np.float32) / 5) + 3).astype(np.float32)
    _, _, reference = spectrogram(
        trace[-256:], fs=1.0, window="hann", nperseg=16, noverlap=8, nfft=16,
        detrend="constant", scaling="spectrum", mode="magnitude",
    )
    expected = np.log1p(np.maximum(reference.astype(np.float32), 0.0))
    actual = builder.transform_trace(trace)
    assert actual.shape == (9, 31)
    assert actual.dtype == np.float32
    np.testing.assert_array_equal(actual, expected)
    raw_builder = FdcSpectrogramV1(SIGNALS, config=SpectrogramConfig(log1p=False))
    np.testing.assert_array_equal(raw_builder.transform_trace(trace), reference)


def test_notebook_trace_padding_and_nonfinite_policy():
    builder = FdcSpectrogramV1(SIGNALS)
    result = builder.fixed_trace([1.0, np.nan, 3.0, np.inf])
    np.testing.assert_array_equal(result[:252], np.full(252, 2, dtype=np.float32))
    np.testing.assert_array_equal(result[-4:], [1, 2, 3, 2])
    np.testing.assert_array_equal(builder.fixed_trace(np.arange(300)), np.arange(44, 300))
    for trace in ([], [np.nan, np.inf], [[1, 2]], ["1", "2"], [True, False]):
        with pytest.raises(ValueError):
            builder.fixed_trace(trace)


def test_batch_uses_declared_names_and_preserves_signal_pad_axes():
    builder = FdcSpectrogramV1(SIGNALS)
    base = np.sin(np.arange(256, dtype=np.float32) / 5)
    lot = {
        signal: {pad: base * (1 + 4 * signal_index + pad_index)
                 for pad_index, pad in reversed(list(enumerate(PAD_ORDER)))}
        for signal_index, signal in reversed(list(enumerate(SIGNALS)))
    }
    batch = builder.transform_batch([lot, lot])
    assert batch.shape == (2, *FEATURE_SHAPE)
    assert batch.dtype == np.float32
    for signal_index, signal in enumerate(SIGNALS):
        for pad_index, pad in enumerate(PAD_ORDER):
            np.testing.assert_array_equal(
                batch[0, signal_index, pad_index], builder.transform_trace(lot[signal][pad])
            )
    np.testing.assert_array_equal(batch[0], batch[1])
    metadata = builder.metadata()
    assert metadata["signal_order"] == list(SIGNALS)
    assert metadata["pad_order"] == list(PAD_ORDER)
    assert metadata["axis_order"] == ["lot", "signal", "pad", "frequency", "time"]
    assert metadata["feature_shape"] == [7, 4, 9, 31]
    lot[SIGNALS[0]].pop("P4")
    with pytest.raises(ValueError, match="Pads"):
        builder.transform_batch([lot])
    with pytest.raises(ValueError, match="signals"):
        builder.transform_batch([{}])


@pytest.mark.parametrize("signals,pads", [
    (SIGNALS[:6], PAD_ORDER),
    (("same",) * 7, PAD_ORDER),
    (SIGNALS, tuple(reversed(PAD_ORDER))),
])
def test_feature_metadata_rejects_ambiguous_order(signals, pads):
    with pytest.raises(ValueError):
        FdcSpectrogramV1(signals, pad_order=pads)


def test_normalizer_fits_training_lots_and_time_only_and_roundtrips():
    rng = np.random.default_rng(17)
    features = rng.normal(size=(4, *FEATURE_SHAPE)).astype(np.float32)
    features[2:] += 1000
    normalizer = FittedSpectrogramNormalizer.fit(features, [0, 1], signal_order=SIGNALS)
    np.testing.assert_array_equal(normalizer.mean, features[:2].mean(axis=(0, 4), keepdims=True))
    np.testing.assert_array_equal(normalizer.std, features[:2].std(axis=(0, 4), keepdims=True))
    assert normalizer.mean.shape == (1, 7, 4, 9, 1)
    features[2:] += 1000
    refit = FittedSpectrogramNormalizer.fit(features, [0, 1], signal_order=SIGNALS)
    np.testing.assert_array_equal(refit.mean, normalizer.mean)
    np.testing.assert_array_equal(refit.std, normalizer.std)
    normalized = normalizer.transform(features, signal_order=SIGNALS)
    np.testing.assert_allclose(normalized[:2].mean(axis=(0, 4)), 0, atol=1e-6)
    np.testing.assert_allclose(normalized[:2].std(axis=(0, 4)), 1, atol=1e-6)
    restored = FittedSpectrogramNormalizer.from_dict(json.loads(json.dumps(normalizer.to_dict())))
    np.testing.assert_array_equal(restored.transform(features, signal_order=SIGNALS), normalized)
    assert restored.train_indices == (0, 1)
    assert not restored.mean.flags.writeable
    with pytest.raises(ValueError, match="order differs"):
        restored.transform(features, signal_order=tuple(reversed(SIGNALS)))


def test_constant_training_dimensions_use_unit_scale():
    features = np.full((2, *FEATURE_SHAPE), 3, dtype=np.float32)
    normalizer = FittedSpectrogramNormalizer.fit(features, [0], signal_order=SIGNALS)
    np.testing.assert_array_equal(normalizer.std, np.ones((1, 7, 4, 9, 1)))
    np.testing.assert_array_equal(normalizer.transform(features, signal_order=SIGNALS), 0)


@pytest.mark.parametrize("indices", [[], [0, 0], [-1], [2], [0.5], [True]])
def test_normalizer_rejects_invalid_or_repeated_training_lots(indices):
    with pytest.raises(ValueError, match="Training indices"):
        FittedSpectrogramNormalizer.fit(
            np.zeros((2, *FEATURE_SHAPE), dtype=np.float32), indices, signal_order=SIGNALS
        )


def test_normalizer_rejects_corrupt_shape_nonfinite_and_state():
    values = np.zeros((2, *FEATURE_SHAPE), dtype=np.float32)
    with pytest.raises(ValueError, match="shaped"):
        FittedSpectrogramNormalizer.fit(values[:, :, :, :, :30], [0], signal_order=SIGNALS)
    values[1, 0, 0, 0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        FittedSpectrogramNormalizer.fit(values, [0], signal_order=SIGNALS)
    values[1, 0, 0, 0, 0] = 0
    state = FittedSpectrogramNormalizer.fit(values, [0], signal_order=SIGNALS).to_dict()
    state["reduction_axes"] = [0]
    with pytest.raises(ValueError, match="contract"):
        FittedSpectrogramNormalizer.from_dict(state)
    state["reduction_axes"] = [0, 4]
    state["std"] = np.zeros((1, 7, 4, 9, 1)).tolist()
    with pytest.raises(ValueError, match="positive"):
        FittedSpectrogramNormalizer.from_dict(state)
