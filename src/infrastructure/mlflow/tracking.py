"""Explicit run IDs; no process-global fluent MLflow active run."""

import hashlib
import math
import shutil
from pathlib import Path
from uuid import uuid4

from src.domain.ml import canonical_json, Conflict, MLError


class LocalExperimentTracker:
    """Durable local development registry, explicitly forbidden outside dev."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def start(self, attempt_id: str, tags: dict[str, str]) -> str:
        run = self.root/attempt_id
        run.mkdir(exist_ok=True)
        (run/"tags.json").write_text(canonical_json(tags))
        return attempt_id

    def metrics(self, run_id: str, values: dict[str, float], step: int) -> None:
        if not all(math.isfinite(x) for x in values.values()):
            raise MLError("Non-finite metrics")
        (self.root/run_id/f"metrics-{step}.json").write_text(canonical_json(values))

    def register(self, run_id: str, name: str, bundle_path: str,
                 manifest_digest: str, attempt_id: str) -> tuple[str, str]:
        destination = self.root/run_id/"bundle"
        if destination.exists():
            if hashlib.sha256((destination/"manifest.json").read_bytes()).hexdigest() != manifest_digest:
                raise Conflict("Run already contains another bundle")
        else:
            temporary = self.root/run_id/f"bundle-{uuid4()}.tmp"
            shutil.copytree(bundle_path, temporary)
            temporary.rename(destination)
        return attempt_id, str(destination)

    def finish(self, run_id: str, status: str) -> None:
        (self.root/run_id/"status.json").write_text(canonical_json({"status": status}))


class MLflowExperimentTracker:
    def __init__(self, tracking_uri: str, experiment_name: str = "ml-training") -> None:
        from mlflow import MlflowClient
        self.client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
        experiment = self.client.get_experiment_by_name(experiment_name)
        self.experiment_id = experiment.experiment_id if experiment else self.client.create_experiment(
            experiment_name)

    def start(self, attempt_id: str, tags: dict[str, str]) -> str:
        runs = self.client.search_runs([self.experiment_id],
                                       f"tags.attempt_id = '{attempt_id}'", order_by=["attributes.start_time ASC"], max_results=10)
        if runs:
            return runs[0].info.run_id
        return self.client.create_run(self.experiment_id, tags={**tags, "attempt_id": attempt_id}).info.run_id

    def metrics(self, run_id: str, values: dict[str, float], step: int) -> None:
        import time
        for name, value in values.items():
            if not math.isfinite(value):
                raise MLError("Non-finite metrics")
            self.client.log_metric(run_id, name, value, timestamp=int(time.time()*1000), step=step)

    def register(self, run_id: str, name: str, bundle_path: str,
                 manifest_digest: str, attempt_id: str) -> tuple[str, str]:
        # Registration retries reconcile by the immutable origin before creating a version.
        matches = self.client.search_model_versions(f"name = '{name}'")
        for version in sorted(matches, key=lambda v: int(v.version)):
            if version.tags.get("attempt_id") == attempt_id:
                if version.tags.get("manifest_digest") != manifest_digest:
                    raise Conflict("Registry origin has a different artifact")
                return str(version.version), f"runs:/{run_id}/bundle"
        self.client.log_artifacts(run_id, bundle_path, artifact_path="bundle")
        existing = self.client.get_registered_model(name) if matches else None
        if existing is None:
            from mlflow.exceptions import MlflowException
            try:
                self.client.create_registered_model(name)
            except MlflowException as error:
                if error.error_code != "RESOURCE_ALREADY_EXISTS":
                    raise
        version = self.client.create_model_version(name, f"runs:/{run_id}/bundle", run_id,
                                                   tags={"attempt_id": attempt_id, "manifest_digest": manifest_digest})
        return str(version.version), f"runs:/{run_id}/bundle"

    def finish(self, run_id: str, status: str) -> None:
        self.client.set_terminated(run_id, status=status)
