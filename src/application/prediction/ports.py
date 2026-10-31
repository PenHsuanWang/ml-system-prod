from typing import Protocol

from src.domain.prediction.models import Prediction
from src.domain.prediction.ebm import EBMPredictionBatch, EBMNeuralPrediction


class PredictionExecutor(Protocol):
    def predict(self, values: tuple[tuple[float, float], ...]) -> tuple[Prediction, ...]: ...


class EBMPredictionExecutor(Protocol):
    def predict(self, values: EBMPredictionBatch) -> tuple[EBMNeuralPrediction, ...]: ...
