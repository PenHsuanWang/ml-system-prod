"""Bound EBM physical computation through deadlines and validate neural outputs."""

import math
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Protocol

from src.domain.ml import CapacityExceeded, MLError
from src.domain.prediction.ebm import EBMPredictionBatch, EBMNeuralPrediction
from src.infrastructure.serving.bounded_prediction import PredictionDeadline


class EBMRuntimePort(Protocol):
    def predict(self, spectrograms: Any, *, signal_order: tuple[str, ...],
                pad_order: tuple[str, ...]) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


def _finite(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


class BoundedEBMPredictionExecutor:
    def __init__(self, runtime: EBMRuntimePort, *, latent_dim: int,
                 capacity: int = 2, timeout: float = 5) -> None:
        if capacity < 1 or timeout <= 0 or not math.isfinite(timeout) or latent_dim < 1:
            raise ValueError("EBM prediction limits must be positive")
        self.runtime, self.latent_dim, self.timeout = runtime, latent_dim, timeout
        self.capacity = threading.BoundedSemaphore(capacity)
        self.pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="ebm-inference")
        self.closed = False

    def _compute(self, values: EBMPredictionBatch) -> list[dict[str, Any]]:
        import numpy as np
        array = np.asarray(values.spectrograms, dtype=np.float32)
        return self.runtime.predict(array, signal_order=values.signal_order, pad_order=values.pad_order)

    def predict(self, values: EBMPredictionBatch) -> tuple[EBMNeuralPrediction, ...]:
        if self.closed or not self.capacity.acquire(blocking=False):
            raise CapacityExceeded("EBM prediction capacity is full")
        try:
            future = self.pool.submit(self._compute, values)
        except RuntimeError:
            self.capacity.release()
            raise CapacityExceeded("EBM prediction is shutting down") from None
        future.add_done_callback(lambda _: self.capacity.release())
        try:
            raw = future.result(timeout=self.timeout)
        except TimeoutError:
            raise PredictionDeadline("EBM prediction deadline exceeded") from None
        if not isinstance(raw, list) or len(raw) != len(values.spectrograms):
            raise MLError("Invalid EBM output count")
        predictions = []
        for item in raw:
            if (not isinstance(item, dict)
                    or set(item) != {"mu", "sigma", "latent", "raw_nn_score", "score_kind"}
                    or item["score_kind"] != "uncalibrated_latent_mean_plus_std"
                    or any(not _finite(item[key]) for key in ("mu", "sigma", "raw_nn_score"))
                    or item["sigma"] <= 0 or not isinstance(item["latent"], (list, tuple))
                    or len(item["latent"]) != self.latent_dim
                    or any(not _finite(value) for value in item["latent"])):
                raise MLError("Invalid EBM neural output contract")
            predictions.append(EBMNeuralPrediction(float(item["mu"]), float(item["sigma"]),
                tuple(float(value) for value in item["latent"]), float(item["raw_nn_score"])))
        return tuple(predictions)

    def close(self) -> None:
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.runtime.close()
