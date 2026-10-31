"""Evaluate the exact exported inference bundle, never an unrelated in-memory model."""

from collections.abc import Callable
import json
from uuid import uuid4
from typing import Any

from src.domain.ml import canonical_json, digest
from src.domain.model_registry.models import Evaluation, ModelBinding
from src.infrastructure.serving.materializer import BundleMaterializer
from src.infrastructure.datasets.snapshots import FileDatasetSource
from src.infrastructure.serving.bounded_prediction import Runtime
from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
from src.infrastructure.serving.ebm_runtime import EBMRuntime
from src.domain.training.workloads import EBM_WORKLOAD, workload_for_spec
from src.domain.ml import MLError


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


class EBMBundleEvaluator:
    """Score the exact frozen neural artifact; operator policy is frozen in the spec."""

    def __init__(self, materializer: BundleMaterializer,
                 runtime_factory: Callable[..., EBMRuntime], dataset_provider: EBMFileDatasetSource,
                 metric_calculator: Callable[..., dict[str, Any]],
                 policy_decider: Callable[[dict[str, Any], dict[str, Any]], str]) -> None:
        self.materializer = materializer
        self.runtime_factory = runtime_factory
        self.dataset_provider = dataset_provider
        self.metric_calculator = metric_calculator
        self.policy_decider = policy_decider

    def evaluate(self, binding: ModelBinding, training_spec_json: str) -> Evaluation:
        spec = json.loads(training_spec_json)
        if workload_for_spec(spec) != EBM_WORKLOAD:
            raise MLError("EBM evaluator received an incompatible workload")
        directory = self.materializer.materialize(binding.artifact_ref, binding.manifest_digest)
        runtime = self.runtime_factory(directory, binding.manifest_digest)
        try:
            reference = spec["dataset"]
            snapshot = self.dataset_provider.load(reference)
            if (runtime.manifest["lineage"]["dataset_digest"] != snapshot.content_digest
                    or runtime.manifest["lineage"]["training_spec_digest"] != digest(
                        ["training-spec-v1", training_spec_json])):
                raise MLError("EBM evaluation dataset does not match the exported model lineage")
            predictions = []
            for offset in range(0, len(snapshot.metadata.test), runtime.max_batch):
                indices = list(snapshot.metadata.test[offset:offset + runtime.max_batch])
                predictions.extend(runtime.predict(snapshot.spectrograms[indices],
                    signal_order=snapshot.metadata.signal_order, pad_order=snapshot.metadata.pad_order))
            metrics = self.metric_calculator(predictions, snapshot.metadata, snapshot.metadata.test,
                                            cdf_anchor_eps=spec["training"]["cdf_anchor_eps"])
        finally:
            runtime.close()
        policy = spec.get("evaluation_policy_config")
        policy_version = spec["evaluation_policy"]
        if policy is None:
            if policy_version != "ebm-unqualified-v1":
                raise MLError("EBM evaluation is missing its frozen quality policy")
            decision = "REJECTED_BY_POLICY"
            metrics["quality_policy_configured"] = False
        else:
            if not isinstance(policy, dict) or policy.get("policy_version") != policy_version:
                raise MLError("EBM evaluation policy does not match its frozen version")
            decision = self.policy_decider(policy, metrics)
            if decision not in {"APPROVED_BY_POLICY", "REJECTED_BY_POLICY"}:
                raise MLError("Invalid EBM policy decision")
            metrics["quality_policy_configured"] = True
        report = {"model_binding_id": binding.binding_id, "manifest_digest": binding.manifest_digest,
            "dataset_digest": snapshot.content_digest, "policy_version": policy_version,
            "policy_config_digest": digest(policy), "evaluator_version": EBM_WORKLOAD.evaluator_version,
            "semantics": "retrospective_replay", "decision": decision, "metrics": metrics}
        return Evaluation(str(uuid4()), binding.binding_id, binding.manifest_digest,
            snapshot.content_digest, policy_version, EBM_WORKLOAD.evaluator_version,
            decision, canonical_json(metrics), digest(report))


class WorkloadBundleEvaluator:
    def __init__(self, classifier: BundleEvaluator, ebm: EBMBundleEvaluator) -> None:
        self.classifier, self.ebm = classifier, ebm

    def evaluate(self, binding: ModelBinding, training_spec_json: str) -> Evaluation:
        workload = workload_for_spec(json.loads(training_spec_json))
        if workload == EBM_WORKLOAD:
            return self.ebm.evaluate(binding, training_spec_json)
        return self.classifier.evaluate(binding, training_spec_json)
