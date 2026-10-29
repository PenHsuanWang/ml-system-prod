"""One immutable CPU model handle per serving process."""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from torch.nn import Module

from src.domain.ml import CapacityExceeded, MLError, SchemaMismatch
from src.infrastructure.serving.artifacts import ArtifactVerifier


class PyTorchRuntime:
    def __init__(self, directory: Path, expected_digest: str,
                 model_factory: Callable[[dict[str, object]], Module],
                 *, max_inflight: int = 2, max_batch: int = 128,
                 expected_code_digest: str | None = None) -> None:
        import torch
        self.manifest = ArtifactVerifier().verify(directory, expected_digest)
        model_contract = self.manifest["model"]
        if (model_contract.get("architecture_key") != "mlp_v1" or
            model_contract.get("architecture_version") != "1" or
            expected_code_digest is not None and
                model_contract.get("trusted_code_digest") != expected_code_digest):
            raise MLError("Model code does not match the approved runtime")
        self._capacity = threading.BoundedSemaphore(max_inflight)
        self.max_batch = max_batch
        self._closed = False
        config = self.manifest["model"]["constructor_config"]
        self.model = model_factory(config)
        self.model.load_state_dict(torch.load(directory/"weights.pt", map_location="cpu",
                                              weights_only=True), strict=True)
        self.model.eval()
        self.preprocessing = json.loads((directory/"preprocessing.json").read_text())
        self.mapping = json.loads((directory/"output_mapping.json").read_text())
        feature_schema = json.loads((directory/"feature_schema.json").read_text())
        if (feature_schema.get("version") != "1" or
            [item.get("name") for item in feature_schema.get("features", [])] != ["sensor_a", "sensor_b"] or
            self.manifest.get("contract", {}).get("input_schema_version") != "1" or
            self.manifest.get("contract", {}).get("output_schema_version") != "1" or
            len(self.mapping.get("labels", [])) != 2 or
            len(set(self.mapping["labels"])) != 2 or
                self.mapping.get("score_kind") != "uncalibrated_softmax"):
            raise MLError("Input/output contract mismatch")
        if (len(self.preprocessing["mean"]) != 2 or len(self.preprocessing["scale"]) != 2 or
            self.preprocessing["feature_order"] != ["sensor_a", "sensor_b"] or any(
                not math.isfinite(v) or v <= 0 for v in self.preprocessing["scale"]
        ) or any(not math.isfinite(v) for v in self.preprocessing["mean"])):
            raise MLError("Invalid fitted preprocessing contract")
        golden = json.loads((directory/"golden_io.json").read_text())
        if (not 1 <= len(golden["inputs"]) <= 16 or
            len(golden["inputs"]) != len(golden["outputs"]) or
            any(len(row) != 2 for row in golden["outputs"]) or
            not 0 < golden["absolute_tolerance"] <= 1e-3 or any(
                not math.isfinite(number) for row in golden["outputs"] for number in row)):
            raise MLError("Invalid golden fixture")
        actual = self._probabilities(golden["inputs"])
        if any(not math.isfinite(number) for row in actual for number in row):
            raise MLError("Model produced non-finite golden output")
        if any(abs(a-b) > golden["absolute_tolerance"] for x, y in zip(actual, golden["outputs"])
               for a, b in zip(x, y)):
            raise MLError("Golden inference mismatch")

    def _probabilities(self, values: list[list[float]]) -> list[list[float]]:
        import torch
        means, scales = self.preprocessing["mean"], self.preprocessing["scale"]
        batch = [[(row[i]-means[i])/scales[i] for i in range(2)] for row in values]
        with torch.inference_mode():
            return cast(list[list[float]], self.model(torch.tensor(batch, dtype=torch.float32)).softmax(1).tolist())

    def predict(self, values: list[list[float]]) -> list[dict[str, object]]:
        if self._closed:
            raise MLError("Model handle is closed")
        if not values or len(values) > self.max_batch or any(
            len(row) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or
                                 not math.isfinite(x) or abs(x) > 100 for x in row)
            for row in values
        ):
            raise SchemaMismatch("Invalid features or batch")
        if not self._capacity.acquire(blocking=False):
            raise CapacityExceeded("Inference capacity is full")
        try:
            result = []
            for row in self._probabilities(values):
                label = max(range(len(row)), key=row.__getitem__)
                result.append({"class_id": label, "label": self.mapping["labels"][label],
                               "score": row[label], "score_kind": "uncalibrated_softmax"})
            return result
        finally:
            # This executes after physical computation, even if the HTTP client leaves.
            self._capacity.release()

    def close(self) -> None:
        self._closed = True
