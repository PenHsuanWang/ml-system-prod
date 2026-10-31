"""Immutable CPU inference from precomputed EBM spectrograms and a frozen bundle."""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from src.domain.datasets.ebm import EBM_FEATURE_SHAPE, EBM_PAD_ORDER, EBM_SIGNAL_ORDER
from src.domain.ml import CapacityExceeded, MLError, SchemaMismatch, digest
from src.infrastructure.serving.ebm_artifacts import (
    EBM_COMPATIBILITY_ID,
    EBMArtifactVerifier,
    load_json_bytes,
    load_npz_bytes,
)


def _validate_spectrogram_config(config: dict[str, Any]) -> None:
    """Approve the complete v1 feature contract without loading feature plugins."""
    expected = {
        "version": "fdc_spectrogram_v1",
        "fixed_seconds": 256,
        "sample_rate_hz": 1.0,
        "nperseg": 16,
        "nfft": 16,
        "noverlap": 8,
        "window": "hann",
        "detrend": "constant",
        "scaling": "spectrum",
        "mode": "magnitude",
        "return_onesided": True,
        "log1p": config.get("log1p"),
        "trace_policy": "keep_tail_left_pad_and_replace_nonfinite_with_finite_median",
    }
    if (config != expected or type(config.get("log1p")) is not bool
            or type(config.get("return_onesided")) is not bool
            or any(type(config.get(key)) is not int
                   for key in ("fixed_seconds", "nperseg", "nfft", "noverlap"))
            or isinstance(config.get("sample_rate_hz"), bool)
            or not isinstance(config.get("sample_rate_hz"), (int, float))):
        raise ValueError("Unsupported EBM spectrogram configuration")


class EBMRuntime:
    """Bootstrap supplies the approved factory; bundles cannot choose Python imports."""

    def __init__(self, directory: Path, expected_digest: str,
                 model_factory: Callable[[dict[str, Any]], Any], *, expected_code_digest: str,
                 max_batch: int = 128, max_inflight: int = 2) -> None:
        if (type(max_batch) is not int or not 1 <= max_batch <= 128
                or type(max_inflight) is not int or not 1 <= max_inflight <= 32):
            raise ValueError("Invalid EBM runtime capacity")
        if (not isinstance(expected_code_digest, str) or not 1 <= len(expected_code_digest) <= 128
                or expected_code_digest.strip() != expected_code_digest):
            raise MLError("EBM runtime requires a trusted code digest from its caller")
        # Verify every file before importing Torch or deserializing weights.
        self.manifest, payloads = EBMArtifactVerifier().verified_payloads(directory, expected_digest)
        self.max_batch = max_batch
        self._capacity = threading.BoundedSemaphore(max_inflight)
        self._closed = False
        try:
            import numpy as np
            import torch

            contract, model = self.manifest["contract"], self.manifest["model"]
            runtime = self.manifest["runtime"]
            if (model.get("architecture_key") != "ebm_cnn_v1"
                    or model.get("architecture_version") != "1"
                    or model.get("trusted_code_digest") != expected_code_digest
                    or runtime.get("compatibility_id") != EBM_COMPATIBILITY_ID
                    or runtime.get("device") != "cpu"
                    or runtime.get("torch_version", "").split(".")[:2]
                    != torch.__version__.split(".")[:2]
                    or contract != {"input_schema_version": "ebm-spectrogram-input-v1",
                                    "output_schema_version": "ebm-neural-output-v1"}):
                raise ValueError("EBM code/runtime contract mismatch")
            self.config = load_json_bytes(payloads["model_config.json"])
            if self.config != model["constructor_config"]:
                raise ValueError("EBM constructor config mismatch")
            features_per_pad = self.config.get("features_per_pad")
            if type(features_per_pad) is not int or not 1 <= features_per_pad <= 64:
                raise ValueError("Invalid EBM latent feature count")
            self.latent_dim = len(EBM_SIGNAL_ORDER) * len(EBM_PAD_ORDER) * features_per_pad
            feature = load_json_bytes(payloads["feature_schema.json"])
            self.signal_order = tuple(feature["signal_order"])
            self.pad_order = tuple(feature["pad_order"])
            if (feature.get("version") != "ebm-spectrogram-input-v1"
                    or feature.get("shape") != [None, 7, 4, 9, 31]
                    or feature.get("dtype") != "float32"
                    or feature.get("granularity") != "lot"
                    or self.signal_order != EBM_SIGNAL_ORDER or self.pad_order != EBM_PAD_ORDER):
                raise ValueError("EBM feature contract mismatch")
            self.spectrogram_config = load_json_bytes(payloads["spectrogram_config.json"])
            _validate_spectrogram_config(self.spectrogram_config)
            if feature.get("spectrogram_config_digest") != digest(self.spectrogram_config):
                raise ValueError("EBM spectrogram config mismatch")
            preprocessing = load_json_bytes(payloads["preprocessing.json"])
            if (preprocessing.get("strategy") != "ebm-train-lot-standardizer-v1"
                    or preprocessing.get("fit_split") != "train"
                    or preprocessing.get("state_file") != "normalization.npz"
                    or preprocessing.get("signal_order") != list(self.signal_order)
                    or preprocessing.get("pad_order") != list(self.pad_order)
                    or preprocessing.get("fit_dataset_digest")
                    != self.manifest["lineage"]["dataset_digest"]):
                raise ValueError("EBM fitted preprocessing mismatch")
            arrays = load_npz_bytes(payloads["normalization.npz"],
                                   {"mean": (1, 7, 4, 9, 1), "std": (1, 7, 4, 9, 1)},
                                   maximum_bytes=16384)
            self.mean, self.std = arrays["mean"], arrays["std"]
            if np.any(self.std <= 0):
                raise ValueError("EBM normalization scale must be positive")
            self.mapping = load_json_bytes(payloads["output_mapping.json"])
            risk_z = self.mapping.get("risk_z")
            if (self.mapping.get("version") != "ebm-neural-output-v1"
                    or self.mapping.get("score_kind") != "uncalibrated_latent_mean_plus_std"
                    or self.mapping.get("sigma_kind") != "latent_feature_std"
                    or self.mapping.get("outputs") != ["mu", "sigma", "latent", "raw_nn_score"]
                    or type(self.mapping.get("latent_dim")) is not int
                    or self.mapping["latent_dim"] != self.latent_dim
                    or isinstance(risk_z, bool) or not isinstance(risk_z, (int, float))
                    or not math.isfinite(risk_z) or not 0 <= risk_z <= 10):
                raise ValueError("EBM output mapping mismatch")
            self.risk_z = float(risk_z)
            self.model = model_factory(self.config)
            self.model.load_state_dict(torch.load(io.BytesIO(payloads["weights.pt"]),
                                                 map_location="cpu", weights_only=True), strict=True)
            self.model.to("cpu")
            self.model.eval()
            golden = load_json_bytes(payloads["golden_io.json"])
            count = golden["samples"]
            tolerance = golden["absolute_tolerance"]
            if (type(count) is not int or not 1 <= count <= 4
                    or isinstance(tolerance, bool) or not isinstance(tolerance, (float, int))
                    or not 0 < tolerance <= 1e-4
                    or golden.get("input_file") != "golden_input.npz"):
                raise ValueError("Invalid EBM golden fixture")
            inputs = load_npz_bytes(payloads["golden_input.npz"],
                                    {"spectrograms": (count, 7, 4, 9, 31)},
                                    maximum_bytes=4 * 7 * 4 * 9 * 31 * 4 + 1024)["spectrograms"]
            actual = self._outputs(inputs)
            if len(golden["outputs"]) != len(actual):
                raise ValueError("Invalid EBM golden output count")
            for observed, expected in zip(actual, golden["outputs"]):
                if set(expected) != set(observed):
                    raise ValueError("Invalid EBM golden output schema")
                for key in ("mu", "sigma", "raw_nn_score", "latent"):
                    a = np.asarray(observed[key], dtype=np.float64)
                    b = np.asarray(expected[key], dtype=np.float64)
                    if a.shape != b.shape or not np.isfinite(b).all() or not np.allclose(
                            a, b, atol=tolerance, rtol=0):
                        raise ValueError("EBM golden inference mismatch")
                if expected["score_kind"] != observed["score_kind"]:
                    raise ValueError("EBM golden score kind mismatch")
        except (KeyError, ValueError, TypeError, AttributeError, RuntimeError) as exc:
            raise MLError("EBM runtime contract or golden validation failed") from exc

    def _outputs(self, spectrograms: Any) -> list[dict[str, Any]]:
        import numpy as np
        import torch

        with np.errstate(over="ignore", invalid="ignore"):
            normalized = (spectrograms - self.mean) / self.std
        if not np.isfinite(normalized).all():
            raise SchemaMismatch("EBM normalization produced non-finite features")
        try:
            with torch.inference_mode():
                values = self.model(torch.from_numpy(np.ascontiguousarray(normalized)))
            if (not isinstance(values, (tuple, list)) or len(values) != 3
                    or any(not isinstance(value, torch.Tensor) or value.dtype != torch.float32
                           or value.device.type != "cpu" for value in values)):
                raise ValueError("Expected three CPU float32 tensors")
            mu, sigma, latent = values
            if (tuple(mu.shape) != (len(spectrograms),)
                    or tuple(sigma.shape) != (len(spectrograms),)
                    or tuple(latent.shape) != (len(spectrograms), self.latent_dim)
                    or not all(bool(torch.isfinite(value).all()) for value in values)
                    or bool((sigma <= 0).any())):
                raise ValueError("Invalid EBM neural output values or shapes")
        except (TypeError, ValueError, RuntimeError, AttributeError) as exc:
            raise MLError("EBM model produced invalid neural outputs") from exc
        if self.mapping["latent_dim"] != self.latent_dim:
            raise MLError("EBM model produced invalid neural outputs")
        scores = mu + float(self.risk_z) * sigma
        if not torch.isfinite(scores).all():
            raise MLError("EBM model produced non-finite raw scores")
        return [{"mu": float(mu[i]), "sigma": float(sigma[i]), "latent": latent[i].tolist(),
                 "raw_nn_score": float(scores[i]), "score_kind": self.mapping["score_kind"]}
                for i in range(len(spectrograms))]

    def predict(self, spectrograms: Any, *, signal_order: Sequence[str],
                pad_order: Sequence[str]) -> list[dict[str, Any]]:
        import numpy as np

        if self._closed:
            raise MLError("EBM model handle is closed")
        if (tuple(signal_order) != self.signal_order or tuple(pad_order) != self.pad_order
                or not isinstance(spectrograms, np.ndarray)
                or spectrograms.dtype != np.dtype("float32") or spectrograms.ndim != 5
                or spectrograms.shape[1:] != EBM_FEATURE_SHAPE
                or not 1 <= len(spectrograms) <= self.max_batch
                or not np.isfinite(spectrograms).all()):
            raise SchemaMismatch("Invalid EBM spectrogram batch or signal/Pad order")
        if not self._capacity.acquire(blocking=False):
            raise CapacityExceeded("EBM inference capacity is full")
        try:
            return self._outputs(spectrograms)
        finally:
            self._capacity.release()

    def close(self) -> None:
        self._closed = True
