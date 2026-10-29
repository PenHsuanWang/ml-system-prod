from typing import Protocol

from src.domain.prediction.models import Prediction


class PredictionExecutor(Protocol):
    def predict(self, values: tuple[tuple[float, float], ...]) -> tuple[Prediction, ...]: ...
