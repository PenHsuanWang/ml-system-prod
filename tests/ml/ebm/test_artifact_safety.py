"""Adversarial bundle contracts, bounded array loading and inference outputs."""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from src.domain.datasets.ebm import EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import MLError, digest
from src.infrastructure.serving.ebm_artifacts import (
    EBM_BUNDLE_FILES,
    EBMArtifactVerifier,
    load_npz,
    read_json,
)
from src.infrastructure.serving.ebm_runtime import EBMRuntime

CODE_DIGEST = "sha256:" + "a" * 64


def npy_bytes(shape, *, dtype="<f4", body=b"\x00" * 8):
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        stream, {"shape": shape, "fortran_order": False, "descr": dtype},
    )
    return stream.getvalue() + body


@pytest.mark.parametrize("case", ["huge_shape", "short", "extra", "object", "float64", "endian"])
def test_npz_headers_are_rejected_before_numpy_allocates(tmp_path, monkeypatch, case):
    shape, dtype, body = (2,), "<f4", b"\x00" * 8
    if case == "huge_shape":
        shape = (10**12,)
    elif case == "short":
        body = body[:-1]
    elif case == "extra":
        body += b"\x00"
    elif case == "object":
        dtype = "|O"
    elif case == "float64":
        dtype = "<f8"
    elif case == "endian":
        dtype = ">f4"
    path = tmp_path / "attack.npz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("value.npy", npy_bytes(shape, dtype=dtype, body=body))

    def forbidden_load(*args, **kwargs):
        pytest.fail("NPY preflight must reject this payload before np.load")

    monkeypatch.setattr(np, "load", forbidden_load)
    with pytest.raises(MLError, match="array contract"):
        load_npz(path, {"value": (2,)}, maximum_bytes=1024)


@pytest.mark.parametrize("case", ["extra_member", "duplicate_member", "zip_expansion"])
def test_npz_archive_contract_is_checked_before_array_loading(tmp_path, monkeypatch, case):
    path = tmp_path / "attack.npz"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        body = b"\x00" * (4096 if case == "zip_expansion" else 8)
        archive.writestr("value.npy", npy_bytes((2,), body=body))
        if case == "duplicate_member":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("value.npy", npy_bytes((2,)))
        elif case == "extra_member":
            archive.writestr("extra.npy", b"\x00")

    def forbidden_load(*args, **kwargs):
        pytest.fail("ZIP preflight must reject this archive before np.load")

    monkeypatch.setattr(np, "load", forbidden_load)
    with pytest.raises(MLError, match="array contract"):
        load_npz(path, {"value": (2,)}, maximum_bytes=1024)


@pytest.mark.parametrize("compressed", [False, True])
def test_npz_valid_arrays_roundtrip_and_nonfinite_values_fail(tmp_path, compressed):
    path = tmp_path / "values.npz"
    save = np.savez_compressed if compressed else np.savez
    values = np.array([0.25, 1.5], dtype=np.float32)
    save(path, value=values)
    actual = load_npz(path, {"value": (2,)}, maximum_bytes=1024)
    np.testing.assert_array_equal(actual["value"], values)
    values[0] = np.inf
    save(path, value=values)
    with pytest.raises(MLError, match="array contract"):
        load_npz(path, {"value": (2,)}, maximum_bytes=1024)


@pytest.mark.parametrize("parent_link", [False, True])
def test_artifact_readers_reject_file_and_parent_symlinks(tmp_path, parent_link):
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "value.json").write_text('{"valid": true}')
    np.savez(actual / "value.npz", value=np.ones(2, dtype=np.float32))
    alias = tmp_path / "alias"
    if parent_link:
        alias.symlink_to(actual, target_is_directory=True)
    else:
        alias.mkdir()
        for name in ("value.json", "value.npz"):
            (alias / name).symlink_to(actual / name)
    with pytest.raises(MLError, match="JSON contract"):
        read_json(alias / "value.json")
    with pytest.raises(MLError, match="array contract"):
        load_npz(alias / "value.npz", {"value": (2,)}, maximum_bytes=1024)


@pytest.mark.parametrize("body", [
    '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}',
    '{"value":1,"value":2}', '{"value":' + '[' * 2000 + '0' + ']' * 2000 + '}',
])
def test_json_contract_rejects_nonfinite_duplicate_and_deep_values(tmp_path, body):
    path = tmp_path / "invalid.json"
    path.write_text(body)
    with pytest.raises(MLError, match="JSON contract"):
        read_json(path)


def test_importing_ebm_adapters_does_not_load_numerical_runtimes():
    code = """
import builtins
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'numpy', 'scipy', 'torch'}:
        raise AssertionError('Numerical dependency imported: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
import src.infrastructure.datasets.ebm_snapshots
import src.infrastructure.serving.ebm_artifacts
import src.infrastructure.serving.ebm_runtime
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[3],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def write_json(path, value):
    path.write_text(json.dumps(value))


def seal_bundle(directory):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifact"]["files"] = [
        {"path": name, "size_bytes": (directory / name).stat().st_size,
         "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
        for name in sorted(EBM_BUNDLE_FILES)
    ]
    write_json(path, manifest)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def base_bundle(tmp_path_factory):
    torch = pytest.importorskip("torch")
    from src.plugins.features.fdc_spectrogram_v1 import SpectrogramConfig
    from src.plugins.models.ebm_cnn_v1 import EBMModelConfig, build_ebm_cnn

    torch.set_num_threads(1)
    directory = tmp_path_factory.mktemp("ebm-safe-bundle")
    config = EBMModelConfig(
        channels=(2,), kernel_sizes=((3, 3),), strides=((1, 1),), features_per_pad=1,
    ).to_dict()
    model = build_ebm_cnn(config).eval()
    inputs = np.zeros((1, 7, 4, 9, 31), dtype=np.float32)
    with torch.inference_mode():
        mu, sigma, latent = model(torch.from_numpy(inputs))
        score = mu + 1.645 * sigma
    outputs = [{"mu": float(mu[0]), "sigma": float(sigma[0]), "latent": latent[0].tolist(),
                "raw_nn_score": float(score[0]),
                "score_kind": "uncalibrated_latent_mean_plus_std"}]
    torch.save(model.state_dict(), directory / "weights.pt")
    np.savez(directory / "normalization.npz",
             mean=np.zeros((1, 7, 4, 9, 1), dtype=np.float32),
             std=np.ones((1, 7, 4, 9, 1), dtype=np.float32))
    np.savez(directory / "golden_input.npz", spectrograms=inputs)
    spectral = SpectrogramConfig().to_dict()
    write_json(directory / "model_config.json", config)
    write_json(directory / "spectrogram_config.json", spectral)
    write_json(directory / "feature_schema.json", {
        "version": "ebm-spectrogram-input-v1", "shape": [None, 7, 4, 9, 31],
        "dtype": "float32", "granularity": "lot", "signal_order": EBM_SIGNAL_ORDER,
        "pad_order": EBM_PAD_ORDER, "spectrogram_config_digest": digest(spectral),
    })
    write_json(directory / "preprocessing.json", {
        "strategy": "ebm-train-lot-standardizer-v1", "fit_split": "train",
        "state_file": "normalization.npz", "signal_order": EBM_SIGNAL_ORDER,
        "pad_order": EBM_PAD_ORDER, "fit_dataset_digest": "b" * 64,
    })
    write_json(directory / "output_mapping.json", {
        "version": "ebm-neural-output-v1", "outputs": ["mu", "sigma", "latent", "raw_nn_score"],
        "score_kind": "uncalibrated_latent_mean_plus_std", "sigma_kind": "latent_feature_std",
        "risk_z": 1.645, "latent_dim": 28,
    })
    write_json(directory / "golden_io.json", {
        "samples": 1, "absolute_tolerance": 1e-5, "input_file": "golden_input.npz",
        "outputs": outputs,
    })
    write_json(directory / "manifest.json", {
        "manifest_version": "2",
        "artifact": {"format": "trusted_pytorch_state_dict", "profile": "ebm-neural-bundle-v1"},
        "model": {"architecture_key": "ebm_cnn_v1", "architecture_version": "1",
                  "trusted_code_digest": CODE_DIGEST, "constructor_config": config},
        "runtime": {"compatibility_id": "ebm-torch-cpu-v1", "device": "cpu",
                    "torch_version": torch.__version__},
        "contract": {"input_schema_version": "ebm-spectrogram-input-v1",
                     "output_schema_version": "ebm-neural-output-v1"},
        "lineage": {"dataset_digest": "b" * 64},
    })
    seal_bundle(directory)
    return directory


@pytest.fixture
def bundle(tmp_path, base_bundle):
    directory = tmp_path / "bundle"
    shutil.copytree(base_bundle, directory)
    return directory


def runtime(directory, **kwargs):
    from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn

    expected = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    return EBMRuntime(directory, expected, build_ebm_cnn,
                      expected_code_digest=CODE_DIGEST, **kwargs)


@pytest.mark.torch
@pytest.mark.parametrize("filename", [
    "weights.pt", "normalization.npz", "model_config.json", "golden_input.npz", "golden_io.json",
    "manifest.json",
])
def test_runtime_consumes_verified_bytes_despite_later_file_mutation(bundle, monkeypatch, filename):
    torch = pytest.importorskip("torch")
    golden = json.loads((bundle / "golden_io.json").read_text())["outputs"]
    verified_weights = (bundle / "weights.pt").read_bytes()
    original_verify = EBMArtifactVerifier.verified_payloads
    original_torch_load = torch.load
    original_numpy_load = np.load

    def mutate_after_verification(verifier, directory, expected_digest):
        manifest, payloads = original_verify(verifier, directory, expected_digest)
        assert set(payloads) == EBM_BUNDLE_FILES
        assert all(isinstance(value, bytes) for value in payloads.values())
        (directory / filename).write_bytes(b"replaced after verified payloads were captured")
        return manifest, payloads

    def checked_torch_load(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO)
        assert source.getvalue() == verified_weights
        return original_torch_load(source, *args, **kwargs)

    def checked_numpy_load(source, *args, **kwargs):
        assert isinstance(source, io.BytesIO)
        return original_numpy_load(source, *args, **kwargs)

    monkeypatch.setattr(EBMArtifactVerifier, "verified_payloads", mutate_after_verification)
    monkeypatch.setattr(torch, "load", checked_torch_load)
    monkeypatch.setattr(np, "load", checked_numpy_load)
    handle = runtime(bundle)
    output = handle.predict(np.zeros((1, 7, 4, 9, 31), dtype=np.float32),
                            signal_order=EBM_SIGNAL_ORDER, pad_order=EBM_PAD_ORDER)
    assert output == golden
    assert (bundle / filename).read_bytes().startswith(b"replaced after")


@pytest.mark.torch
def test_verifier_payload_api_preserves_manifest_api_and_size_limit(bundle):
    expected = seal_bundle(bundle)
    verifier = EBMArtifactVerifier()
    manifest, payloads = verifier.verified_payloads(bundle, expected)
    assert verifier.verify(bundle, expected) == manifest
    assert set(payloads) == EBM_BUNDLE_FILES
    assert sum(map(len, payloads.values())) <= verifier.maximum_bytes == 128 * 1024 * 1024
    with pytest.raises(MLError, match="verification"):
        EBMArtifactVerifier(maximum_bytes=32).verified_payloads(bundle, expected)


@pytest.mark.torch
@pytest.mark.parametrize("field,value", [
    ("version", "fdc_spectrogram_v2"), ("fixed_seconds", 255), ("sample_rate_hz", True),
    ("nperseg", 32), ("nfft", 32), ("noverlap", 4), ("window", "boxcar"),
    ("detrend", False), ("scaling", "density"), ("mode", "psd"),
    ("return_onesided", 1), ("log1p", 1), ("trace_policy", "keep_head"), ("extra", True),
])
def test_spectral_semantics_rejected_even_after_all_digests_are_updated(
    bundle, monkeypatch, field, value,
):
    torch = pytest.importorskip("torch")
    config = json.loads((bundle / "spectrogram_config.json").read_text())
    config[field] = value
    write_json(bundle / "spectrogram_config.json", config)
    schema = json.loads((bundle / "feature_schema.json").read_text())
    schema["spectrogram_config_digest"] = digest(config)
    write_json(bundle / "feature_schema.json", schema)
    seal_bundle(bundle)

    def forbidden_load(*args, **kwargs):
        pytest.fail("Invalid feature semantics must fail before weight deserialization")

    monkeypatch.setattr(torch, "load", forbidden_load)
    with pytest.raises(MLError, match="contract"):
        runtime(bundle)


@pytest.mark.torch
@pytest.mark.parametrize("case", ["unknown_signal", "reordered_signals", "reordered_pads"])
def test_bundle_requires_approved_signal_and_pad_order(bundle, case):
    feature = json.loads((bundle / "feature_schema.json").read_text())
    if case == "unknown_signal":
        feature["signal_order"][0] = "unknown_signal"
    elif case == "reordered_signals":
        feature["signal_order"].reverse()
    else:
        feature["pad_order"].reverse()
    write_json(bundle / "feature_schema.json", feature)
    preprocessing = json.loads((bundle / "preprocessing.json").read_text())
    preprocessing.update(signal_order=feature["signal_order"], pad_order=feature["pad_order"])
    write_json(bundle / "preprocessing.json", preprocessing)
    seal_bundle(bundle)
    with pytest.raises(MLError, match="contract"):
        runtime(bundle)


@pytest.mark.torch
@pytest.mark.parametrize("latent_dim", [True, 0, 27, 28.0, 1793])
def test_mapping_latent_dimension_must_match_bounded_model_constructor(bundle, latent_dim):
    mapping = json.loads((bundle / "output_mapping.json").read_text())
    mapping["latent_dim"] = latent_dim
    write_json(bundle / "output_mapping.json", mapping)
    seal_bundle(bundle)
    with pytest.raises(MLError, match="contract"):
        runtime(bundle)


@pytest.mark.torch
def test_runtime_requires_explicit_trusted_code_binding(bundle):
    from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn

    expected = seal_bundle(bundle)
    with pytest.raises(TypeError, match="expected_code_digest"):
        EBMRuntime(bundle, expected, build_ebm_cnn)
    for code in (None, "", " ", "x" * 129):
        with pytest.raises(MLError, match="trusted code digest"):
            EBMRuntime(bundle, expected, build_ebm_cnn, expected_code_digest=code)
    with pytest.raises(MLError, match="contract"):
        EBMRuntime(bundle, expected, build_ebm_cnn, expected_code_digest="different-code")


@pytest.mark.torch
@pytest.mark.parametrize("parent_link", [False, True])
def test_runtime_rejects_symlinked_bundle_or_payload(bundle, tmp_path, parent_link):
    from src.plugins.models.ebm_cnn_v1 import build_ebm_cnn

    expected = seal_bundle(bundle)
    if parent_link:
        alias = tmp_path / "alias"
        alias.symlink_to(bundle, target_is_directory=True)
        directory = alias
    else:
        target = tmp_path / "weights.pt"
        (bundle / "weights.pt").rename(target)
        (bundle / "weights.pt").symlink_to(target)
        directory = bundle
    with pytest.raises(MLError, match="verification"):
        EBMRuntime(directory, expected, build_ebm_cnn, expected_code_digest=CODE_DIGEST)


@pytest.mark.torch
@pytest.mark.parametrize("case", [
    "short_mu", "long_mu", "short_sigma", "long_sigma", "zero_sigma", "negative_sigma",
    "nan_mu", "inf_sigma", "nan_latent", "latent_width", "latent_rows", "integer",
    "complex", "numpy", "missing", "score_overflow",
])
def test_runtime_rejects_invalid_neural_outputs(bundle, case):
    torch = pytest.importorskip("torch")
    handle = runtime(bundle)
    mu, sigma, latent = torch.zeros(1), torch.ones(1), torch.zeros((1, 28))
    if case == "short_mu":
        mu = torch.zeros(0)
    elif case == "long_mu":
        mu = torch.zeros(2)
    elif case == "short_sigma":
        sigma = torch.ones(0)
    elif case == "long_sigma":
        sigma = torch.ones(2)
    elif case == "zero_sigma":
        sigma.zero_()
    elif case == "negative_sigma":
        sigma.neg_()
    elif case == "nan_mu":
        mu.fill_(float("nan"))
    elif case == "inf_sigma":
        sigma.fill_(float("inf"))
    elif case == "nan_latent":
        latent.fill_(float("nan"))
    elif case == "latent_width":
        latent = torch.zeros((1, 29))
    elif case == "latent_rows":
        latent = torch.zeros((2, 28))
    elif case == "integer":
        mu = torch.zeros(1, dtype=torch.int64)
    elif case == "complex":
        mu = torch.zeros(1, dtype=torch.complex64)
    elif case == "numpy":
        mu = np.zeros(1, dtype=np.float32)
    elif case == "score_overflow":
        mu.fill_(torch.finfo(torch.float32).max)
        sigma.fill_(torch.finfo(torch.float32).max)
    values = (mu, sigma) if case == "missing" else (mu, sigma, latent)
    handle.model = lambda features: values
    with pytest.raises(MLError, match="invalid neural outputs|non-finite raw scores"):
        handle.predict(np.zeros((1, 7, 4, 9, 31), dtype=np.float32),
                       signal_order=EBM_SIGNAL_ORDER, pad_order=EBM_PAD_ORDER)
