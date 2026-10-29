"""Evaluate the exact exported inference bundle, never an unrelated in-memory model."""

from collections.abc import Callable
import json
from uuid import uuid4

from src.domain.ml import canonical_json, digest
from src.domain.model_registry.models import Evaluation, ModelBinding
from src.infrastructure.serving.materializer import BundleMaterializer
from src.infrastructure.datasets.snapshots import FileDatasetSource
from src.infrastructure.serving.bounded_prediction import Runtime


class BundleEvaluator:
    def __init__(self, materializer: BundleMaterializer, runtime_factory: Callable[..., Runtime],
                 dataset_provider: FileDatasetSource) -> None:
        self.materializer = materializer
        self.runtime_factory = runtime_factory
        self.dataset_provider = dataset_provider

    def evaluate(self, binding: ModelBinding, training_spec_json: str) -> Evaluation:
        directory = self.materializer.materialize(binding.artifact_ref, binding.manifest_digest)
        runtime = self.runtime_factory(directory, binding.manifest_digest)
        try:
            reference = json.loads(training_spec_json)["dataset"]
            snapshot = self.dataset_provider.load(reference)
            values = [list(snapshot.features[i]) for i in snapshot.test]
            labels = [snapshot.labels[i] for i in snapshot.test]
            predictions = []
            for offset in range(0, len(values), 128):
                predictions.extend(runtime.predict(values[offset:offset+128]))
        finally:
            runtime.close()
        accuracy = sum(p["class_id"] == label for p, label in zip(predictions, labels))/len(labels)
        metrics = {"accuracy": accuracy, "samples": len(labels)}
        data_digest = reference["content_digest"]
        decision = "APPROVED_BY_POLICY" if accuracy >= 0.75 else "REJECTED_BY_POLICY"
        report = {"model_binding_id": binding.binding_id, "manifest_digest": binding.manifest_digest,
                  "dataset_digest": data_digest, "policy_version": "demo-accuracy-v1",
                  "evaluator_version": "classifier-v1", "decision": decision, "metrics": metrics}
        return Evaluation(str(uuid4()), binding.binding_id, binding.manifest_digest,
                          data_digest, "demo-accuracy-v1", "classifier-v1", decision, canonical_json(metrics), digest(report))
