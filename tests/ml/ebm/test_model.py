"""Neural computation contracts; synthetic tensors are not PoC data parity."""

import io
import json
import subprocess
import sys

import pytest

from src.plugins.models.ebm_cnn_v1 import INPUT_SHAPE, EBMModelConfig, build_ebm_cnn

torch = pytest.importorskip("torch")
pytestmark = pytest.mark.torch


@pytest.fixture(autouse=True)
def bounded_cpu_threads():
    original = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(original)


def test_model_module_import_does_not_load_torch():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.plugins.models.ebm_cnn_v1; "
            "assert 'torch' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_default_model_outputs_and_state_dict_round_trip():
    torch.manual_seed(23)
    model = build_ebm_cnn().eval()
    value = torch.randn(2, *INPUT_SHAPE)
    with torch.inference_mode():
        mu, sigma, latent = model(value)
        encoded, reconstructed = model.autoencode(value)
    assert latent.shape == (2, 112)
    assert mu.shape == sigma.shape == (2,)
    assert reconstructed.shape == value.shape
    torch.testing.assert_close(encoded, latent, rtol=0, atol=0)
    torch.testing.assert_close(mu, latent.mean(dim=1), rtol=0, atol=0)
    torch.testing.assert_close(sigma, latent.std(dim=1, unbiased=False).clamp_min(1e-6))
    assert torch.isfinite(reconstructed).all()

    checkpoint = io.BytesIO()
    torch.save(model.state_dict(), checkpoint)
    checkpoint.seek(0)
    restored = build_ebm_cnn(json.loads(json.dumps(model.config.to_dict()))).eval()
    restored.load_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True))
    with torch.inference_mode():
        for expected, actual in zip((mu, sigma, latent), restored(value), strict=True):
            torch.testing.assert_close(actual, expected, rtol=0, atol=0)


def test_signal_autoencoder_is_shared_across_pads():
    model = build_ebm_cnn().eval()
    value = torch.randn(2, *INPUT_SHAPE)
    permutation = [2, 0, 3, 1]
    with torch.inference_mode():
        latent, reconstruction = model.autoencode(value)
        permuted_latent, permuted_reconstruction = model.autoencode(value[:, :, permutation])
    expected = latent.reshape(2, 7, 4, 4)[:, :, permutation].reshape(2, 112)
    torch.testing.assert_close(permuted_latent, expected, rtol=0, atol=0)
    torch.testing.assert_close(permuted_reconstruction, reconstruction[:, :, permutation])


def test_autoencoder_training_updates_encoder_and_decoder():
    model = build_ebm_cnn().train()
    value = torch.randn(2, *INPUT_SHAPE)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    encoder = model.signal_autoencoders[0].encoder_conv[0].weight
    decoder = model.signal_autoencoders[0].decoder_conv[-1].weight
    before_encoder, before_decoder = encoder.detach().clone(), decoder.detach().clone()
    latent, reconstruction = model.autoencode(value)
    loss = latent.square().mean() + (reconstruction - value).square().mean()
    loss.backward()
    assert torch.isfinite(loss)
    for parameter in model.parameters():
        assert parameter.grad is not None
        assert torch.isfinite(parameter.grad).all()
    optimizer.step()
    assert not torch.equal(before_encoder, encoder)
    assert not torch.equal(before_decoder, decoder)


def test_configuration_is_immutable_and_json_round_trips():
    config = EBMModelConfig(features_per_pad=3, dropout=0.0)
    assert EBMModelConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config
    model = build_ebm_cnn(config).eval()
    with torch.inference_mode():
        assert model(torch.zeros(1, *INPUT_SHAPE))[2].shape == (1, 84)
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        config.features_per_pad = 8


@pytest.mark.parametrize(
    "config",
    [
        {"channels": []},
        {"channels": [True, 32]},
        {"channels": [16.5, 32]},
        {"channels": [16, 129]},
        {"kernel_sizes": [[3, 3]]},
        {"kernel_sizes": [[0, 3], [3, 3]]},
        {"kernel_sizes": [3, 3]},
        {"strides": [[True, 1], [1, 1]]},
        {"seed_size": [4, 0]},
        {"seed_size": "4,4"},
        {"features_per_pad": False},
        {"features_per_pad": 0},
        {"dropout": float("nan")},
        {"dropout": float("inf")},
        {"dropout": True},
        {"dropout": 1.0},
        {"feedback_dim": 1},
        {"qk_dim": 64},
        {"use_aux_head": True},
        {"input_shape": [7, 4, 9, 31]},
    ],
)
def test_invalid_or_unimplemented_config_is_rejected(config):
    with pytest.raises(ValueError):
        build_ebm_cnn(config)


@pytest.mark.parametrize("shape", [(0, 7, 4, 9, 31), (2, 28, 9, 31), (2, 7, 4, 9, 30)])
def test_wrong_input_shape_is_rejected(shape):
    with pytest.raises(ValueError, match="shaped"):
        build_ebm_cnn()(torch.zeros(shape))


@pytest.mark.parametrize("dtype", [torch.float64, torch.int64, torch.float16])
def test_wrong_input_dtype_is_rejected(dtype):
    with pytest.raises(ValueError, match="float32"):
        build_ebm_cnn()(torch.zeros(1, *INPUT_SHAPE, dtype=dtype))


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_input_is_rejected(bad_value):
    value = torch.zeros(1, *INPUT_SHAPE)
    value[0, 0, 0, 0, 0] = bad_value
    with pytest.raises(ValueError, match="finite"):
        build_ebm_cnn().autoencode(value)
