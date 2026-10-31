"""Framework-free precomputed spectrogram input and raw neural output DTOs."""

from dataclasses import dataclass

from src.domain.prediction.models import ServingDeployment

SpectrogramTime = tuple[float, ...]
SpectrogramFrequency = tuple[SpectrogramTime, ...]
SpectrogramPads = tuple[SpectrogramFrequency, ...]
Spectrogram = tuple[SpectrogramPads, ...]


@dataclass(frozen=True)
class EBMPredictionBatch:
    spectrograms: tuple[Spectrogram, ...]
    signal_order: tuple[str, ...]
    pad_order: tuple[str, ...]


@dataclass(frozen=True)
class EBMNeuralPrediction:
    mu: float
    sigma: float
    latent: tuple[float, ...]
    raw_nn_score: float
    score_kind: str = "uncalibrated_latent_mean_plus_std"


@dataclass(frozen=True)
class EBMPredictionResponse:
    predictions: tuple[EBMNeuralPrediction, ...]
    provenance: ServingDeployment
    request_id: str
