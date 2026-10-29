"""Own durable orchestration; all computation runs in a supervised child process."""

import json
import time
from dataclasses import asdict
from typing import Protocol
from uuid import uuid4

from src.application.model_registry.ports import ExperimentTracker, RegistryRepository
from src.application.training.ports import TrainingExecutor, TrainingRepository
from src.domain.ml import canonical_json, digest, Unavailable
from src.domain.model_registry.models import Evaluation, ModelBinding
from src.domain.training.execution import ExecutionHandle, TrainingOutput
from src.domain.training.models import Claim, TrainingCommand


class Evaluator(Protocol):
    def evaluate(self, binding: ModelBinding, training_spec_json: str) -> Evaluation: ...


class TrainingSupervisor:
    def __init__(self, repository: TrainingRepository, executor: TrainingExecutor,
                 registry: RegistryRepository, tracker: ExperimentTracker,
                 evaluator: Evaluator, *, timeout: int = 300, heartbeat: int = 2,
                 runtime_code: str | None = None, runtime_image: str | None = None) -> None:
        self.repository = repository
        self.executor = executor
        self.registry = registry
        self.tracker = tracker
        self.evaluator = evaluator
        self.timeout = timeout
        self.heartbeat = heartbeat
        self.runtime_code = runtime_code
        self.runtime_image = runtime_image

    def execute(self, command: TrainingCommand) -> None:
        claim = self.repository.claim(command)
        if claim.already_complete:
            return
        spec = json.loads(claim.job.spec_json)
        if (self.runtime_code is not None and spec["code_revision"] != self.runtime_code or
                self.runtime_image is not None and spec["training_image_digest"] != self.runtime_image):
            self.repository.stop(claim, "RUNTIME_QUALIFICATION_MISMATCH", confirmed=True)
            return
        envelope = {"spec": spec,
                    "training_spec_digest": claim.job.spec_digest,
                    "producer": {"job_id": claim.job.job_id, "attempt_id": claim.job.attempt_id}}
        handle = self.executor.prepare(canonical_json(envelope), claim.job.attempt_id,
                                       claim.owner_token)
        # The stable launch key is durable BEFORE spawning, including the crash window.
        self.repository.prepare_execution(claim, canonical_json(asdict(handle)))
        try:
            self.executor.launch(handle)
        except OSError:
            self.repository.stop(claim, "LAUNCH_FAILED", confirmed=True)
            return
        self.monitor(claim, handle)

    def monitor(self, claim: Claim, handle: ExecutionHandle) -> None:
        started = time.monotonic()
        next_heartbeat = 0.0
        observed_running = False
        while True:
            observation = self.executor.inspect(handle)
            if observation.state == "STOPPED":
                break
            now = time.monotonic()
            if now >= next_heartbeat:
                job = self.repository.heartbeat(claim)
                if observation.state == "RUNNING" and not observed_running:
                    self.repository.execution_started(claim, canonical_json(asdict(handle)))
                    observed_running = True
                if job.cancel_requested or now-started > self.timeout or time.time() > job.deadline:
                    confirmed = self.executor.stop(handle)
                    self.repository.stop(claim, "CANCELLED" if job.cancel_requested else "TRAINING_DEADLINE",
                                         confirmed=confirmed)
                    if not confirmed:
                        raise Unavailable("Execution requires reconciliation")
                    return
                next_heartbeat = now+self.heartbeat
            if observation.state == "UNKNOWN" and now-started > 10:
                self.repository.stop(claim, "EXECUTION_UNCERTAIN", confirmed=False)
                raise Unavailable("Execution requires reconciliation")
            time.sleep(0.05)
        job = self.repository.heartbeat(claim)
        if job.cancel_requested:
            self.repository.stop(claim, "CANCELLED", confirmed=True)
            return
        if time.time() > job.deadline:
            self.repository.stop(claim, "FINALIZATION_DEADLINE", confirmed=True)
            return
        result = self.executor.result(handle)
        if observation.exit_code != 0 or result is None:
            self.repository.stop(claim, "TRAINING_FAILED", confirmed=True)
            return
        self.finalize(claim, result)

    def finalize(self, claim: Claim, result: TrainingOutput) -> None:
        # A transient external failure leaves recoverable phase evidence; no second fit.
        self.repository.checkpoint_phase(claim, "REGISTER", canonical_json(asdict(result)))
        spec = json.loads(claim.job.spec_json)
        run_id = self.tracker.start(claim.job.attempt_id,
                                    {"training_job_id": claim.job.job_id, "spec_digest": claim.job.spec_digest,
                                     "dataset_version": spec["dataset"]["version"],
                                     "dataset_digest": spec["dataset"]["content_digest"],
                                     "recipe_version": spec["recipe"]["version"], "git_sha": spec["code_revision"],
                                     "image_digest": spec["training_image_digest"],
                                     "compatibility_id": "torch-cpu-v1", "evaluation_policy_version": spec["evaluation_policy"]})
        for item in result.metrics:
            self.tracker.metrics(run_id, dict(item.values), item.step)
        name = "tenant-"+digest(claim.job.tenant_id)[:16]+"-nn"
        version, artifact_ref = self.tracker.register(run_id, name, result.bundle_ref,
                                                      result.manifest_digest, claim.job.attempt_id)
        body = {"name": name, "version": version, "manifest_digest": result.manifest_digest,
                "run_id": run_id, "tenant_id": claim.job.tenant_id}
        binding = self.registry.save_binding(ModelBinding(str(uuid4()), claim.job.tenant_id,
                                                          claim.job.attempt_id, name, version, run_id, artifact_ref,
                                                          result.manifest_digest, digest(body)))
        self.repository.checkpoint_phase(claim, "EVALUATE")
        try:
            evaluation = self.evaluator.evaluate(binding, claim.job.spec_json)
        except Exception:
            # Record a truthful immutable ERROR while preserving bounded recovery.
            # Exception messages may contain SDK credentials or raw inputs.
            metrics = {"error_code": "EVALUATION_EXECUTION_FAILED"}
            report = {"model_binding_id": binding.binding_id, "manifest_digest": binding.manifest_digest,
                      "dataset_digest": spec["dataset"]["content_digest"], "policy_version": spec["evaluation_policy"],
                      "evaluator_version": "classifier-v1", "decision": "ERROR", "metrics": metrics}
            self.registry.save_evaluation(Evaluation(str(uuid4()), binding.binding_id,
                                                     binding.manifest_digest, spec["dataset"]["content_digest"], spec["evaluation_policy"],
                                                     "classifier-v1", "ERROR", canonical_json(metrics), digest(report)))
            raise
        evaluation = self.registry.save_evaluation(evaluation)
        self.repository.finish(claim, binding.binding_id, evaluation.evaluation_id)
        self.tracker.finish(run_id, "FINISHED")
