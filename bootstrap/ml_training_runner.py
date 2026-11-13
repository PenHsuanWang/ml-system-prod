"""Trusted child entry point. No HTTP, queue consumers or database credentials."""

import argparse
import json
import os
import resource
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.domain.ml import MLError
from src.domain.training.workloads import (
    APPROVED_WORKLOADS,
    EBM_WORKLOAD,
    MLP_WORKLOAD,
    workload_for_spec,
)
from src.infrastructure.execution.local_process import process_identity


def _train_ebm(
    spec: dict[str, Any],
    root: Path,
    progress: Callable[[int, dict[str, float]], None],
    producer: dict[str, str],
    platform_spec_digest: str,
) -> None:
    dataset_root = Path(os.environ["ML_DATASET_ROOT"])
    if (
        spec.get("resource_profile_id") != "cpu-ebm-v1"
        or spec.get("model", {}).get("architecture") != "ebm_cnn_v1"
    ):
        raise MLError("EBM execution profile or model is not approved")
    from src.domain.training.ebm import EBMTrainingConfig
    from src.infrastructure.datasets.ebm_snapshots import EBMFileDatasetSource
    from src.plugins.models.ebm_cnn_v1 import EBMModelConfig
    from src.plugins.training.ebm_trainer_v1 import EBMTrainerV1

    snapshot = EBMFileDatasetSource(dataset_root).load(spec["dataset"])
    EBMTrainerV1().train(
        EBMTrainingConfig.from_dict(spec["training"]),
        root,
        progress,
        dataset=snapshot,
        code_digest=spec["code_revision"],
        model_config=EBMModelConfig.from_dict(spec["model"]["config"]),
        producer=producer,
        platform_spec_digest=platform_spec_digest,
    )


def _train_mlp(
    spec: dict[str, Any],
    root: Path,
    progress: Callable[[int, dict[str, float]], None],
    producer: dict[str, str],
    platform_spec_digest: str,
) -> None:
    dataset_root = Path(os.environ["ML_DATASET_ROOT"])
    if (
        spec.get("resource_profile_id") != "cpu-small-v1"
        or spec.get("model", {}).get("architecture") != "mlp_v1"
    ):
        raise MLError("MLP execution profile or model is not approved")
    from src.infrastructure.datasets.snapshots import FileDatasetSource
    from src.plugins.training.pytorch_trainer import PyTorchTrainer

    dataset = FileDatasetSource(dataset_root).load(spec["dataset"])
    PyTorchTrainer().train(spec, root, progress, producer=producer, dataset=dataset)


TrainingHandler = Callable[
    [dict[str, Any], Path, Callable[[int, dict[str, float]], None], dict[str, str], str], None
]


def _build_training_handlers(
    registrations: Iterable[tuple[str, TrainingHandler]],
) -> Mapping[str, TrainingHandler]:
    handlers: dict[str, TrainingHandler] = {}
    approved_ids = {workload.workload_id for workload in APPROVED_WORKLOADS.values()}
    for workload_id, handler in registrations:
        if (
            not isinstance(workload_id, str)
            or workload_id not in approved_ids
            or not callable(handler)
        ):
            raise MLError("Invalid approved training handler registration")
        if workload_id in handlers:
            raise MLError("Duplicate approved training handler")
        handlers[workload_id] = handler
    return MappingProxyType(handlers)


TRAINING_HANDLERS = _build_training_handlers(
    (
        (MLP_WORKLOAD.workload_id, _train_mlp),
        (EBM_WORKLOAD.workload_id, _train_ebm),
    )
)


def run_training(
    spec: dict[str, Any],
    root: Path,
    progress: Callable[[int, dict[str, float]], None],
    producer: dict[str, str],
    *,
    platform_spec_digest: str,
) -> None:
    workload = workload_for_spec(spec)
    handler = TRAINING_HANDLERS.get(workload.workload_id)
    if handler is None:
        raise MLError("Approved workload has no registered training handler")
    handler(spec, root, progress, producer, platform_spec_digest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    args = parser.parse_args()
    root = args.directory.resolve(strict=True)
    (root / "process.json").write_text(json.dumps(process_identity(os.getpid())))
    resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    # RSS is enforced by the role container/cgroup, not by a misleading Python lease.
    code = 1
    try:
        import torch

        from src.plugins.training.pytorch_trainer import atomic_json

        torch.set_num_interop_threads(1)
        from src.domain.ml import canonical_json, digest

        envelope = json.loads((root / "spec.json").read_text())
        spec = envelope["spec"]
        if digest(["training-spec-v1", canonical_json(spec)]) != envelope["training_spec_digest"]:
            raise ValueError("Execution spec digest mismatch")

        def progress(step: int, values: dict[str, float]) -> None:
            atomic_json(root / "progress.json", {"step": step, "metrics": values})

        run_training(
            spec,
            root,
            progress,
            envelope["producer"],
            platform_spec_digest=envelope["training_spec_digest"],
        )
        code = 0
    finally:
        (root / "exit.json").write_text(json.dumps({"code": code}))


if __name__ == "__main__":
    main()
