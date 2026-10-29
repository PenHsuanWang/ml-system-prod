"""HTTP timeout never releases a slot still occupied by a physical Torch operation."""

import math
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Protocol

from src.domain.ml import CapacityExceeded, MLError
from src.domain.prediction.models import Prediction


class PredictionDeadline(MLError):
    code, status, retryable = "PREDICTION_DEADLINE", 504, True


class Runtime(Protocol):
    def predict(self, values: list[list[float]]) -> list[dict[str, object]]: ...
    def close(self) -> None: ...


class BoundedPredictionExecutor:
    def __init__(self, runtime: Runtime, *, capacity: int = 2, timeout: float = 5) -> None:
        if capacity < 1 or timeout <= 0:
            raise ValueError("Prediction limits must be positive")
        self.runtime = runtime
        self.capacity = threading.BoundedSemaphore(capacity)
        self.pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="inference")
        self.timeout = timeout
        self.closed = False

    def predict(self, values: tuple[tuple[float, float], ...]) -> tuple[Prediction, ...]:
        if self.closed or not self.capacity.acquire(blocking=False):
            raise CapacityExceeded("Prediction capacity is full")
        try:
            future = self.pool.submit(self.runtime.predict, [list(row) for row in values])
        except RuntimeError:
            self.capacity.release()
            raise CapacityExceeded("Prediction is shutting down") from None
        future.add_done_callback(lambda _: self.capacity.release())
        try:
            raw = future.result(timeout=self.timeout)
        except TimeoutError:
            raise PredictionDeadline("Prediction deadline exceeded") from None
        result = tuple(Prediction(**item) for item in raw)
        if len(result) != len(values) or any(not math.isfinite(p.score) or
                                             not 0 <= p.score <= 1 or p.class_id not in {0, 1} for p in result):
            raise MLError("Invalid model output")
        return result

    def close(self) -> None:
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.runtime.close()
